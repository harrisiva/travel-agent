"""High-level operations: build a query, fetch it, parse it, filter it.

This is the layer the CLI talks to. It owns three things the CLI should not
have to think about:

* **Point of sale.** ``gl`` and ``curr`` are always sent explicitly, so a
  result does not depend on which IP the request happened to leave from. Note
  what this does *not* claim: holding the currency constant and varying only
  ``gl`` returned identical fares on every route measured. The often-quoted
  "249 CAD versus 181 USD" pair is FX conversion (181/249 = 0.73), not a
  different fare feed. ``gl`` may still change carrier availability in markets
  we have not tested; it has simply never been shown to here.
* **The false-negative traps.** A query Google cannot answer returns an empty
  result set, not an error — indistinguishable from "this route has no
  flights" unless you check first. ``validate`` refuses those queries up front.
* **Filtering.** Anything Google will filter server-side goes into the query
  (fewer requests, and it changes what Google actually searches). Everything
  else is applied to the parsed results, where it costs nothing.

Fares are never cached. A stale "cheap" answer is worse than no answer, and
worse still than a stale campsite: it costs the user money. The only cacheable
things here — airline and airport catalogues — are small enough not to bother.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Iterable, Iterator

from .http import (
    MAX_MAX_REQUESTS, FlightsHTTPError, Transport, block_reason, looks_blocked,
)
from .model import Airport, Itinerary, RouteFilters, SearchResult
from .parse import (
    PayloadError, airports_in, detected_currency, parsed_query, search_result,
)
from .tfs import ONE_WAY, ROUND_TRIP, Query, Slice

SEARCH_PATH = "/travel/flights/search"

#: Free-text entry point. Google resolves a place name itself here, which is
#: the only key-free name -> airport resolution available: the payload carries
#: no airport catalogue to search offline.
RESOLVE_PATH = "/travel/flights"

#: Google returns an empty payload — not an error — for departures in the past
#: and for dates beyond its booking horizon. Both would read as "no flights".
MAX_DAYS_AHEAD = 330

#: Sanity ceiling on a date sweep, independent of the request budget: a range
#: this wide is nearly always a typo rather than an intention.
MAX_SWEEP_DAYS = 60


class QueryError(ValueError):
    """The query cannot be answered as asked. Always exit code 2."""


@dataclass(frozen=True)
class Filters:
    """Post-fetch filters, applied to parsed results.

    These deliberately do *not* go into ``tfs``: Google's own filter encoding
    for them is unmapped, and applying them here costs no extra request and no
    risk of silently searching for the wrong thing.
    """

    max_price: int | None = None
    airlines: tuple[str, ...] = ()
    exclude_airlines: tuple[str, ...] = ()
    max_duration_minutes: int | None = None
    min_legroom_inches: int | None = None
    avoid_aircraft: tuple[str, ...] = ()
    depart_after: str | None = None      # "HH:MM", local at the origin
    depart_before: str | None = None
    arrive_before: str | None = None
    arrive_same_day: bool = False
    avoid_layovers: tuple[str, ...] = ()
    max_co2_percent: int | None = None   # e.g. -20 = at least 20% below typical

    def apply(self, itineraries: Iterable[Itinerary]) -> list[Itinerary]:
        return [i for i in itineraries if self._keep(i)]

    def _keep(self, it: Itinerary) -> bool:
        if self.max_price is not None and (it.price is None or it.price > self.max_price):
            return False
        # Match every leg, not just the headline carrier: a mixed-carrier or
        # codeshare itinerary is labelled with one airline but flown by
        # several, so matching only `it.carrier` silently drops trips the user
        # asked for.
        carriers = {leg.carrier.upper() for leg in it.legs} | {it.carrier.upper()}
        if self.airlines and not (carriers & set(self.airlines)):
            return False
        if carriers & set(self.exclude_airlines):
            return False
        if (self.max_duration_minutes is not None
                and it.duration_minutes > self.max_duration_minutes):
            return False
        if self.min_legroom_inches is not None and not _legroom_ok(
            it, self.min_legroom_inches
        ):
            return False
        if self.avoid_aircraft and any(
            bad.lower() in (leg.aircraft or "").lower()
            for leg in it.legs for bad in self.avoid_aircraft
        ):
            return False
        if self.avoid_layovers and set(it.layovers) & set(self.avoid_layovers):
            return False
        if self.max_co2_percent is not None:
            if it.co2_percent_vs_typical is None:
                return False
            if it.co2_percent_vs_typical > self.max_co2_percent:
                return False
        return self._time_ok(it)

    def _time_ok(self, it: Itinerary) -> bool:
        """Clock bounds are times of day, local to their own airport.

        `--arrive-before 18:00` means "lands by 6pm", which is how the words
        read and the only reading that works on a long-haul: an earlier version
        resolved the bound against the *departure* date, so every itinerary
        arriving on a later day failed and a Toronto-Kathmandu search reported
        "no flights match" for any bound at all.

        That leaves the red-eye case — a flight landing 00:05 two days out has
        the clock time "00:05" and passes any evening bound. That is correct for
        "lands by 6pm" and wrong for "nothing landing after 11pm tonight", so
        the second intent gets its own flag (`arrive_same_day`) rather than
        being smuggled into this one. All bounds are inclusive.
        """
        depart, arrive = _clock_of(it.depart), _clock_of(it.arrive)
        if self.depart_after and (depart is None or depart < self.depart_after):
            return False
        if self.depart_before and (depart is None or depart > self.depart_before):
            return False
        if self.arrive_before and (arrive is None or arrive > self.arrive_before):
            return False
        if self.arrive_same_day:
            depart_day, arrive_day = it.depart[:10], it.arrive[:10]
            # Fail closed: two unreadable stamps are equal to each other, which
            # would otherwise sneak a broken record past the only bound that
            # compares dates rather than clocks.
            if len(depart_day) < 10 or len(arrive_day) < 10:
                return False
            if arrive_day != depart_day:
                return False
        return True

def _clock_of(stamp: str) -> str | None:
    """The "HH:MM" of a naive ISO timestamp, or None if it is unreadable.

    Returning None rather than "" matters: an unreadable value must fail every
    time bound, not silently satisfy the ones that happen to compare favourably
    against an empty string.
    """
    return stamp[11:16] if len(stamp) >= 16 else None


def _legroom_ok(it, minimum: int) -> bool:
    """True if every leg states at least `minimum` inches of legroom.

    A minimum of zero is no requirement at all, and keeps everything —
    including legs that publish no figure. Above zero, a leg that does not
    state its legroom fails, because assuming it is fine quietly recommends
    the cramped seat the user asked to avoid.
    """
    if minimum <= 0:
        return True
    for leg in it.legs:
        # The FIRST number, not every digit concatenated: a range like
        # "30-31 in" became 3031 and satisfied any minimum — failing in the
        # generous direction, which is the one that books the cramped seat.
        match = re.search(r"\d+", leg.legroom or "")
        if not match or int(match.group()) < minimum:
            return False
    return True


#: Google's own limit on a single booking.
MAX_PASSENGERS = 9


def _validate_party(adults: int, children: int, in_seat: int, on_lap: int) -> None:
    """Reject a party Google cannot price.

    Unvalidated, a zero or negative count emitted *no* passenger field at all,
    so the search silently ran for a different party than the caller asked for.
    The ds:0 echo catches that after the fact, but it costs a 2.5MB request and
    reports a usage mistake as a payload error.
    """
    counts = {
        "--adults": adults, "--children": children,
        "--infants-in-seat": in_seat, "--infants-on-lap": on_lap,
    }
    for flag, count in counts.items():
        if count < 0:
            raise QueryError(f"{flag} cannot be negative (got {count})")
    if adults < 1:
        raise QueryError("at least one adult is required")
    total = adults + children + in_seat + on_lap
    if total > MAX_PASSENGERS:
        raise QueryError(
            f"{total} travellers exceeds the {MAX_PASSENGERS} Google will price "
            f"in one search"
        )
    if on_lap > adults:
        raise QueryError(
            f"{on_lap} lap infants needs at least {on_lap} adults to hold them "
            f"(got {adults})"
        )


def unsatisfiable(filters: Filters, route: RouteFilters | None) -> str | None:
    """Why these filters can never match on this route, or None if they might.

    Google publishes the route's real bounds — cheapest and dearest fare, the
    carriers that appear, the duration range — so a filter that excludes
    everything is knowable *before* answering. Without this the tool returns
    "nothing matched" (exit 1, "keep waiting"), and a watch loop built on an
    impossible threshold polls forever and never fires. `_check_airlines`
    solved that for carriers; this generalises it to the rest.

    Deliberately conservative: only conditions the payload proves are reported,
    so a filter that merely happens to match nothing today still exits 1.
    """
    if route is None:
        return None

    if (filters.max_price is not None and route.price_min is not None
            and filters.max_price < route.price_min):
        return (
            f"no fare on this route is at or under {filters.max_price} "
            f"{route.currency} — the cheapest is {route.price_min}"
        )

    if (filters.max_duration_minutes is not None
            and route.duration_min_minutes is not None
            and filters.max_duration_minutes < route.duration_min_minutes):
        return (
            f"nothing on this route takes {filters.max_duration_minutes} "
            f"minutes or less — the quickest is {route.duration_min_minutes}"
        )

    known = {code.upper() for code, _ in route.airlines}
    if known and filters.airlines:
        # --airlines is a whitelist, i.e. an OR: "Flair or WestJet" is satisfied
        # by either. Only a request where NOTHING asked for flies the route is
        # provably impossible. Refusing because *one* code of several is absent
        # threw away flights the user had correctly asked for. And because
        # `route.airlines` is Google's over-inclusive codeshare chip list, an
        # intersection is the safe direction to test; a subset test is not.
        wanted = set(filters.airlines)
        if not (wanted & known):
            return (
                f"{', '.join(sorted(wanted))} does not fly this route. "
                f"Carriers on it: {', '.join(sorted(known))}"
            )
    if known and filters.exclude_airlines and known <= set(filters.exclude_airlines):
        return "every carrier on this route is excluded"

    if (filters.depart_after and filters.depart_before
            and filters.depart_after > filters.depart_before):
        return (
            f"no departure can be both after {filters.depart_after} and before "
            f"{filters.depart_before}"
        )

    return None


def build_query(
    origin: str,
    destination: str,
    depart: date,
    ret: date | None = None,
    *,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: str = "economy",
    max_stops: int | None = None,
) -> Query:
    """A one-way or round-trip query, validated."""
    validate_route(origin, destination)
    validate_dates(depart, ret)
    _validate_party(adults, children, infants_in_seat, infants_on_lap)
    slices = [Slice(origin, destination, depart, max_stops)]
    if ret is not None:
        slices.append(Slice(destination, origin, ret, max_stops))
    return Query(
        slices=tuple(slices),
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        trip_type=ROUND_TRIP if ret is not None else ONE_WAY,
    )


#: Metro codes and the airports they stand for, for the *only* purpose of
#: catching a route whose endpoints are the same city (YTO -> YYZ). Not used to
#: resolve anything — `airports` does that from Google's own data — so this
#: cannot go stale in a way that produces a wrong answer, only a missed check.
#: IATA metropolitan codes are stable enough for that narrow job.
METRO_AIRPORTS = {
    "YTO": {"YYZ", "YTZ", "YHM"}, "YMQ": {"YUL", "YHU"},
    "NYC": {"JFK", "LGA", "EWR"}, "LON": {"LHR", "LGW", "STN", "LCY", "LTN", "SEN"},
    "PAR": {"CDG", "ORY", "BVA"}, "MIL": {"MXP", "LIN", "BGY"},
    "ROM": {"FCO", "CIA"}, "TYO": {"HND", "NRT"}, "OSA": {"KIX", "ITM"},
    "WAS": {"DCA", "IAD", "BWI"}, "CHI": {"ORD", "MDW"}, "BER": {"BER"},
    "SAO": {"GRU", "CGH", "VCP"}, "RIO": {"GIG", "SDU"},
    "BUE": {"EZE", "AEP"}, "MOW": {"SVO", "DME", "VKO"},
    "STO": {"ARN", "BMA", "NYO"}, "SEL": {"ICN", "GMP"}, "BJS": {"PEK", "PKX"},
}


def _same_city(origin: str, destination: str) -> bool:
    return (
        destination in METRO_AIRPORTS.get(origin, ())
        or origin in METRO_AIRPORTS.get(destination, ())
    )


def validate_route(origin: str, destination: str) -> None:
    for code in (origin, destination):
        if not (len(code) == 3 and code.isalpha()):
            raise QueryError(
                f"{code!r} is not a 3-letter IATA airport code. Pass the code "
                f"itself (Toronto Pearson is YYZ); `route` lists the airports "
                f"and connections Google knows for a route you can already name."
            )
    origin, destination = origin.upper(), destination.upper()
    if origin == destination:
        raise QueryError(f"origin and destination are both {origin}")
    if _same_city(origin, destination):
        raise QueryError(
            f"{origin} and {destination} are the same city — {origin} is a "
            f"metropolitan code covering {destination}. Google returns an "
            f"empty payload for this, which is not the same as 'no flights'."
        )


def validate_dates(depart: date, ret: date | None) -> None:
    """Refuse the queries Google answers with a misleading empty payload.

    This is the repo's "refuse rather than return a false negative" rule. Every
    condition below returns zero itineraries from Google with no error at all,
    which an agent would faithfully report as "no flights available".
    """
    today = date.today()
    if depart < today:
        raise QueryError(
            f"departure {depart} is in the past — Google returns an empty "
            f"result for past dates, which is not the same as 'no flights'."
        )
    horizon = today + timedelta(days=MAX_DAYS_AHEAD)
    if depart > horizon:
        raise QueryError(
            f"departure {depart} is beyond Google's booking horizon "
            f"(about {MAX_DAYS_AHEAD} days, so on or before {horizon}). "
            f"It would return an empty result, not an error."
        )
    if ret is not None:
        if ret < depart:
            raise QueryError(f"return {ret} is before departure {depart}")
        if ret > horizon:
            raise QueryError(
                f"return {ret} is beyond Google's booking horizon ({horizon})"
            )


def _check_echo(query: Query, echo: dict | None) -> None:
    """Verify Google answered the question we asked.

    Cheap insurance against the failure mode this encoding actually has: a
    mis-set field returns a plausible fare for a different trip. A wrong party
    size once made every one-way search quietly price an extra child, and a
    malformed date is silently rewritten to another date entirely. Both show up
    here immediately.
    """
    if echo is None:
        return  # no echo on this page; nothing to cross-check against
    wanted = {
        "adults": query.adults,
        "children": query.children,
        "infants_in_seat": query.infants_in_seat,
        "infants_on_lap": query.infants_on_lap,
    }
    got = {key: echo[key] for key in wanted}
    if got != wanted:
        raise PayloadError(
            f"Google searched for {got} but we asked for {wanted}. The query "
            f"encoding is wrong — these fares are for a different party."
        )
    asked = [s.depart.isoformat() for s in query.slices]
    echoed = echo.get("dates") or []
    if echoed and echoed[: len(asked)] != asked:
        raise PayloadError(
            f"Google searched {echoed} but we asked for {asked}. It substitutes "
            f"a date it can parse rather than rejecting one it cannot, so these "
            f"fares are for different days."
        )


class Client:
    """Fetches and parses Google Flights searches."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        country: str = "CA",
        currency: str = "CAD",
        language: str = "en",
    ):
        self.transport = transport or Transport()
        self.country = country
        self.currency = currency
        self.language = language

    def search(self, query: Query) -> SearchResult:
        """Run one query and return its parsed result."""
        params = {
            "tfs": query.tfs(),
            "hl": self.language,
            "gl": self.country,
            "curr": self.currency,
        }
        html = self.transport.get(SEARCH_PATH, params)
        covers = "round trip" if query.trip_type == ROUND_TRIP else "one way"
        try:
            _check_echo(query, parsed_query(html))
            result = search_result(html, self.currency, covers)
        except PayloadError:
            # Ordering matters: a blocked page also fails to parse, and
            # "Google blocked us" is far more actionable than "layout changed".
            if looks_blocked(html):
                raise FlightsHTTPError(
                    f"Google served {block_reason(html)} instead of results. "
                    f"This is a block, not an empty route — wait a few minutes, "
                    f"and run fewer searches back to back."
                ) from None
            raise
        # Only once the page is known to BE a results page: a non-results page
        # carrying foreign-currency prices would otherwise be reported as a
        # currency mismatch (exit 2) rather than the block it is (exit 3).
        self._check_currency(html)
        return result

    def _check_currency(self, html: str) -> None:
        """Refuse to label a price in a currency Google did not use.

        `--currency` is a request, not a guarantee: an unrecognised code is
        silently ignored and the page comes back priced in the market default.
        The tool used to stamp the requested code onto those numbers, so
        `--currency XYZ` reported Canadian dollars labelled XYZ — a wrong
        price with no error, and the user cannot tell by looking. The page
        states its own currency, so it is checked rather than assumed.
        """
        actual = detected_currency(html)
        if actual and actual != self.currency:
            raise QueryError(
                f"Google priced this search in {actual}, not the {self.currency} "
                f"that was asked for — it ignores a currency it does not "
                f"support rather than reporting an error. Re-run with "
                f"--currency {actual}, or use a currency Google offers."
            )

    def resolve(self, place: str) -> tuple[list[Airport], list[Airport]]:
        """Airports Google routes to for a free-text place name.

        Returns (resolved, also_on_page).

        The phrasing matters more than it looks. ``"<place> flights"`` and
        ``"flights from <place>"`` both make the place a *destination* and fill
        the origin from the viewer's IP — so the old implementation was reading
        the datacenter's own city, and only appeared to work for places near
        it. Asking ``"flights to <place>"`` and reading the *destinations* of
        the itineraries Google returns is independent of where the request came
        from.

        Resolution is structural, never textual. Matching the query string
        against airport names cannot work: Bali's airport is in Denpasar, and
        no amount of substring matching gets there.
        """
        if not place.strip():
            raise QueryError("give a place to look up, e.g. `airports Toronto`")
        html = self.transport.get(
            RESOLVE_PATH,
            {
                "q": f"flights to {place}",
                "hl": self.language,
                "gl": self.country,
                "curr": self.currency,
            },
        )
        try:
            result = search_result(html, self.currency, "one way")
        except PayloadError:
            if looks_blocked(html):
                raise FlightsHTTPError(
                    f"Google served {block_reason(html)} instead of results — "
                    f"this is a block, not an unknown place."
                ) from None
            # A place Google cannot resolve comes back as a results block with
            # nothing in it. That is a usage problem (exit 2), and it must not
            # surface as the payload error the shared guard raises — whose
            # message blames the tfs encoding, which resolution does not use.
            raise QueryError(
                f"Google could not resolve {place!r} to anywhere it flies. "
                f"Check the spelling, try a nearby larger city, or pass the "
                f"IATA code directly if you know it."
            ) from None

        # The destinations Google actually routed to ARE the resolved place.
        codes = {i.destination for i in result.itineraries if i.destination}
        by_code = {a.code: a for a in result.airports}
        # A destination Google routed to but did not describe still answers the
        # question — the code is the answer. Synthesising a bare Airport beats
        # dropping it and reporting "no flights to X" when flights came back.
        resolved = [
            by_code.get(code) or Airport(code, code, None, None, None, None)
            for code in sorted(codes)
        ]
        others = [a for a in result.airports if a.code not in codes]
        return resolved, others

    def sweep(
        self, query: Query, days: int, *, step: int = 1
    ) -> "Iterator[tuple[date, SearchResult]]":
        """Run the same trip across a window of departure dates.

        One request per date — Google has no bulk endpoint — so the budget is
        checked before the first request rather than discovered at the last.
        """
        if days < 1:
            raise QueryError("--days must be at least 1")
        if step < 1:
            raise QueryError(
                f"--step must be at least 1 (got {step}). A zero or negative "
                f"stride produces an empty sweep, which would be reported as "
                f"'nothing available' rather than as the mistake it is."
            )
        if days > MAX_SWEEP_DAYS:
            raise QueryError(
                f"--days {days} exceeds the {MAX_SWEEP_DAYS}-day sweep limit. "
                f"Each day is a separate ~2.5MB request."
            )
        offsets = list(range(0, days, step))
        # A window wider than the hard request ceiling is only reachable by
        # sampling it. Saying so here beats failing with advice the caller
        # cannot follow: --max-requests cannot be raised past that ceiling.
        if len(offsets) > MAX_MAX_REQUESTS:
            needed = -(-days // MAX_MAX_REQUESTS)  # ceil
            raise QueryError(
                f"a {days}-day window at --step {step} needs {len(offsets)} "
                f"requests, above the hard ceiling of {MAX_MAX_REQUESTS}. Use "
                f"--step {needed} or more to sample that window, or narrow "
                f"--days."
            )
        self.transport.plan(len(offsets), f"a {days}-day sweep")

        # Dates past the booking horizon are refused up front rather than
        # skipped silently. Dropping them made the results array quietly cover
        # a shorter window than --days asked for, and a window entirely past
        # the horizon reported "nothing available" (exit 1) where `search`
        # would correctly call it a usage error.
        for offset in offsets:
            shifted = _shift(query, offset)
            validate_dates(shifted.slices[0].depart, _return_date(shifted))

        # A generator, so a caller that rejects the first result — an
        # impossible filter, say — stops after one request rather than paying
        # for the whole window first. Validation above stays eager because it
        # runs before the first yield is requested.
        def run() -> "Iterator[tuple[date, SearchResult]]":
            for offset in offsets:
                shifted = _shift(query, offset)
                yield shifted.slices[0].depart, self.search(shifted)

        return run()


def _return_date(query: Query) -> date | None:
    return query.slices[1].depart if len(query.slices) > 1 else None


def _shift(query: Query, days: int) -> Query:
    """The same trip, every date moved by `days`, trip length preserved."""
    if not days:
        return query
    delta = timedelta(days=days)
    slices = tuple(
        replace(s, depart=s.depart + delta) for s in query.slices
    )
    return replace(query, slices=slices)
