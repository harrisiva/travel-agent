"""Addresses → Location — Lane A.

search():   mapsSearchV1 {"query": text} → PlaceCandidate list (max 5)
resolve():  getDeliveryLocationV1 {"placeReferenceType": provider, "placeId": id,
            "provider": provider} → Location with coordinates (design 01 §3.2, G2)
token():    base64url(json.dumps(loc.cookie(), separators=(",", ":"))) with no
            padding; from_token() inverts it and raises ValueError on garbage
LocationCache: <dir>/locations.json keyed by fold(text), 30-day TTL, in-memory
            when no directory is writable (try $UBEREATS_CACHE_DIR, then
            ~/.cache/ueats, then the system temp dir, then memory). Never
            stores anything but Locations.

Why coordinates are mandatory: a uev2.loc cookie without latitude/longitude is
accepted by Uber but answers relative to somewhere else entirely (01 §3.2:
distance 2,263 mi, ETA 170 min). That is a silent wrong answer, so resolve()
refuses to build a Location without numeric coordinates.

The directory chain lives in default_cache_dir(); LocationCache itself takes a
directory (or None for memory) so tests can point it anywhere. The cache file
holds only cookie objects — the same shape the token carries — so one decoder
(_location_from_cookie) serves both, and anything that is not a location is
rejected on the way in.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
import time

from .http import PayloadError, Transport
from .model import Location, PlaceCandidate, fold

CACHE_TTL_DAYS = 30
CACHE_FILE = "locations.json"
CACHE_SCHEMA = 1
MAX_CANDIDATES = 5

_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_-]+$")


# --- resolution ----------------------------------------------------------------

def search(transport: Transport, text: str) -> list[PlaceCandidate]:
    """Address text → up to five candidates, in Uber's order (01 §3.1).

    Raises ValueError on blank text (no request is made) and PayloadError when
    `data` is not the list of candidates the API has always returned.
    """
    query = (text or "").strip()
    if not query:
        raise ValueError("an address to look up is required")
    data = transport.call("mapsSearchV1", {"query": query})
    if not isinstance(data, list):
        raise PayloadError(
            "mapsSearchV1 answered without a candidate list — the API may have changed"
        )
    out: list[PlaceCandidate] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        place_id, provider = entry.get("id"), entry.get("provider")
        if not isinstance(place_id, str) or not place_id or not isinstance(provider, str) or not provider:
            continue
        out.append(PlaceCandidate(
            id=place_id, provider=provider,
            line1=str(entry.get("addressLine1") or "").strip(),
            line2=str(entry.get("addressLine2") or "").strip(),
        ))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def resolve(transport: Transport, candidate: PlaceCandidate) -> Location:
    """One candidate → the Location Uber itself would put in the cookie (G2)."""
    data = transport.call("getDeliveryLocationV1", {
        "placeReferenceType": candidate.provider,
        "placeId": candidate.id,
        "provider": candidate.provider,
    })
    if not isinstance(data, dict):
        raise PayloadError(
            "getDeliveryLocationV1 answered without a location object — the API may have changed"
        )
    address = data.get("address")
    if not isinstance(address, dict):
        address = {}
    lat, lon = _coord(data.get("latitude")), _coord(data.get("longitude"))
    if lat is None or lon is None:
        raise PayloadError(
            f"getDeliveryLocationV1 returned no coordinates for {candidate.line1!r} — "
            f"without them every distance and ETA would be wrong, so this is refused"
        )
    line1 = _text(address.get("address1")) or _text(address.get("title")) or candidate.line1
    line2 = _text(address.get("address2")) or _text(address.get("subtitle")) or candidate.line2
    return Location(
        line1=line1,
        line2=line2,
        reference=_text(data.get("reference")) or candidate.id,
        reference_type=_text(data.get("referenceType")) or candidate.provider,
        latitude=lat,
        longitude=lon,
        formatted=_text(address.get("eaterFormattedAddress")),
    )


def _coord(value) -> float | None:
    """A finite float coordinate, or None. bool is excluded (True == 1.0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


# --- tokens --------------------------------------------------------------------

def _cookie_json(loc: Location) -> str:
    return json.dumps(loc.cookie(), separators=(",", ":"), ensure_ascii=False)


def token(loc: Location) -> str:
    """The location as one opaque, shell-safe word: base64url of the cookie JSON."""
    return base64.urlsafe_b64encode(_cookie_json(loc).encode("utf-8")).decode("ascii").rstrip("=")


def from_token(text: str) -> Location:
    """Invert token(). Raises ValueError for anything that is not one.

    The check is strict on purpose: `--at` takes either a token or an address,
    and an address must never be mistaken for a token (urlsafe_b64decode would
    happily "decode" one by skipping the characters it does not like).
    """
    word = (text or "").strip()
    if not word or not _TOKEN_CHARS.match(word):
        raise ValueError("not a location token")
    padded = word + "=" * (-len(word) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        cookie = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise ValueError("not a location token") from e
    try:
        return _location_from_cookie(cookie)
    except ValueError as e:
        raise ValueError("not a location token") from e


def is_token(text: str) -> bool:
    """True when `text` decodes as a location token (so --at needs no request)."""
    try:
        from_token(text)
    except ValueError:
        return False
    return True


def _location_from_cookie(cookie) -> Location:
    """A Location from a uev2.loc object (token or cache entry); ValueError if not one."""
    if not isinstance(cookie, dict):
        raise ValueError("not a location")
    address = cookie.get("address")
    if not isinstance(address, dict):
        raise ValueError("not a location")
    reference = cookie.get("reference")
    reference_type = cookie.get("referenceType") or cookie.get("type")
    if not isinstance(reference, str) or not reference:
        raise ValueError("not a location")
    if not isinstance(reference_type, str) or not reference_type:
        raise ValueError("not a location")
    line1 = _text(address.get("address1")) or _text(address.get("title"))
    line2 = _text(address.get("address2")) or _text(address.get("subtitle"))
    if not line1:
        raise ValueError("not a location")
    lat, lon = _coord(cookie.get("latitude")), _coord(cookie.get("longitude"))
    if lat is None or lon is None:
        # A coordinate-less cookie is accepted by Uber but answers from the
        # wrong place (01 §3.2); a token or cache entry without them is refused.
        raise ValueError("not a location")
    formatted = _text(address.get("eaterFormattedAddress"))
    if formatted == line2:
        formatted = ""   # cookie() fills eaterFormattedAddress from line2; keep the round-trip exact
    return Location(line1=line1, line2=line2, reference=reference, reference_type=reference_type,
                    latitude=lat, longitude=lon, formatted=formatted)


# --- cache ---------------------------------------------------------------------

def default_cache_dir() -> str | None:
    """The first writable directory in the chain, created if needed, or None.

    $UBEREATS_CACHE_DIR → ~/.cache/ueats → <tempdir>/ueats-cache → None. A
    directory counts only if a file can actually be created in it: a
    read-only mount or a HOME that is not ours fails here, not later.
    """
    candidates: list[str] = []
    env = os.environ.get("UBEREATS_CACHE_DIR")
    if env:
        candidates.append(env)
    home = os.path.expanduser("~")
    if home and not home.startswith("~"):
        candidates.append(os.path.join(home, ".cache", "ueats"))
    try:
        candidates.append(os.path.join(tempfile.gettempdir(), "ueats-cache"))
    except Exception:  # no usable temp dir at all
        pass
    for path in candidates:
        if _writable_dir(path):
            return path
    return None


def _writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".probe-", dir=path)
        os.close(fd)
        os.unlink(probe)
        return True
    except Exception:
        return False


class LocationCache:
    """Resolved locations only, keyed by folded address text, 30 days.

    Every read goes to the file (it is tiny), so two CLI invocations never
    see stale state; every write is atomic (temp file + os.replace) so a
    crash mid-write cannot leave a truncated file. A corrupt or foreign file
    is treated as empty and overwritten on the next put. Any I/O failure
    silently degrades to memory for this process — the cache is a
    convenience, never a reason to fail a command.
    """

    def __init__(self, directory: str | None = None) -> None:
        self.directory = directory          # None → in-memory only
        self._memory: dict[str, dict] = {}

    @property
    def path(self) -> str | None:
        return os.path.join(self.directory, CACHE_FILE) if self.directory else None

    @staticmethod
    def key(text: str) -> str:
        return fold(text)

    def get(self, text: str) -> Location | None:
        key = self.key(text)
        if not key:
            return None
        entries = self._load()
        entry = entries.get(key) or self._memory.get(key)
        if not entry:
            return None
        if not self._fresh(entry):
            return None
        try:
            return _location_from_cookie(entry.get("location"))
        except ValueError:
            return None

    def put(self, text: str, loc: Location) -> None:
        if not isinstance(loc, Location):
            raise TypeError("LocationCache stores Locations only")
        key = self.key(text)
        if not key:
            return
        entry = {"saved_at": time.time(), "label": loc.label, "location": loc.cookie()}
        self._memory[key] = entry
        if not self.path:
            return
        entries = self._load()
        entries = {k: v for k, v in entries.items() if self._fresh(v)}
        entries[key] = entry
        self._store(entries)

    # -- file I/O ---------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict) or doc.get("schema") != CACHE_SCHEMA:
            return {}
        entries = doc.get("locations")
        if not isinstance(entries, dict):
            return {}
        return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)}

    def _store(self, entries: dict[str, dict]) -> None:
        doc = {"schema": CACHE_SCHEMA, "locations": entries}
        try:
            os.makedirs(self.directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".locations-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            # Not writable after all: memory already holds the entry.
            self.directory = None

    @staticmethod
    def _fresh(entry: dict) -> bool:
        saved = entry.get("saved_at")
        if isinstance(saved, bool) or not isinstance(saved, (int, float)):
            return False
        age = time.time() - float(saved)
        return 0 <= age <= CACHE_TTL_DAYS * 86400
