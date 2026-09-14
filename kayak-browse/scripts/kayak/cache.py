"""On-disk cache and request ledger.

Lifted from `campsite-search/scripts/campsites/cache.py`, and it keeps that
skill's central rule: **cache the stable thing, never the volatile thing.**

Cached here:

* autocomplete / place resolutions (7 days) — a city id does not move
* constants mappings (30 days) — tenant configuration, effectively static

Never cached, and this is not negotiable:

* `/poll` responses and prices. A stale price is worse than no price.
* `searchId` and `cluster`. Both are session-scoped; a reused `searchId`
  returns *some* answer rather than an error, so caching one would silently
  answer today's question with yesterday's search.
* the apiKey, in a cache key or a cached value. Keys go only to the key file
  written by `auth.store_key`, at mode 0600.

The cache is an optimisation, never a dependency. In a sandbox `HOME` may be
unset or the filesystem read-only, so the location is resolved defensively and
degrades to in-process memory. Every candidate must be creatable *and* pass a
real write probe — a read-only directory survives `mkdir(exist_ok=True)` and
would otherwise fail on every write.

The **request ledger** lives here too, because it needs the same directory.
It records a timestamp per API call so that the trailing-hour count survives
across CLI invocations: KAYAK's sandbox allows 250 car searches an hour, and an
agent that ran a sweep two minutes ago in a different process has already spent
some of that.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL = 7 * 24 * 60 * 60

#: Per-dataset lifetimes. Prices and poll responses are deliberately absent —
#: they are never cached at all.
TTLS: dict[str, int] = {
    "autocomplete": 7 * 24 * 60 * 60,
    # `places` is the namespace `Client.places` actually writes under. It was
    # missing here and fell through to DEFAULT_TTL, which happened to be the
    # same number — a right answer by accident, and one that would have gone
    # silently wrong the moment DEFAULT_TTL changed.
    "places": 7 * 24 * 60 * 60,
    "constants": 30 * 24 * 60 * 60,
}

#: Point this at a writable path in a locked-down environment, or set it to
#: "none" to run with no on-disk cache (and no cross-process ledger).
ENV_CACHE_DIR = "KAYAK_CACHE_DIR"

_OFF = {"", "0", "off", "none", "false", "no"}

LEDGER_FILE = "requests.json"

#: Sandbox quotas per API family, per hour, from the KAYAK developer portal.
#: "May change at any time", so these are a client-side guard rail, not a
#: contract: exceeding them yields 429s, and we would rather refuse first.
HOURLY_LIMITS: dict[str, int] = {
    "autocomplete": 100,
    "cars": 250,
    "hotels": 250,
    "flights": 250,
    "priceinsights": 50,
}

_HOUR = 3600


def ttl_for(key: str) -> int:
    """TTL for a cache key like 'autocomplete:cars:9f3c...'."""
    return TTLS.get(key.split(":", 1)[0], DEFAULT_TTL)


def _home() -> Path | None:
    """`Path.home()`, or None when there is no resolvable home directory.

    With `HOME` unset and no matching passwd entry — normal in a container —
    this raises RuntimeError, and on some platforms yields a bare "~" that
    would create a literal `~` directory in the working tree.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError, KeyError):
        return None
    return home if home.is_absolute() else None


def _xdg_dir() -> Path | None:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "kayak-browse"
    home = _home()
    return home / ".cache" / "kayak-browse" if home else None


def _temp_dir() -> Path:
    """Last-resort location: the system temp dir, per-user where possible."""
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"kayak-browse{suffix}"


def _usable(path: Path, own: bool = False) -> bool:
    """True if `path` exists (or can be created) and a file can be written.

    `own` additionally requires that we own the directory and that it is not a
    symlink: the temp fallback sits in a world-writable place under a
    predictable name, so it must not be something another user planted there.
    That check matters more here than in campsites — this directory holds the
    API key file.
    """
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if own and path.is_symlink():
            return False
        if own and hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
            return False
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except (OSError, ValueError):  # ValueError: embedded NUL, bad path
        return False


def _disabled() -> bool:
    override = os.environ.get(ENV_CACHE_DIR)
    return override is not None and override.strip().lower() in _OFF


def _resolve_dir() -> Path | None:
    """First writable candidate, or None to fall back to in-memory caching."""
    if _disabled():
        return None
    explicit = os.environ.get(ENV_CACHE_DIR)
    candidates: list[tuple[Path, bool]] = []
    if explicit:
        # A directory named explicitly by the caller is taken at its word.
        candidates.append((Path(explicit).expanduser(), False))
    else:
        xdg = _xdg_dir()
        if xdg is not None:
            candidates.append((xdg, True))
    candidates.append((_temp_dir(), True))
    seen: set[Path] = set()
    for candidate, own in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _usable(candidate, own):
            return candidate
    return None


