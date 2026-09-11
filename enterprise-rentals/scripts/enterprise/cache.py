"""On-disk cache for the *stable* half of the API.

The rule this file exists to enforce: cache the stable thing, never the
volatile thing. Branch catalogues and opening hours barely move and are cached
for days. Prices, availability and one-way eligibility are **never** cached at
any TTL - a stale "available" is worse than no answer at all.

There is deliberately no API here for caching a quote.

The second rule, learned the hard way: **a cache entry is only as trustworthy
as the check that reads it back.** A renter-age payload once sat on disk under
a `locations` key; the caller found no `location.id` in it and reported a real
branch as "does not exist" for seven days. Every caller now passes `validate`,
and anything that fails it is a miss - on the way in *and* on the way out, so a
poisoned or negative entry heals itself instead of needing `cache --clear`.

The skill may run somewhere with no writable directory and no HOME, so every
filesystem operation degrades to an in-memory dict rather than raising.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

#: TTLs in seconds, by namespace.
TTLS = {
    "locations": 7 * 86400,
    "hours": 86400,       # holiday hours move
    "renterage": 30 * 86400,
}

_MEMORY: dict[str, tuple[float, Any]] = {}


def _cache_root() -> Path | None:
    """First writable candidate, or None to run memory-only."""
    override = os.environ.get("ENTERPRISE_CACHE_DIR")
    candidates = [Path(override)] if override else []
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home and home != "~":
        candidates.append(Path(home) / ".cache" / "enterprise-rentals")
    candidates.append(Path(tempfile.gettempdir()) / "enterprise-rentals")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return None


_ROOT = _cache_root()


def _slug(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:120]


def _trusted(value: Any, validate: Callable[[Any], bool] | None) -> bool:
    """Whether `value` is worth handing back, and worth keeping.

    A validator that itself blows up on a surprising payload is answering the
    question: that payload is not what the caller expects.
    """
    if validate is None:
        return True
    try:
        return bool(validate(value))
    except Exception:
        return False


def _reject(namespace: str, key: str, value: Any) -> Any:
    """What to return when a freshly fetched value fails its validator.

    The value is handed back rather than raised on: callers already know how to
    recognise an unusable payload (`by_id` refetches, `search` returns nothing),
    and raising here would turn a soft miss into a hard error.
    """
    return value


def get_or_fetch(
    namespace: str,
    key: str,
    fetch: Callable[[], Any],
    *,
    enabled: bool = True,
    validate: Callable[[Any], bool] | None = None,
) -> Any:
    """Return a cached value, else fetch and store it.

    A cache miss, an unreadable file, a corrupt entry and an entry that fails
    `validate` are all just misses - the last one because a payload that is not
    the shape this key promises is indistinguishable from junk, however it got
    there.

    `validate` also decides what is *stored*: an error or "unknown id" body
    that the API happens to serve with a 200 fails it, so negative answers are
    returned once and never persisted. Callers that need to know whether the
    network was actually consulted should watch their own `fetch` being called
    - a value that came from the cache has, by construction, passed `validate`.
    """
    if not enabled:
        # --no-cache skips STORAGE, not validation. Letting an unusable payload
        # through here made the uncached path behave differently from the
        # cached one - and `doctor`, the diagnostic command, is the one that
        # runs uncached.
        value = fetch()
        return value if _trusted(value, validate) else _reject(namespace, key, value)

    ttl = TTLS.get(namespace, 3600)
    slot = f"{namespace}/{_slug(key)}"
    now = time.time()

    hit = _MEMORY.get(slot)
    if hit and now - hit[0] < ttl:
        if _trusted(hit[1], validate):
            return hit[1]
        _MEMORY.pop(slot, None)

    path = _ROOT / namespace / f"{_slug(key)}.json" if _ROOT else None
    if path and path.exists():
        try:
            if now - path.stat().st_mtime < ttl:
                value = json.loads(path.read_text(encoding="utf-8"))
                if _trusted(value, validate):
                    _MEMORY[slot] = (now, value)
                    return value
                # Poisoned or stale-shaped: delete it, so the next run does not
                # pay the same lookup again for the same wrong answer.
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    value = fetch()
    if not _trusted(value, validate):
        return value  # answered, but not something to remember
    _MEMORY[slot] = (now, value)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass  # memory-only is a perfectly good outcome
    return value


def clear() -> str:
    _MEMORY.clear()
    if not _ROOT:
        return "in-memory cache cleared (no writable cache directory)"
    removed = 0
    for path in _ROOT.rglob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return f"cleared {removed} cached file(s) from {_ROOT}"


def describe() -> str:
    return str(_ROOT) if _ROOT else "(memory only - no writable directory)"
