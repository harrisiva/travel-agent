"""API key resolution, key storage, and userTrackId minting.

KAYAK emails the affiliate key on signup and stores it nowhere retrievable, so
losing it means asking for a new one. That shapes two rules here:

* **The key is never printed.** Not in an error, not in `--json`, not in a
  cache key, not in argv (see `http._curl`). `redact()` is the last line of
  defence for text that came from somewhere else.
* **`login` stores it at mode 0600** in the cache directory, so a user who has
  it in an email can stop pasting it into every command — and so it never has
  to live in shell history.

Resolution order, first hit wins:

1. `--api-key`
2. `$KAYAK_API_KEY`
3. `--key-file <path>` (`-` reads stdin)
4. the key file written by `kayak.py login`
5. failure, exit 4

`userTrackId` gets its own home here because getting it wrong is a
bot-detection problem rather than a correctness one. KAYAK requires a UUID per
end user per session: a constant like "test" gets rate-limited, and a fresh id
per HTTP request looks like a swarm of users. So exactly one is minted per CLI
invocation and shared across every thread of a sweep.
"""

from __future__ import annotations

import os
import stat
import sys
import uuid
from pathlib import Path

from .cache import Cache
from .errors import AuthError, UsageError

ENV_API_KEY = "KAYAK_API_KEY"

#: Name of the stored-key file inside the cache directory. Deliberately not
#: under the `v1/` subtree that `cache-clear` empties — clearing cached
#: lookups must not log the user out.
KEY_FILE = "api-key"

_HELP = (
    "Pass --api-key, set $KAYAK_API_KEY, or run "
    "`kayak.py login --key-file <path>` once to store it."
)


def mint_user_track_id() -> str:
    """A fresh userTrackId for this invocation. Call once, share everywhere."""
    return uuid.uuid4().hex


def redact(text: str, key: str | None) -> str:
    """Remove an API key from text that is about to be shown or logged.

    Nothing in this package should be building a message containing the key in
    the first place. This exists for text we did not write — a server error
    that echoes the request URL, most of all.
    """
    if not key or not text:
        return text
    return text.replace(key, "***")


def stored_key_path(cache: Cache) -> Path | None:
    root = cache.dir
    return None if root is None else root / KEY_FILE


def load_stored_key(cache: Cache) -> str | None:
    path = stored_key_path(cache)
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return value or None


def store_key(cache: Cache, key: str) -> Path:
    """Persist `key` at mode 0600, and report where it went.

    The file is created with the restrictive mode *before* anything is written
    to it, so the secret is never briefly world-readable.
    """
    path = stored_key_path(cache)
    if path is None:
        raise UsageError(
            "no writable cache directory, so the key cannot be stored. "
            f"Set $KAYAK_CACHE_DIR to a writable path, or export ${ENV_API_KEY} "
            "instead."
        )
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key.strip() + "\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise UsageError(f"could not write the key file: {e}") from e
    return path


def read_key_file(path: str) -> str:
    """Read a key from a file, or from stdin when `path` is "-"."""
    if path == "-":
        key = sys.stdin.read().strip()
        if not key:
            raise UsageError("no key on stdin")
        return key
    try:
        key = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as e:
        raise UsageError(f"could not read {path}: {e}") from e
    if not key:
        raise UsageError(f"{path} is empty")
    return key


def resolve_key(
    explicit: str | None = None,
    key_file: str | None = None,
    cache: Cache | None = None,
) -> str:
    """The API key to use, by the documented order of precedence.

    Raises AuthError (exit 4) rather than returning None, because every code
    path that wants a key cannot proceed without one, and "no key" must never
    be reported as "nothing available".
    """
    if explicit:
        return explicit.strip()
    # An explicitly passed --key-file outranks the environment. Both are
    # deliberate, but the flag was typed for THIS run while the variable may
    # be a stale export from another shell; silently preferring the ambient
    # value means a user who passes a key file can be authenticated as
    # somebody else and never be told.
    if key_file:
        return read_key_file(key_file)
    env = os.environ.get(ENV_API_KEY)
    if env and env.strip():
        return env.strip()
    stored = load_stored_key(cache or Cache())
    if stored:
        return stored
    raise AuthError(f"no KAYAK API key found. {_HELP}")
