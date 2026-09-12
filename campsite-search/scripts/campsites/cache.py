"""On-disk cache for Camis reference data.

Reference data (parks, equipment, maps, site metadata) is large — Ontario's
resource payload is ~500KB — and changes rarely. Availability is NEVER cached:
a stale availability answer is worse than no answer.

The cache is an optimisation, never a dependency. In a sandbox `HOME` may be
unset, read-only or ephemeral, so the location is resolved defensively:

1. `$CAMPSITES_CACHE_DIR` if set (``none``/``off``/``0``/empty disables the
   on-disk cache outright).
2. The platform cache directory — `~/Library/Caches` (macOS), `$XDG_CACHE_HOME`
   or `~/.cache` (Linux), `%LOCALAPPDATA%` (Windows).
3. A directory under the system temp dir.

Each candidate must be creatable *and* pass a real write probe — a read-only
directory that already exists survives `mkdir(exist_ok=True)` and would
otherwise fail on every write. If none is usable the cache degrades to
in-process memory, which still collapses repeat lookups within a single run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL = 7 * 24 * 60 * 60

#: Per-dataset lifetimes; anything unlisted (resourcecategory) gets DEFAULT_TTL.
#: Availability, date schedules (`window`, goLiveDate) and alerts are
#: deliberately absent — they are never cached, because a stale answer about
#: them is worse than no answer.
TTLS: dict[str, int] = {
    "parks": 7 * 24 * 60 * 60,          # parks appear about once a year
    "equipment": 30 * 24 * 60 * 60,     # tenant config, effectively static
    "bookingcategories": 30 * 24 * 60 * 60,
    "attributes": 30 * 24 * 60 * 60,
    "maps": 7 * 24 * 60 * 60,
    "resources": 7 * 24 * 60 * 60,      # ~500KB on Ontario; the biggest win
}

#: Point this at a writable path (e.g. /tmp/campsites) in a locked-down
#: environment, or set it to "none" to run without an on-disk cache.
ENV_CACHE_DIR = "CAMPSITES_CACHE_DIR"

_OFF = {"", "0", "off", "none", "false", "no"}


def ttl_for(key: str) -> int:
    """TTL for a cache key like 'resources:-2147483568'."""
    return TTLS.get(key.split(":", 1)[0], DEFAULT_TTL)


def _home() -> Path | None:
    """`Path.home()`, or None when there is no resolvable home directory.

    With `HOME` unset and no matching passwd entry — normal in a container —
    this raises RuntimeError, and on some platforms it yields a bare "~" that
    would create a literal `~` directory in the working tree.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError, KeyError):
        return None
    return home if home.is_absolute() else None


def _platform_dir() -> Path | None:
    """The conventional cache location for this OS, if one can be derived."""
    home = _home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            if home is None:
                return None
            base = str(home / "AppData" / "Local")
        return Path(base) / "campsites" / "cache"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "campsites" if home else None
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        if home is None:
            return None
        base = str(home / ".cache")
    return Path(base) / "campsites"


def _temp_dir() -> Path:
    """Last-resort location: the system temp dir, per-user where possible."""
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"campsites-cache{suffix}"


def cache_dir() -> Path | None:
    """The preferred cache location, or None if on-disk caching is disabled.

    This is the *requested* directory; it may not be writable. `Cache.dir`
    reports the one actually in use.
    """
    override = os.environ.get(ENV_CACHE_DIR)
    if override is not None:
        if override.strip().lower() in _OFF:
            return None
        return Path(override).expanduser()
    return _platform_dir()


def _usable(path: Path, own: bool = False) -> bool:
    """True if `path` exists (or can be created) and a file can be written.

    `own` additionally requires that we own the directory and that it is not a
    symlink: the temp fallback sits in a world-writable place under a
    predictable name, so it must not be something another user planted there.
    A directory named explicitly by the caller is taken at its word.
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
    """True if the caller asked for no on-disk cache at all."""
    override = os.environ.get(ENV_CACHE_DIR)
    return override is not None and override.strip().lower() in _OFF


def _resolve_dir() -> Path | None:
    """First writable candidate, or None to fall back to in-memory caching.

    An explicit "off" is honoured as-is; an explicit directory that turns out
    to be unwritable still falls back, because failing to cache must never
    fail the search.
    """
    if _disabled():
        return None
    explicit = os.environ.get(ENV_CACHE_DIR) is not None
    seen: set[Path] = set()
    # (candidate, must be ours) — only a path we picked ourselves has to pass
    # the ownership check.
    for candidate, own in ((cache_dir(), not explicit), (_temp_dir(), True)):
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        if _usable(candidate, own):
            return candidate
    return None


class Cache:
    def __init__(self, enabled: bool = True, ttl: int | None = None):
        """`ttl` overrides the per-dataset lifetimes in TTLS when set."""
        self.enabled = enabled
        self.ttl = ttl
        self._dir: Path | None = None
        self._resolved = False
        #: Used only when no directory is writable. Values are the caller's own
        #: objects, which CamisClient already memoises for the process, so this
        #: costs dict overhead rather than a second copy of the payloads.
        self._mem: dict[tuple[str, str], tuple[float, Any]] = {}

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

    def _path(self, host: str, key: str) -> Path | None:
        root = self.dir
        if root is None:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        return root / host / f"{digest}.json"

    def get(self, host: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        ttl = self.ttl or ttl_for(key)
        path = self._path(host, key)
        if path is None:
            hit = self._mem.get((host, key))
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

    def set(self, host: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(host, key)
        if path is None:
            self._mem[(host, key)] = (time.time(), value)
            return
        # Per-process temp name: several CLI runs can be writing the same key
        # at once, and a shared scratch file would let them interleave.
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(value, fh)
            tmp.replace(path)  # atomic; safe against concurrent CLI runs
        except (OSError, ValueError, TypeError):
            # Cache failures must never break a search — including a value that
            # will not serialise, which would otherwise leave a truncated file.
            try:
                tmp.unlink()
            except OSError:
                pass

    def clear(self) -> int:
        self._mem.clear()
        root = self.dir
        if root is None:
            return 0
        removed = 0
        try:
            stale = list(root.rglob("*.json")) + list(root.rglob("*.tmp"))
        except OSError:
            return 0
        for path in stale:
            try:
                path.unlink()
                if path.suffix == ".json":
                    removed += 1
            except OSError:
                pass
        return removed
