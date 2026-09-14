"""Client: validate -> build ts -> fetch -> block/unknown/echo checks -> parse (04 §5).

Lane D owns this file. Never indexes a payload; uses parse.entity_record().

The order of checks after a fetch is the load-bearing logic of the skill
(04 §5), so it lives in one place, ``Client.interpret``:

1. 3xx           -> HotelsHTTPError, raised by the transport (exit 3)
2. ds:2 absent   -> HotelsHTTPError "blocked" (exit 3) — never a phrase list
3. error ds:1    -> UnknownEntity, raised by the parser (exit 2)
4. echo mismatch -> EchoMismatch (a PayloadError, exit 3), naming both stays
4b. currency     -> QueryError (exit 2) — a refusal, separate from the echo
5. empty union   -> the caller's exit 1; 6. otherwise a priced record.

Every input Google would silently answer with the *default* stay (today + 31,
one night, two adults) is refused in ``validate_stay`` before any request; the
echo check is the second line of defence, not the first.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Iterator, Sequence

from .http import (  # noqa: F401 — THROTTLE_SECONDS is re-exported for callers
    THROTTLE_SECONDS, HotelsHTTPError, RequestBudgetError, block_reason, looks_blocked,
)
from .model import (
    Candidate, CompareBasis, Echo, EntityRecord, HotelIds, SellerRow, Stay,
    cheapest_seller,
)
from .parse import CURRENCIES, PayloadError, UnknownEntity, entity_record
from .ts import encode_ts

ENTITY_PATH = "/travel/hotels/entity/{token}"

MAX_OFFSET_DAYS = 330   # 01: +330 priced, +347 fell back to the default stay
MAX_NIGHTS = 30         # 01: 30 priced, 31 fell back
MIN_NIGHTS = 1
MAX_ADULTS = 8
MAX_CHILDREN = 6
MAX_CHILD_AGE = 17
# THROTTLE_SECONDS (2.5 s, the probe's pace) lives in http.py; imported above so it cannot drift.

#: `cheapest --days` ceiling. Entity pages are 1.6–4.3 MB each (04 §3.4).
MAX_SWEEP_DAYS = 21


def today() -> date:
    """The package's ONE clock. Every "in the past" check and every `+N` date
    goes through here so the self-check can pin the calendar (monkeypatch
    `ghotels.client.today`) and a captured stay never expires the suite.
    Nothing else in the package reads the system date directly."""
    return date.today()


def _clock() -> date:
    # looked up by name at call time, so a monkeypatched `today` is honoured
    # even inside validate_stay, whose parameter shadows the function
    return today()


class QueryError(Exception):
    """A refusal before or after one request (exit 2). Message says why and what to do."""


class EchoMismatch(PayloadError):
    """Google priced a different stay than asked (04 §5 step 4). Exit 3, never 1.

    A subclass so a sweep or shortlist can label the date/hotel "priced a
    different stay" while every other PayloadError keeps its own message.
    """


def _party(adults: int | None, child_ages: Sequence[int] | None) -> str:
    if adults is None:
        return "an unstated party"
    text = f"{adults} adult" + ("" if adults == 1 else "s")
    if child_ages is None:
        return text + " (no occupancy echoed)"
    if child_ages:
        text += " + " + ("child " if len(child_ages) == 1 else "children ") + ", ".join(
            str(a) for a in child_ages
        )
    return text


def describe_stay(checkin: date | None, checkout: date | None, adults: int | None,
                  child_ages: Sequence[int] | None) -> str:
    """"2026-12-31→2027-01-02 for 2 adults + child 5" — used in the mismatch message."""
    when = f"{checkin.isoformat() if checkin else '?'}→{checkout.isoformat() if checkout else '?'}"
    return f"{when} for {_party(adults, child_ages)}"


def validate_stay(checkin: date, checkout: date, adults: int, child_ages: Sequence[int],
                  currency: str, today: date | None = None) -> Stay:
    """Refuse every input Google would silently answer with the default stay (04 §5).

    A past check-in, a checkout on or before check-in, more than 30 nights and
    a check-in beyond ~330 days all return a normal page priced for today + 31,
    one night, two adults (01). Reported naively that is a confident wrong
    price, so each is a usage error here, before any request leaves.
    """
    today = today or _clock()
    if checkin < today:
        raise QueryError(
            f"check-in {checkin} is in the past — Google would silently price a "
            f"different stay instead of refusing (today is {today})"
        )
    horizon = today + timedelta(days=MAX_OFFSET_DAYS)
    if checkin > horizon:
        raise QueryError(
            f"check-in {checkin} is more than {MAX_OFFSET_DAYS} days out (the last "
            f"date Google prices is about {horizon}) — it would answer with its "
            f"default stay, not an error"
        )
    if checkout <= checkin:
        raise QueryError(
            f"checkout {checkout} is not after check-in {checkin} — a stay needs "
            f"at least {MIN_NIGHTS} night"
        )
    nights = (checkout - checkin).days
    if nights > MAX_NIGHTS:
        raise QueryError(
            f"{nights} nights is more than the {MAX_NIGHTS} Google will price in one "
            f"stay ({checkin} → {checkout}) — it would answer with its default stay"
        )
    if not 1 <= adults <= MAX_ADULTS:
        raise QueryError(f"--adults must be between 1 and {MAX_ADULTS} (got {adults})")
    ages = tuple(int(a) for a in child_ages)
    if len(ages) > MAX_CHILDREN:
        raise QueryError(
            f"at most {MAX_CHILDREN} children can be priced in one room (got {len(ages)})"
        )
    for age in ages:
        if not 0 <= age <= MAX_CHILD_AGE:
            raise QueryError(f"--child-age must be between 0 and {MAX_CHILD_AGE} (got {age})")
    code = (currency or "").upper()
    if code not in CURRENCIES:
        raise QueryError(
            f"currency {currency!r} is not one Google Hotels offers — it would "
            f"price in the market default instead of refusing. Use one of the "
            f"{len(CURRENCIES)} codes in its catalogue (CAD, USD, EUR, GBP, ...)"
        )
    return Stay(checkin=checkin, checkout=checkout, adults=adults, child_ages=ages, currency=code)


def cheapest_row(sellers: Sequence[SellerRow], basis: CompareBasis, per: str = "night",
                 ) -> tuple[SellerRow | None, bool]:
    """``model.cheapest_seller`` — the one ranking path, tie rule included —
    applied to the nightly price (``per="night"``) or the stay price
    (``per="stay"``). The stay case reuses the same function by swapping each
    row's stay into its nightly slot, so the two can never disagree on the rule.
    """
    rows = tuple(sellers)
    if per == "night":
        return cheapest_seller(rows, basis)
    swapped = [replace(s, nightly=s.stay) for s in rows]
    row, tie = cheapest_seller(tuple(swapped), basis)
    if row is None:
        return None, tie
    return rows[swapped.index(row)], tie


#: Exceptions that end a shortlist/sweep as a whole (exit 2) rather than one item.
_RUN_LEVEL = (QueryError, RequestBudgetError)


class Client:
    """One entity fetch per question. Never books, never caches (04 §8)."""

    def __init__(self, transport, *, hl: str = "en", gl: str = "CA"):
        self.transport = transport
        self.hl, self.gl = hl, gl

    # -- the two halves of a quote, split so `doctor` and tests can stub one --

    def fetch_entity(self, ids: HotelIds, stay: Stay) -> str:
        """The raw entity page for this stay. Raises HotelsHTTPError (3) only.

        `ts` carries dates, occupancy and currency (01 §ts); the URL's
        checkin=/checkout= are ignored by Google and never sent.
        """
        params = {
            "hl": self.hl,
            "gl": self.gl,
            "ts": encode_ts(stay.checkin, stay.checkout, stay.adults,
                            list(stay.child_ages), stay.currency),
        }
        return self.transport.get(ENTITY_PATH.format(token=ids.token), params)

    def interpret(self, html: str, stay: Stay) -> EntityRecord:
        """Steps 2–4b of 04 §5 on a fetched page."""
        if looks_blocked(html):
            raise HotelsHTTPError(
                f"Google served {block_reason(html)} instead of a hotel page "
                f"(www.google.com) — a block, not 'no rates'. Wait a few minutes "
                f"and run fewer requests back to back."
            )
        record = entity_record(html)  # UnknownEntity (2) / PayloadError (3) pass through
        if not record.echo.matches(stay):
            raise EchoMismatch(
                f"Google priced {_echo_text(record.echo)}, not "
                f"{describe_stay(stay.checkin, stay.checkout, stay.adults, stay.child_ages)}. "
                f"Refusing to report it — it is a real price for a different stay."
            )
        self._check_currency(record, stay)
        return record

    def quote(self, ids: HotelIds, stay: Stay) -> EntityRecord:
        """One entity fetch. Raises HotelsHTTPError (3), UnknownEntity (2), QueryError (2, currency), PayloadError (3, incl. echo mismatch)."""
        return self.interpret(self.fetch_entity(ids, stay), stay)

    def _check_currency(self, record: EntityRecord, stay: Stay) -> None:
        """Refuse to label a price in a currency Google did not use (04 §5 4b).

        Both `[6][1][3]` (the echo) and `p[15]` (the headline's currency) must
        equal the request; `p[15]` is null on a page with no headline, and then
        the echo alone decides. Absent evidence makes no claim.
        """
        seen = {c.upper() for c in (record.echo.currency, record.currency) if c}
        wrong = sorted(c for c in seen if c != stay.currency)
        if wrong:
            raise QueryError(
                f"Google priced this hotel in {', '.join(wrong)}, not the "
                f"{stay.currency} asked for — it ignores a currency it will not "
                f"price in rather than reporting an error. Re-run with "
                f"--currency {wrong[0]}, or a currency Google offers."
            )

    # -- fan-out: plan first, validate eagerly, fetch lazily ------------------
    #
    # Per item, EVERY exception is that item's failure (yielded, counted toward
    # the caller's exit-3 rule) — a drifted block raising a raw TypeError on
    # one hotel must not abort the other nine, and must never be a silent
    # skip. Only the run-level refusals propagate: a currency mismatch or a
    # budget shortfall is the same on every item and is exit 2. Ctrl-C is a
    # BaseException and passes through untouched.

    def shortlist(self, candidates: Sequence[Candidate], stay: Stay,
                  ) -> Iterator[tuple[Candidate, EntityRecord | None, Exception | None]]:
        """One fetch per candidate, budget planned up front, failures yielded not raised.

        A transport failure, an echo mismatch or an unknown id on ONE hotel is
        that hotel's failure (the CLI lists it under `failed[]`); a currency
        refusal or a budget shortfall is the run's, and propagates.
        """
        chosen = list(candidates)
        self.transport.plan(len(chosen), f"a shortlist of {len(chosen)} hotels")

        def run():
            for cand in chosen:
                try:
                    yield cand, self.quote(cand.ids, stay), None
                except _RUN_LEVEL:
                    raise
                except Exception as exc:  # one hotel's failure, never the run's
                    yield cand, None, exc

        return run()

    def sweep(self, ids: HotelIds, first_checkin: date, nights: int, days: int, step: int,
              adults: int, child_ages: Sequence[int], currency: str, today: date | None = None,
              ) -> Iterator[tuple[Stay, EntityRecord | None, Exception | None]]:
        """Eager validation of every visited date, then a lazy generator (one fetch per yield).

        Every shifted date is validated before the first request; a date past
        the horizon is a refusal, not a skip — skipping made the results cover
        a shorter window than --days asked for (flights, 02 §2.4).
        """
        if days < 1:
            raise QueryError("--days must be at least 1")
        if step < 1:
            raise QueryError(f"--step must be at least 1 (got {step})")
        if days > MAX_SWEEP_DAYS:
            raise QueryError(
                f"--days {days} exceeds the {MAX_SWEEP_DAYS}-day sweep limit — each "
                f"day is a separate 2–4 MB page"
            )
        stays = [
            validate_stay(first_checkin + timedelta(days=offset),
                          first_checkin + timedelta(days=offset + nights),
                          adults, child_ages, currency, today)
            for offset in range(0, days, step)
        ]
        self.transport.plan(len(stays), f"a {days}-day sweep at --step {step}")

        def run():
            for stay in stays:
                try:
                    yield stay, self.quote(ids, stay), None
                except _RUN_LEVEL:
                    raise
                except Exception as exc:  # one date's failure, never the sweep's
                    yield stay, None, exc

        return run()


def _echo_text(echo: Echo) -> str:
    return describe_stay(echo.checkin, echo.checkout, echo.adults, echo.child_ages)
