"""Store identifiers — Lane A.

Accepted forms (design 02 §2):
  * a UUID  80a1f654-f869-40b2-80bc-f15ba251c4ad  (any case)
  * a store URL  https://www.ubereats.com/ca/store/<slug>/<id>[?…]  or just <id>:
    22 base64url chars = the 16 UUID bytes without padding (verified 404/404)
  * anything else → None (a name; the client runs find)

The 22-char form is accepted only when it round-trips: url_id(store_uuid(x))
== x. A 22-character word whose last character carries non-zero trailing bits
is not something Uber ever emits, so treating it as an id would turn a
restaurant name into a phantom store. Every actionUrl id in the feed evidence
round-trips (see the Lane A verification notes).
"""
from __future__ import annotations

import base64
import binascii
import re
import uuid as _uuid
import urllib.parse

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_URL_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
_HOSTS = ("ubereats.com", "www.ubereats.com")


def store_uuid(text: str) -> str | None:
    """Canonical lowercase UUID, or None when `text` is not an id form."""
    word = (text or "").strip()
    if not word:
        return None
    if _UUID.match(word):
        return word.lower()
    if _URL_ID.match(word):
        return _from_url_id(word)
    segment = _store_url_segment(word)
    if segment is None:
        return None
    if _UUID.match(segment):
        return segment.lower()
    if _URL_ID.match(segment):
        return _from_url_id(segment)
    return None


def url_id(uuid: str) -> str:
    """The 22-char base64url id Uber uses in store URLs."""
    raw = _uuid.UUID(str(uuid).strip()).bytes
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _from_url_id(word: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode((word + "==").encode("ascii"))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 16:
        return None
    parsed = _uuid.UUID(bytes=raw)
    # Only an id Uber could have produced: an RFC 4122 UUID (every real store
    # id is one) whose encoding is exact. Together these reject most 22-letter
    # words that would otherwise decode into a phantom store.
    if parsed.variant != _uuid.RFC_4122:
        return None
    value = str(parsed)
    return value if url_id(value) == word else None


def _store_url_segment(word: str) -> str | None:
    """The id segment of an Uber Eats store URL (or a bare /…/store/… path)."""
    if "/" not in word:
        return None
    lowered = word.lower()
    if "://" not in word and lowered.startswith(_HOSTS):
        word = "https://" + word          # a pasted link with the scheme trimmed
    try:
        parts = urllib.parse.urlsplit(word)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        host = (parts.hostname or "").lower()
        if host not in _HOSTS and not host.endswith(".ubereats.com"):
            return None                   # some other site's /store/ URL
    path = parts.path or ""
    segments = [s for s in path.split("/") if s]
    if "store" not in segments:
        return None
    index = segments.index("store")
    after = segments[index + 1:]
    if not after:
        return None
    # /store/<slug>/<id> is the shape seen; tolerate /store/<id> as well.
    return urllib.parse.unquote(after[-1])
