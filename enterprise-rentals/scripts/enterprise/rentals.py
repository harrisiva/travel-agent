"""Pricing client: one POST answers almost everything.

A single `reservations/initiate` call returns the whole fleet - every class,
priced, with capacity and drivetrain - in roughly 700 KB. So `quote` issues one
request, and filtering, ranking and points-vs-cash are all local work on the
result. The fan-out commands (`sweep`, `compare`) are that same call repeated
across dates or branches.

Fan-out is deliberately bounded. Six concurrent requests complete in about the
time one takes, but the host's real rate limit was never probed, so: a small
pool, a ceiling refused up front, and a circuit breaker that aborts the whole
run after two consecutive blocks rather than earning 26 more strikes.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Sequence

from .errors import BotBlocked, RequestCeiling, TransportError, UsageError
from .http import USER_AGENT, Transport
from .model import Quote, QuoteRequest, parse_quote

ENDPOINT = "https://prd-east.webapi.enterprise.ca/enterprise-ewt/reservations/initiate"

#: Measured: 6 concurrent complete in ~3.4s against ~2.8s for one. Past this
#: there is nothing to gain against server-side latency, and it starts to look
#: like abuse.
MAX_WORKERS = 6

#: Consecutive blocks that abort a fan-out. A 403 means fingerprinting noticed
#: us; continuing is the worst possible response.
BREAKER_TRIP = 2


def _headers(locale: str, brand: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.enterprise.ca",
        "Referer": "https://www.enterprise.ca/",
        "brand": brand,
        "locale": locale,
        "channel": "WEB",
        "User-Agent": USER_AGENT,
    }


@dataclass(frozen=True)
class Plan:
    """What a fan-out intends to do, shown before anything is sent."""

    requests: int
    label: str

    @property
    def megabytes(self) -> float:
        return round(self.requests * 0.7, 1)

    @property
    def seconds(self) -> int:
        batches = -(-self.requests // MAX_WORKERS)
        return max(3, batches * 4)

    def describe(self) -> str:
        return (
            f"{self.label} = {self.requests} request(s), "
            f"~{self.megabytes} MB, ~{self.seconds}s"
        )

    def check(self, ceiling: int) -> None:
        if self.requests > ceiling:
            raise RequestCeiling(
                f"{self.describe()} exceeds --max-requests {ceiling}. "
                f"Narrow the range or raise the ceiling deliberately."
            )


class RentalClient:
    def __init__(
        self,
        transport: Transport,
        *,
        locale: str = "en_CA",
        brand: str = "ENTERPRISE",
    ) -> None:
        self.transport = transport
        self.headers = _headers(locale, brand)
        self._blocks = 0
        self._lock = threading.Lock()

    def quote(self, request: QuoteRequest) -> Quote:
        """One priced fleet for one (branch, dates, age) tuple."""
        self._guard()
        try:
            raw = self.transport.post_json(ENDPOINT, self.headers, request.as_body())
        except BotBlocked:
            with self._lock:
                self._blocks += 1
            raise
        with self._lock:
            self._blocks = 0
        return parse_quote(raw, request)

    def _guard(self) -> None:
        with self._lock:
            if self._blocks >= BREAKER_TRIP:
                raise BotBlocked(
                    f"aborted after {self._blocks} consecutive blocked requests - "
                    f"continuing would only make it worse"
                )

    # -- fan-out ----------------------------------------------------------

    def map_quotes(
        self,
        requests: Sequence[QuoteRequest],
        *,
        on_result: Callable[[QuoteRequest, Quote | Exception], None],
        workers: int = MAX_WORKERS,
    ) -> None:
        """Run quotes concurrently, streaming each result to `on_result`.

        Results are handed over as they arrive so callers can reduce
        incrementally and never hold every 700 KB response at once.
        """
        if not requests:
            return
        width = max(1, min(workers, MAX_WORKERS, len(requests)))
        with ThreadPoolExecutor(max_workers=width) as pool:
            futures = {pool.submit(self.quote, r): r for r in requests}
            for future in as_completed(futures):
                request = futures[future]
                try:
                    on_result(request, future.result())
                except BotBlocked:
                    for pending in futures:
                        pending.cancel()
                    raise
                except (TransportError, UsageError) as exc:
                    on_result(request, exc)


def date_windows(
    start: str, end: str, nights: int, step: int = 1
) -> list[tuple[str, str]]:
    """Pickup/return pairs for a rental of `nights` sliding across a range.

    `end` is the last date the car may still be *out*, so the final window
    starts `nights` days before it.
    """
    if nights < 1:
        raise UsageError("--nights must be at least 1")
    if step < 1:
        raise UsageError("--step must be at least 1")
    first = _date(start)
    last = _date(end)
    if last < first:
        raise UsageError(f"end date {end} is before start date {start}")

    windows: list[tuple[str, str]] = []
    cursor = first
    while cursor + timedelta(days=nights) <= last:
        pickup = cursor.strftime("%Y-%m-%d")
        drop = (cursor + timedelta(days=nights)).strftime("%Y-%m-%d")
        windows.append((pickup, drop))
        cursor += timedelta(days=step)
    if not windows:
        raise UsageError(
            f"a {nights}-night rental does not fit between {start} and {end}"
        )
    return windows


def _date(text: str) -> datetime:
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise UsageError(f"bad date {text!r} - use YYYY-MM-DD") from exc