class Cache:
    """Key/value store with per-dataset TTLs, degrading to memory."""

    def __init__(self, enabled: bool = True, ttl: int | None = None):
        """`ttl` overrides the per-dataset lifetimes in TTLS when set."""
        self.enabled = enabled
        self.ttl = ttl
        self._dir: Path | None = None
        self._resolved = False
        self._mem: dict[str, tuple[float, Any]] = {}
        #: In-process request timestamps, used when no directory is writable.
        #: The budget must still be enforced then — degraded persistence is a
        #: reason to under-count, never a reason to stop counting.
        self._mem_ledger: dict[str, list[float]] = {}

    @property
    def dir(self) -> Path | None:
        """The directory in use, or None when the cache is memory-only.

        Resolved on first access so that constructing a Cache never touches
        the filesystem.
        """
        if not self._resolved:
            self._resolved = True
            self._dir = _resolve_dir()
        return self._dir

    def _path(self, key: str) -> Path | None:
        root = self.dir
        if root is None:
            return None
        # The key is hashed, so a search term never lands in a filename. The
        # apiKey is never part of a key in the first place — see key_for().
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        return root / "v1" / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        ttl = self.ttl or ttl_for(key)
        path = self._path(key)
        if path is None:
            hit = self._mem.get(key)
            if hit is None or time.time() - hit[0] > ttl:
                return None
            return hit[1]
        try:
            if time.time() - path.stat().st_mtime > ttl:
                return None
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        if path is None:
            self._mem[key] = (time.time(), value)
            return
        _atomic_write_json(path, value)

    def clear(self) -> int:
        self._mem.clear()
        self._mem_ledger.clear()
        root = self.dir
        if root is None:
            return 0
        removed = 0
        try:
            stale = list((root / "v1").rglob("*.json"))
        except OSError:
            return 0
        for path in stale:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ---------------- request ledger ----------------

    def recent_requests(self, family: str, window: int = _HOUR) -> int:
        """How many calls this machine has made to `family` in the last hour.

        Reads the in-process counter only when the cache is disabled — see
        `_ledger_path` for why a disabled cache must not consult the file.
        """
        return len(self._load_ledger().get(family, []))

    def record_requests(self, family: str, count: int = 1) -> None:
        """Append `count` timestamps for `family`, pruning anything older than
        an hour. Best-effort: a lost write under-counts, which can only make
        us more permissive, so it must never raise into a search.

        Counting always happens; only the *persistence* is conditional. A
        sweep must still be unable to exceed its quota within one process.
        """
        if count <= 0:
            return
        now = time.time()
        ledger = self._load_ledger()
        ledger.setdefault(family, []).extend([now] * count)
        self._save_ledger(ledger)

    def _ledger_path(self) -> Path | None:
        """The ledger file, or None to keep the count in this process only.

        None when the cache is disabled, and this is the non-obvious part:
        the ledger is *quota* state, not cache state, so the intuitive reading
        — "we should always record what we sent" — is exactly backwards here.
        `enabled=False` means no request is really being spent (the offline
        self-check, `--no-cache`, a dry run), and charging those to the
        250/hour ledger bills a user for traffic that never left the machine.
        Two runs of the offline suite used to leave ~240 fabricated car
        requests in `~/.cache/kayak-browse/requests.json`, after which genuine
        searches were refused with BudgetError for an hour.

        `self.dir` is deliberately not touched on the disabled path: resolving
        it probes and *creates* a cache directory, and "disabled" has to mean
        touch nothing, not touch nothing except this.
        """
        if not self.enabled:
            return None
        root = self.dir
        return None if root is None else root / LEDGER_FILE

    def _load_ledger(self) -> dict[str, list[float]]:
        cutoff = time.time() - _HOUR
        path = self._ledger_path()
        raw: dict[str, list[float]]
        if path is None:
            raw = self._mem_ledger
        else:
            try:
                with path.open(encoding="utf-8") as fh:
                    loaded = json.load(fh)
                raw = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                raw = {}
        pruned: dict[str, list[float]] = {}
        for family, stamps in raw.items():
            if not isinstance(stamps, list):
                continue
            kept = [float(t) for t in stamps
                    if isinstance(t, (int, float)) and t > cutoff]
            if kept:
                pruned[family] = kept
        return pruned

    def _save_ledger(self, ledger: dict[str, list[float]]) -> None:
        path = self._ledger_path()
        if path is None:
            self._mem_ledger = ledger
            return
        _atomic_write_json(path, ledger)


def _atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON via a per-process temp file and os.replace.

    Several CLI runs can be writing the same key at once; a shared scratch
    file would let them interleave. A failure here is swallowed — the cache
    must never break a search, including a value that will not serialise,
    which would otherwise leave a truncated file behind.
    """
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(value, fh)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        try:
            tmp.unlink()
        except OSError:
            pass


def key_for(namespace: str, *parts: str) -> str:
    """A cache key. `parts` are hashed together; never pass the apiKey.

    Search terms are normalised (stripped, lowercased) before hashing so that
    "  Toronto " and "toronto" share one entry.
    """
    digest = hashlib.sha256(
        "\x1f".join(p.strip().lower() for p in parts).encode("utf-8")
    ).hexdigest()
    return f"{namespace}:{digest}"
