"""The one "one request per item, tolerate failure" primitive.

`hours` and `routing` both need it: take a list of places, make an independent
call for each, and attach the result. Written twice it was wrong twice — both
copies caught only `NetworkError` inside the worker, while `ThreadPoolExecutor`
re-raises anything else, so a single shifted index in one item destroyed a
search that had already succeeded.

Everything here follows from two rules:

1. **One item's failure degrades that item, never the run.** Google's payloads
   are positional and undocumented, so `TypeError`/`IndexError` from a moved
   field is a routine occurrence, not an impossible one.
2. **The budget is checked before the work, not during.** An agent asked for
   "every restaurant in the city" should be told the plan is too big up front
   rather than discovering it 300 requests in.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

from gmaps.errors import GmapsError

T = TypeVar("T")

#: Concurrent requests to one host. Measured on a 20-request workload: 8
#: workers took 0.74 s, 12 took 0.60 s, 16 took 0.61 s — the knee is 12, which
#: clears the 25-item ceiling in two waves. No throttling was seen up to 24,
#: but this is an internal surface with no published limit, so 12 is where
#: courtesy and the measurement agree.
DEFAULT_WORKERS = 12


def fan_out(items: list[T], work: Callable[[T], None], *,
            budget: int, workers: int | None = None,
            on_skipped: Callable[[T], None] | None = None) -> int:
    """Run `work` over the first `budget` items, concurrently and safely.

    `work` mutates its item in place and returns nothing; it is called exactly
    once per item. Any exception it raises is swallowed — a worker cannot take
    down the pool — so `work` is responsible for recording its own failure on
    the item it was given.

    Items beyond `budget` are passed to `on_skipped` instead, so a caller can
    mark them explicitly rather than leaving them silently unannotated.

    Returns the number of items actually processed.

    `workers` defaults to `DEFAULT_WORKERS` *at call time*, not at import time.
    A default argument is evaluated once when the function is defined, so
    `--concurrency`, which lowers the module attribute after import, silently
    did nothing and a caller told to turn the rate down could not.
    """
    if workers is None:
        workers = DEFAULT_WORKERS
    if not items or budget <= 0:
        for item in items:
            if on_skipped:
                on_skipped(item)
        return 0

    targets = items[:budget]

    def guarded(item: T) -> None:
        try:
            work(item)
        except GmapsError:
            pass  # work() already recorded it on the item
        except Exception:  # noqa: BLE001
            # A moved index or an unexpected shape. One item is now missing a
            # field; the other nineteen results are still a good answer.
            pass

    if len(targets) == 1 or workers <= 1:
        for item in targets:
            guarded(item)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
            list(pool.map(guarded, targets))

    if on_skipped:
        for item in items[budget:]:
            on_skipped(item)
    return len(targets)


def check_budget(planned: int, ceiling: int, what: str,
                 flag: str = "--max-place-requests") -> None:
    """Refuse an oversized plan up front rather than issuing it.

    Mirrors `campsites find --max-requests`: an agent will cheerfully ask for
    thousands of requests, and the honest response is to say the plan is too
    big, not to start making them.
    """
    if planned > ceiling:
        from gmaps.errors import UsageError
        raise UsageError(
            f"{what} would take {planned} requests, over the {ceiling} ceiling. "
            f"Narrow the query (a smaller --limit), or raise {flag} deliberately."
        )
