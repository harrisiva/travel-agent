"""The KAYAK affiliate client: search orchestration, not transport.

`http.Transport` makes one call and knows nothing about what it means. This
module knows what a call means and therefore what to do when it fails — a poll
can be repeated against the same `searchId`, a start call cannot, and a 401 is
not worth repeating at all.

Three rules here are load-bearing, and each one exists because the obvious
implementation is quietly wrong:

* **Poll until `status` is `complete`.** The API answers in phases:
  `first-phase` results are merely *first*, not cheapest. Reporting the top of
  a first-phase response as "the cheapest" is a confident wrong answer, which
  is the failure mode CLAUDE.md tells us to hunt.
* **Always send `pageSize` and `priceMode` explicitly.** The request defaults
  to 50 rows while responses document 500, and every interesting filter runs
  client-side. Filtering "cheapest SUV with unlimited mileage" out of a
  price-sorted 50 returns a confident false "none available".
* **Refuse a fan-out we cannot afford, before issuing it.** The sandbox allows
  250 car requests an hour — 0.069/second sustained. A blocking token bucket
  would park the CLI for fourteen seconds at a time inside an agent's bash
  timeout. Naming the numbers and exiting is kinder and more debuggable.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Sequence

from .auth import mint_user_track_id
from .cache import Cache, HOURLY_LIMITS, key_for
from .errors import (
    AuthError,
    BudgetError,
    RateLimited,
    SearchTimeout,
    TransportError,
    UsageError,
)
from .http import Transport
from .model import (
    CalendarDay,
    CalendarSearch,
    CarSearch,
    Hotel,
    HotelSearch,
    Maps,
    Offer,
    Place,
)

#: The sandbox host. Production is a different tenant and a different key; the
#: CLI decides which by `--base-url` / KAYAK_BASE_URL, and everything
#: downstream reads `Client.sandbox` rather than re-parsing the host, so the
#: honesty banner cannot disagree with the host actually queried.
SANDBOX_HOST = "sandbox-en-us.kayakaffiliates.com"

#: Paths. `apiKey` rides in the query string on every one of them.
CARS_POLL = "/i/api/affiliate/search/car/v1/poll"
AUTOCOMPLETE = "/api/affiliate/autocomplete/v1/{vertical}"
HOTELS = "/api/3.0/hotels"
HOTEL = "/api/3.0/hotel"
CALENDAR = "/i/api/affiliate/priceInsights/flights/v1/calendar"

#: Sleep before each successive poll. Front-loaded because most searches are
#: done inside ten seconds, then flattened so a slow one does not spin.
POLL_SLEEPS: tuple[float, ...] = (1.0, 1.0, 1.5, 1.5, 2.0, 2.5, 3.0, 4.0)
POLL_SLEEP_MAX = 4.0

#: Wall-clock ceiling. 25s is chosen for claude.ai, where a bash call that runs
#: much longer risks being cut off with nothing to show; Claude Code can afford
#: `--max-poll-seconds 90`. Documented in SKILL.md, because the right value is
#: a property of the environment rather than of the search.
DEFAULT_MAX_POLL_SECONDS = 25.0

#: Once the major providers are in, a search that stops growing is done in
#: practice. Two consecutive polls with an unchanged `totalCount`, at least
#: this long after second-phase began, is a real signal rather than a guess —
#: and typically saves ten to twenty seconds per search.
SECOND_PHASE_SETTLE_SECONDS = 3.0

STATUS_FIRST, STATUS_SECOND, STATUS_COMPLETE = "first-phase", "second-phase", "complete"

#: HTTP 202 from the hotels endpoints means "still searching, ask again" — not
#: an error, and emphatically not an empty result set. It falls inside the
#: usual 2xx success band, so it has to be named explicitly or a partial search
#: is read as a finished one.
HTTP_ACCEPTED = 202

#: `rooms` is required by /hotel whenever dates are supplied, and the spec's
#: own examples all pass a value. "2" is one room for two adults — the shape
#: is {adults}:{child ages}|{adults}:{child ages}.
DEFAULT_ROOMS = "2"

#: Page bounds for /hotels and /hotels/basic, from the RAML's `pageSize`
#: (default 25, minimum 1, maximum 250). We ask for the maximum by default and
#: send it unconditionally — see `hotels()` for why the default is a trap.
HOTELS_PAGE_MIN, HOTELS_PAGE_MAX = 1, 250
DEFAULT_HOTELS_PAGE_SIZE = HOTELS_PAGE_MAX

#: Maximum span, in whole months INCLUSIVE, that `/calendar` accepts for each
#: aggregation. From CalendarRequest.dateTo in the price-insights RAML. Over
#: the limit the API answers 400, so the CLI refuses first and says which
#: number it broke.
CALENDAR_MAX_MONTHS: dict[str, int] = {"day": 2, "month": 12}

#: Statuses that satisfy `--until`, in increasing order of certainty.
PHASE_RANK = {STATUS_FIRST: 0, STATUS_SECOND: 1, STATUS_COMPLETE: 2}

#: Retry policy. A poll may be repeated against the same searchId for free; a
#: start call may not, so it gets fewer attempts.
POLL_RETRIES, START_RETRIES = 3, 2
RETRY_SLEEPS = (1.0, 2.0, 4.0)

#: A 429 against a fixed hourly quota is not worth a backoff loop — retrying
#: only spends the quota faster. Honour a short Retry-After once, then stop.
MAX_RETRY_AFTER = 10.0


def ceiling_polls(max_seconds: float) -> int:
    """Worst-case requests for one search, given the sleep curve.

    Used to price a sweep *before* issuing it. Deliberately the ceiling and not
    an average: the budget must cover the bad case, or the refusal is a lie.

    Public because `cli.py` quotes the same number back to the user in its
    refusal message. A second copy of this arithmetic in the CLI would drift
    from the one the budget check actually enforces, and a refusal that says
    "60 requests" while the client plans 84 is worse than no refusal at all —
    so there is exactly one formula and both callers read it.
    """
    total, count = 0.0, 1          # the start call is request #1
    while total < max_seconds:
        total += POLL_SLEEPS[min(count - 1, len(POLL_SLEEPS) - 1)] if count <= len(POLL_SLEEPS) else POLL_SLEEP_MAX
        count += 1
    return count


#: Kept so an existing `from .client import _ceiling_polls` keeps working.
_ceiling_polls = ceiling_polls

#: How many of the cheapest days a sweep re-polls to `complete`. Named rather
#: than left as a bare literal because `cli.py` quotes the sweep's worst-case
#: cost back to the user, and that arithmetic multiplies by this number: two
#: places holding "2" independently is how a refusal message starts lying.
#: Import it rather than restating it.
SWEEP_CONFIRM_TOP = 2


class Client:
    """One process-wide client. Thread-safe; one `userTrackId` for its life.

    `userTrackId` identifies an end user *for a session*, and the API's own
    guidance is explicit that a constant value, or one rotated per request,
    reads as id churn and gets rate-limited. A sweep is one user asking one
    question, so every thread shares the id minted here.
    """

    def __init__(
        self,
        api_key: str,
        host: str = SANDBOX_HOST,
        cache: Cache | None = None,
        max_poll_seconds: float = DEFAULT_MAX_POLL_SECONDS,
        client_ip: str = "127.0.0.1",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: Transport | None = None,
    ):
        self.api_key = api_key
        self.host = host
        self.cache = cache or Cache()
        self.max_poll_seconds = max_poll_seconds
        self.user_track_id = mint_user_track_id()
        self.transport = transport or Transport(host, client_ip=client_ip)
        self._sleep = sleep
        #: Injectable so the poll deadline and the second-phase settle rule are
        #: testable without real waiting. A test supplies a clock its fake
        #: sleep advances; otherwise both would run in microseconds and the
        #: settle rule — which is defined in seconds — could never fire.
        self._clock = clock
        #: Set on the first 401 so parallel searches abandon immediately
        #: instead of each independently discovering the key is dead.
        self._auth_failed = threading.Event()

    # ---------------------------------------------------------------- basics

    @property
    def sandbox(self) -> bool:
        """True when pointed at the sandbox, where prices are mock data."""
        return SANDBOX_HOST in self.host

    @property
    def requests_made(self) -> int:
        return self.transport.requests_made

    def _params(self, **extra: Any) -> dict:
        params = {"apiKey": self.api_key}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _call(self, method: str, path: str, family: str,
              params: dict | None = None, body: Any = None,
              headers: dict | None = None):
        """One call, with the shared failure interpretation applied.

        Raises rather than returning an error response, so no caller can
        accidentally treat a 401 body as an empty result set.
        """
        if self._auth_failed.is_set():
            raise AuthError("API key was rejected earlier in this run")

        # Recorded in `finally`, not after a success: a request that raised
        # still left the tenant, still counts against the hourly quota, and a
        # ledger that only counts successes under-counts exactly when things
        # are going wrong and we most need the ceiling to hold.
        try:
            response = self.transport.request(method, path, params=params,
                                              body=body, headers=headers)
        finally:
            self.cache.record_requests(family)

        if response.status in (401, 403) or response.error_code() in (
            "INVALID_API_KEY", "EXPIRED_API_KEY", "UNAUTHORIZED",
        ):
            self._auth_failed.set()
            raise AuthError(
                f"KAYAK rejected the API key ({response.error_message() or response.status}). "
                "Sandbox keys expire three months after they are issued — if this key "
                "used to work, request a new one. This is NOT 'no cars available'."
            )
        if response.status == 429:
            raise RateLimited(
                f"KAYAK rate-limited this key. The sandbox allows "
                f"{HOURLY_LIMITS.get(family, '?')} {family} requests an hour.",
                retry_after=response.retry_after(),
            )
        if not response.ok:
            raise TransportError(
                f"{method} {path} failed: {response.status} "
                f"{response.error_message() or 'no detail'}"
            )
        return response

    def _budget(self, family: str, planned: int, max_requests: int) -> None:
        """Refuse an unaffordable plan before the first call goes out."""
        if planned > max_requests:
            raise BudgetError(
                f"this would take up to {planned} requests, over the "
                f"--max-requests ceiling of {max_requests}. Narrow the date "
                f"range, or raise the ceiling deliberately."
            )
        self._budget_hourly(family, planned)

    def _budget_hourly(self, family: str, planned: int) -> None:
        """The hourly-quota half of the check, usable on its own.

        A single search has no `--max-requests` ceiling to test against, but it
        can still spend a dozen polls — enough to walk off the end of a quota
        that a sweep two minutes ago in another process already half spent. The
        quota check is therefore separated from the fan-out ceiling so that
        every search consults the ledger, not only the ones that fan out.
        """
        limit = HOURLY_LIMITS.get(family)
        if limit:
            used = self.cache.recent_requests(family)
            if used + planned > limit:
                raise BudgetError(
                    f"this would take up to {planned} {family} requests and "
                    f"{used} of the {limit}/hour quota are already spent. "
                    f"Wait, or narrow the range."
                )

    # ------------------------------------------------------------ autocomplete

    def places(self, term: str, vertical: str = "cars",
               refresh: bool = False) -> list[Place]:
        """Resolve a name to the ids the search endpoints accept.

        Cached for days: an airport code does not move. The cache key is
        namespaced by the key fingerprint so two API keys never share entries.

        `refresh=True` skips the cache *read* — the fresh answer is still
        written back. `check` and `login` pass it, because their entire job is
        to prove the key works right now: served from cache, a second `check`
        makes no request at all and would report "key OK" for a key that
        expired an hour ago. That is the false negative rule pointed the other
        way — a false *positive* about credentials is just as dishonest.
        """
        term = (term or "").strip()
        if not term:
            raise UsageError("search term is empty")
        if vertical not in ("cars", "flights", "hotels"):
            raise UsageError(f"unknown vertical {vertical!r}")

        cache_key = key_for("places", vertical, term.lower(), self._key_fingerprint())
        cached = None if refresh else self.cache.get(cache_key)
        if cached is None:
            response = self._call(
                "GET", AUTOCOMPLETE.format(vertical=vertical), "autocomplete",
                params=self._params(searchTerm=term),
            )
            cached = response.body
            self.cache.set(cache_key, cached)
        return Place.parse_many(cached)

    def _key_fingerprint(self) -> str:
        """A derived fingerprint — never a substring of the key itself.

        `check` output lands in chat transcripts and bug reports, so no part of
        the key may be reconstructible from anything we print or store.
        """
        import hashlib

        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:8]

    # -------------------------------------------------------------- car search

    def _start_body(self, pickup: str, pickup_type: str, pickup_date: date,
                    drop_date: date, pickup_time: tuple[int, int],
                    drop_time: tuple[int, int], dropoff: str | None,
                    dropoff_type: str, per_day: bool, currency: str | None,
                    sort: str) -> dict:
        """The start payload.

        `priceMode` and `pageSize` are set here unconditionally rather than
        defaulted in argparse: an omitted field silently inherits KAYAK's
        `perDayTotal` and a 50-row page, and both of those are wrong answers
        rather than merely different ones.
        """
        start: dict[str, Any] = {
            "pickup": {
                "location": {"type": pickup_type, "value": pickup},
                "date": pickup_date.isoformat(),
                "hour": pickup_time[0],
                "minute": pickup_time[1],
            },
            "dropoff": {
                "date": drop_date.isoformat(),
                "hour": drop_time[0],
                "minute": drop_time[1],
            },
        }
        if dropoff:
            start["dropoff"]["location"] = {"type": dropoff_type, "value": dropoff}
        return {
            "searchStartParameters": start,
            # `resultParameters` — the name SearchRequest actually declares.
            # An unrecognised key is dropped silently rather than rejected, so
            # a misspelling here does not fail: it just serves KAYAK's
            # defaults (50 rows, perDayTotal) while the code believes it asked
            # for 500 and totals. Every filter in this CLI runs client-side,
            # so a silent page of 50 is a confident "none available".
            "resultParameters": {
                "priceMode": "perDayTotal" if per_day else "total",
                "pageSize": 500,
                "sort": {"key": sort},
                **({"currency": currency} if currency else {}),
            },
        }

    def search_cars(
        self,
        pickup: str,
        pickup_date: date,
        drop_date: date,
        *,
        pickup_type: str = "airport",
        dropoff: str | None = None,
        dropoff_type: str = "airport",
        pickup_time: tuple[int, int] = (10, 0),
        drop_time: tuple[int, int] = (10, 0),
        per_day: bool = False,
        currency: str | None = None,
        sort: str = "price",
        until: str = STATUS_COMPLETE,
        max_poll_seconds: float | None = None,
    ) -> CarSearch:
        """Start a car search and poll it to `until`.

        Raises SearchTimeout (exit 5) carrying whatever was found if the wall
        clock runs out first. A partial answer is a legitimate thing to show a
        user; calling it complete is not.
        """
        if drop_date < pickup_date:
            raise UsageError("drop-off date is before pick-up date")
        wanted = PHASE_RANK.get(until)
        if wanted is None:
            raise UsageError(f"--until must be one of {', '.join(PHASE_RANK)}")

        # One search is a start call plus up to a dozen polls. Only sweeps used
        # to consult the ledger, so a bare `cars` could spend a dozen requests
        # of a quota another process had already all but exhausted and find out
        # by 429. There is no --max-requests ceiling to test against here, so
        # only the hourly half of the check applies.
        budget_seconds = max_poll_seconds or self.max_poll_seconds
        self._budget_hourly("cars", ceiling_polls(budget_seconds))

        deadline = self._clock() + budget_seconds
        body = self._start_body(pickup, pickup_type, pickup_date, drop_date,
                                pickup_time, drop_time, dropoff, dropoff_type,
                                per_day, currency, sort)
        requested_mode = body["resultParameters"]["priceMode"]

        raw = self._retrying(
            START_RETRIES,
            lambda: self._call("POST", CARS_POLL, "cars",
                               params=self._params(userTrackId=self.user_track_id),
                               body=body).body,
        )
        search = CarSearch.parse(raw)
        self._assert_price_mode(search, requested_mode)

        search = self._poll_until(search, wanted, deadline, requested_mode)
        if PHASE_RANK.get(search.status, 0) < wanted:
            raise SearchTimeout(
                f"search was still {search.status or 'running'} after "
                f"{max_poll_seconds or self.max_poll_seconds:.0f}s — these results "
                f"are partial, not the final cheapest",
                partial=search,
                status=search.status,
            )
        return search

    def _assert_price_mode(self, search: CarSearch, requested: str) -> None:
        """Refuse to label a price in a mode we did not get.

        If the API prices per-day when we asked for a total, every downstream
        number is off by the rental length. Raising is the repo's rule:
        refuse rather than return a plausible wrong answer.
        """
        got = search.price_mode
        if got and got != requested:
            raise TransportError(
                f"asked for priceMode {requested!r} but the response is "
                f"{got!r}; refusing to label these prices"
            )

    def _poll_until(self, search: CarSearch, wanted: int, deadline: float,
                    requested_mode: str) -> CarSearch:
        """Poll to `wanted`, early-settling only when `wanted` is second-phase.

        The settle rule serves sweeps and nothing else. Ranking days against
        each other is a relative question, so a second-phase result that has
        stopped growing answers it, and stopping there is most of the saving.

        Applying it to a caller who asked for `complete` was actively harmful:
        the loop broke at second-phase, the phase check in `search_cars` then
        failed, and the plain `cars` command raised SearchTimeout — exit 5 on
        the happy path, and the word "cheapest" permanently out of reach. So
        the rule is scoped to the request that wants it. Ask for complete and
        you poll to complete or to the deadline; there is no third answer.
        """
        settle_ok = wanted == PHASE_RANK[STATUS_SECOND]
        polls = 0
        settled_at: float | None = None
        last_total = search.total_count

        while PHASE_RANK.get(search.status, 0) < wanted:
            if self._clock() >= deadline:
                break
            self._sleep(POLL_SLEEPS[min(polls, len(POLL_SLEEPS) - 1)]
                        if polls < len(POLL_SLEEPS) else POLL_SLEEP_MAX)
            polls += 1

            raw = self._retrying(
                POLL_RETRIES,
                lambda: self._call(
                    "POST", CARS_POLL, "cars",
                    params=self._params(userTrackId=self.user_track_id,
                                        cluster=search.cluster),
                    body={"searchId": search.search_id},
                ).body,
            )
            search = CarSearch.parse(raw)
            self._assert_price_mode(search, requested_mode)

            if settle_ok and search.status == STATUS_SECOND:
                now = self._clock()
                if settled_at is None:
                    settled_at = now
                    last_total = search.total_count
                elif (search.total_count == last_total
                      and now - settled_at >= SECOND_PHASE_SETTLE_SECONDS):
                    # Stopped growing after the important providers reported.
                    break
                else:
                    last_total = search.total_count
        return search

    def _retrying(self, attempts: int, call: Callable[[], Any]) -> Any:
        """Retry transport failures only. Auth and budget errors go straight up."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return call()
            except RateLimited as exc:
                wait = exc.retry_after
                if attempt == 0 and wait is not None and wait <= MAX_RETRY_AFTER:
                    self._sleep(wait)
                    last = exc
                    continue
                raise
            except AuthError:
                raise
            except TransportError as exc:
                last = exc
                if attempt < attempts - 1:
                    self._sleep(RETRY_SLEEPS[min(attempt, len(RETRY_SLEEPS) - 1)])
        raise last if last else TransportError("request failed")

    # ------------------------------------------------------------------ sweep

    def sweep_cars(
        self,
        pickup: str,
        first_day: date,
        last_day: date,
        nights: int,
        *,
        max_requests: int = 40,
        confirm_top: int = SWEEP_CONFIRM_TOP,
        **search_kwargs: Any,
    ) -> list[tuple[date, CarSearch | None, Exception | None]]:
        """Cheapest pickup day across a range, in two stages.

        Stage one scans every candidate day only as far as `second-phase`:
        ranking days against each other is a *relative* question, and the
        major providers are enough to answer it. Stage two re-polls only the
        best few to `complete`, because the day we actually recommend is the
        one whose price has to be exact. That halves the cost of a sweep
        against a 250/hour budget.
        """
        if last_day < first_day:
            raise UsageError("sweep end date is before its start date")
        if nights < 1:
            raise UsageError("--nights must be at least 1")

        days = [first_day + timedelta(days=n)
                for n in range((last_day - first_day).days + 1)]
        # Price the plan against the deadline the searches will actually run
        # under. `search_cars` takes a `max_poll_seconds` override, and it
        # arrives here inside `search_kwargs`; costing the sweep at the client
        # default while every search polls to a longer one is the same lie as
        # a wrong confirm_top — the budget would clear at 12 polls a search
        # and then spend 24.
        per_search = ceiling_polls(
            search_kwargs.get("max_poll_seconds") or self.max_poll_seconds
        )
        self._budget("cars", len(days) * per_search + confirm_top * per_search,
                     max_requests)

        results: dict[date, tuple[CarSearch | None, Exception | None]] = {}

        def scan(day: date):
            return day, self.search_cars(
                pickup, day, day + timedelta(days=nights),
                until=STATUS_SECOND, **search_kwargs,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(scan, day): day for day in days}
            for future in as_completed(futures):
                day = futures[future]
                try:
                    _, search = future.result()
                    results[day] = (search, None)
                except SearchTimeout as exc:
                    results[day] = (exc.partial, exc)
                except AuthError:
                    for pending in futures:
                        pending.cancel()
                    raise
                except (TransportError, UsageError) as exc:
                    results[day] = (None, exc)

        ranked = sorted(
            (d for d, (s, _) in results.items() if s and s.offers),
            key=lambda d: results[d][0].cheapest_key(),
        )
        for day in ranked[:confirm_top]:
            try:
                results[day] = (
                    self.search_cars(pickup, day, day + timedelta(days=nights),
                                     until=STATUS_COMPLETE, **search_kwargs),
                    None,
                )
            except SearchTimeout as exc:
                results[day] = (exc.partial, exc)
            except (TransportError, UsageError) as exc:
                keep, _ = results[day]
                results[day] = (keep, exc)

        return [(day, results.get(day, (None, None))[0],
                 results.get(day, (None, None))[1]) for day in days]

    # ----------------------------------------------------------------- hotels

    def hotels(
        self,
        destination: str,
        checkin: date | None = None,
        checkout: date | None = None,
        *,
        hotel_id: str | None = None,
        page_size: int = DEFAULT_HOTELS_PAGE_SIZE,
        only_if_complete: bool = False,
        max_poll_seconds: float | None = None,
    ) -> HotelSearch:
        """Search hotels in a destination, or one hotel by its own key.

        `destination` is an **EntityKey**, not a place id: `"kplace:58075"`,
        `"khotel:2589314"`, or `"klatlon:48.86,2.34"`. `places --vertical
        hotels` returns it as `entity_key`. Passing a bare integer place id
        here is a 400, not a fallback.

        Completion is the reason this returns a `HotelSearch` rather than a
        list. `/hotels` answers with whatever providers have reported so far
        and marks the response `isComplete`. With `only_if_complete=True` the
        server instead answers **202 Accepted** until it has finished and the
        client repeats the request — and 202 sits inside the usual 2xx band,
        so a naive `response.ok` reads "still searching" as "here are your
        results". Both halves of that were live bugs: partial results were
        presented as final, which is the one thing this skill exists not to do.

        `pageSize` is sent unconditionally, for exactly the reason cars sends
        `pageSize: 500` unconditionally. The server default is **25 rows**,
        every interesting narrowing in this CLI runs client-side, and 25 rows
        of a popularity-sorted city is not a sample you can honestly answer
        "nothing under $200" from. An omitted field inherits the default in
        silence — there is no error to notice — so the field is set in the
        request builder and not defaulted anywhere a code path could skip it.
        Do not "simplify" this away: it is the hotels half of the same bug
        that had cars filtering a 50-row page and reporting none available.

        `/hotel` (single) has no `pageSize` — it returns one hotel — so the
        parameter is sent only on the destination search.

        Raises SearchTimeout (exit 5) carrying the partial `HotelSearch` if
        the deadline passes while the search is still incomplete — the same
        contract, and the same exit code, as `search_cars`.
        """
        if not HOTELS_PAGE_MIN <= page_size <= HOTELS_PAGE_MAX:
            raise UsageError(
                f"--page-size must be between {HOTELS_PAGE_MIN} and "
                f"{HOTELS_PAGE_MAX}, got {page_size}"
            )
        if (checkin is None) != (checkout is None):
            raise UsageError(
                "give both --checkin and --checkout, or neither. Without "
                "dates the API lists the destination's hotels with no rates."
            )
        if checkin and checkout and checkout <= checkin:
            raise UsageError("check-out must be after check-in")

        single = bool(hotel_id)
        path = HOTEL if single else HOTELS
        if not single and not (destination or "").strip():
            raise UsageError("a destination entity key is required")

        params = self._params(
            userTrackId=self.user_track_id,
            # /hotel takes the hotel's own key and returns exactly one hotel,
            # so pageSize is meaningless there and the RAML does not declare
            # it. /hotels takes a destination and pages.
            **({"hotel": hotel_id} if single
               else {"destination": destination, "pageSize": page_size}),
            checkin=checkin.isoformat() if checkin else None,
            checkout=checkout.isoformat() if checkout else None,
            # Required by /hotel whenever dates are given, and harmless
            # otherwise; without dates there is nothing to price.
            rooms=DEFAULT_ROOMS if checkin else None,
            onlyIfComplete="true" if only_if_complete else None,
        )

        budget_seconds = max_poll_seconds or self.max_poll_seconds
        polls_allowed = ceiling_polls(budget_seconds) if only_if_complete else 1
        self._budget_hourly("hotels", polls_allowed)
        deadline = self._clock() + budget_seconds

        polls = 0
        while True:
            response = self._retrying(
                POLL_RETRIES,
                lambda: self._call("GET", path, "hotels", params=params),
            )
            search = HotelSearch.parse(response.body, single=single)

            if not only_if_complete:
                # Partial is what was asked for. `complete` carries the truth
                # of it, and the caller is responsible for saying so.
                return search
            if response.status != HTTP_ACCEPTED:
                if search.complete or not _has_complete_flag(search):
                    # A 200 under onlyIfComplete=true *is* the finished
                    # signal, so an absent flag is trusted here — but an
                    # explicit `isComplete: false` is not overridden.
                    return _completed(search)
            if self._clock() >= deadline:
                raise SearchTimeout(
                    f"hotel search was still running after {budget_seconds:.0f}s "
                    f"— these {len(search.hotels)} results are partial, not the "
                    f"final cheapest",
                    partial=search,
                    status="incomplete",
                )
            self._sleep(POLL_SLEEPS[min(polls, len(POLL_SLEEPS) - 1)]
                        if polls < len(POLL_SLEEPS) else POLL_SLEEP_MAX)
            polls += 1

    # --------------------------------------------------------------- calendar

    def calendar(
        self,
        origin: str,
        destination: str,
        date_from: str,
        date_to: str,
        *,
        aggregation: str = "day",
        round_trip: bool = False,
        non_stop: bool = False,
        currency: str | None = None,
        exclude_predictions: bool = False,
    ) -> CalendarSearch:
        """Cheapest-by-date for a route, from cached and predicted fares.

        One call, no polling — this endpoint reads prices travellers have
        already seen rather than starting a live search.

        `origin` and `destination` are whatever the user typed. An all-digit
        string is a `placeId`; anything else is an IATA code. That is the
        `PlaceRequest` shape the spec requires, and it is a real distinction:
        a metro area has a place id and no airport code, so an agent that
        guessed `iataCode` for "London" would get UNRECOGNIZED_LOCATION.

        Both `dateFrom` and `dateTo` are required `YYYY-MM` months, and the
        span is capped — two months for day aggregation, twelve for month.
        Over the cap the API answers 400, so it is refused here with the
        number named, rather than surfacing as an opaque failure.
        """
        if aggregation not in CALENDAR_MAX_MONTHS:
            raise UsageError(
                f"--aggregation must be one of {', '.join(CALENDAR_MAX_MONTHS)}"
            )
        start = _year_month(date_from, "--from")
        end = _year_month(date_to, "--to")
        if end < start:
            raise UsageError(f"--to ({date_to}) is before --from ({date_from})")

        span = (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1
        allowed = CALENDAR_MAX_MONTHS[aggregation]
        if span > allowed:
            raise UsageError(
                f"{date_from}..{date_to} spans {span} months; {aggregation} "
                f"aggregation allows at most {allowed}. Narrow the range, or "
                f"use --aggregation month for a wider view."
            )

        self._budget_hourly("priceinsights", 1)

        body: dict[str, Any] = {
            "origin": _place_request(origin, "origin"),
            "destination": _place_request(destination, "destination"),
            "dateFrom": date_from,
            "dateTo": date_to,
            "aggregationType": aggregation,
            "roundTrip": round_trip,
            "noStops": non_stop,
            "excludePredictions": exclude_predictions,
        }
        if currency:
            body["currencyCode"] = currency.upper()

        response = self._call(
            "POST", CALENDAR, "priceinsights",
            params=self._params(userTrackId=self.user_track_id),
            body=body,
        )
        return CalendarSearch.parse(response.body)


def _has_complete_flag(search: HotelSearch) -> bool:
    """True when the response actually carried a completion flag.

    The spec's types call it `isComplete` and one paragraph of its prose calls
    it `isCompleted`; both are accepted. Distinguishing "said false" from
    "said nothing" matters only under `onlyIfComplete`, where the HTTP status
    is the authoritative signal and the flag is corroboration.
    """
    return any(isinstance(search.raw.get(k), bool)
               for k in ("isComplete", "isCompleted"))


def _completed(search: HotelSearch) -> HotelSearch:
    """`search` with `complete` forced true. Used only on a 200 under
    `onlyIfComplete`, where the status code is the completion signal."""
    if search.complete:
        return search
    return replace(search, complete=True)


def _place_request(value: str, label: str) -> dict:
    """A `PlaceRequest`: `{"placeId": int}` or `{"iataCode": "JFK"}`.

    All-digit input is a place id — the spec types it as an integer, and a
    quoted "175312" is a validation error rather than a coercion.
    """
    text = (value or "").strip()
    if not text:
        raise UsageError(f"{label} is empty")
    if text.isdigit():
        return {"placeId": int(text)}
    return {"iataCode": text.upper()}


def _year_month(value: str, label: str) -> tuple[int, int]:
    """Validate a `YYYY-MM` month and return it as (year, month).

    Refused here rather than sent: the endpoint answers 400 INVALID_DATES for
    a malformed month, and "the API said no" is a worse message than naming
    the flag that was wrong.
    """
    text = (value or "").strip()
    try:
        year_text, month_text = text.split("-")
        year, month = int(year_text), int(month_text)
    except (ValueError, AttributeError):
        raise UsageError(f"{label} must be a YYYY-MM month, got {value!r}") from None
    # Zero-padding is part of the format, not cosmetic: the spec's year-month
    # pattern is "^[0-9]{4}-(0[1-9]|1[0-2])$", so "2027-3" is a 400.
    if len(year_text) != 4 or len(month_text) != 2 or not 1 <= month <= 12:
        raise UsageError(f"{label} must be a YYYY-MM month, got {value!r}")
    return year, month
