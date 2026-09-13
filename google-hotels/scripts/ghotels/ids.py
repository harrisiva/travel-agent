"""Offline id conversions: ftid ⇄ place_id ⇄ CID ⇄ entity token (04 §10), and
the reader for `gmaps.py search --full --json` files (04 §3.1/§3.3).

Pure functions. No I/O except `read_gmaps_file`, which reads a local file.
Lane A owns this file. Byte-for-byte assertions against evidence/requests.log
are in test_hotels.py §3.1.

The four forms, and how they relate (01 §Tokens):

    ftid      0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce     two hex halves; the CID is the second
    place_id  ChIJv7gvLjvKcFMRzvb0LKnpmQA             base64url protobuf {1:{1:fixed64 A, 2:fixed64 B}}
    cid       43322584249726670                        = B
    token     CgkIzu3T55K1-kwQAQ                       base64url protobuf {1:{1:varint cid}, 2:1}

ftid and place_id carry the same two numbers, so every form converts to every
other offline — except a vacation-rental token, which carries a rental id in
field 1.2 instead of a CID and can only come from a pasted Google Hotels link.

The protobuf writer and reader below are the whole dependency: varint,
length-delimited, fixed64. `ts.py` imports the writer.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from .model import KIND_HOTEL, KIND_RENTAL, Candidate, Center, HotelIds


class IdError(ValueError):
    """An id that is not any of the four accepted forms — a usage error (exit 2)."""


# --- minimal protobuf -------------------------------------------------------
#
# Wire types: 0 varint, 1 fixed64, 2 length-delimited. Nothing else appears in
# a token, a place_id or a ts, so nothing else is written; the reader also
# understands wire type 5 (fixed32) purely so an unexpected field skips
# cleanly instead of desynchronising the stream.

_WIRE_VARINT, _WIRE_FIXED64, _WIRE_BYTES, _WIRE_FIXED32 = 0, 1, 2, 5


def pb_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError(f"protobuf varint must be non-negative, got {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return pb_varint((field << 3) | wire)


def pb_uint(field: int, value: int) -> bytes:
    """Field `field` as a varint."""
    return _tag(field, _WIRE_VARINT) + pb_varint(value)


def pb_fixed64(field: int, value: int) -> bytes:
    """Field `field` as a little-endian fixed64."""
    return _tag(field, _WIRE_FIXED64) + value.to_bytes(8, "little")


def pb_bytes(field: int, payload: bytes) -> bytes:
    """Field `field` as a length-delimited blob — a string or a nested message."""
    return _tag(field, _WIRE_BYTES) + pb_varint(len(payload)) + payload


def pb_message(field: int, *parts: bytes) -> bytes:
    """Field `field` as a nested message made of already-encoded parts."""
    return pb_bytes(field, b"".join(parts))


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos
        if shift > 70:
            raise ValueError("varint too long")


def pb_decode(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode one message into [(field, wire, value)], in stream order.

    Varints and fixed-width fields decode to int; length-delimited fields stay
    bytes — the caller decides whether they are a string or a nested message.
    Raises ValueError on anything malformed.
    """
    out: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("protobuf field number 0")
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == _WIRE_FIXED64:
            if pos + 8 > len(data):
                raise ValueError("truncated fixed64")
            value = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        elif wire == _WIRE_BYTES:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[pos:pos + length]
            pos += length
        elif wire == _WIRE_FIXED32:
            if pos + 4 > len(data):
                raise ValueError("truncated fixed32")
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.append((field, wire, value))
    return out


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    """Decode either base64 alphabet, padded or not. ValueError if not base64."""
    if not text or not re.fullmatch(r"[A-Za-z0-9_\-+/]+=*", text):
        raise ValueError("not base64")
    body = text.rstrip("=").replace("+", "-").replace("/", "_")
    if len(body) % 4 == 1:
        raise ValueError("not base64")
    try:
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError) as e:  # binascii.Error is a ValueError
        raise ValueError("not base64") from e


# --- the four forms ---------------------------------------------------------

_FTID_RE = re.compile(r"^0x([0-9a-fA-F]{1,16}):0x([0-9a-fA-F]{1,16})$")
_CID_RE = re.compile(r"^[0-9]{1,20}$")
_MAX_U64 = (1 << 64) - 1


def _ftid_halves(ftid: str) -> tuple[int, int]:
    m = _FTID_RE.match(ftid.strip())
    if not m:
        raise IdError(f"'{ftid}' is not an ftid — expected 0x<hex>:0x<hex>")
    return int(m.group(1), 16), int(m.group(2), 16)


def cid_from_ftid(ftid: str) -> int:
    """'0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce' -> 43322584249726670 (the second half)."""
    return _ftid_halves(ftid)[1]


def _place_id_halves(place_id: str) -> tuple[int, int]:
    """place_id -> (A, B): base64 -> {1: {1: fixed64 A, 2: fixed64 B}}."""
    text = place_id.strip()
    try:
        outer = pb_decode(_b64url_decode(text))
        inner = [v for f, w, v in outer if f == 1 and w == _WIRE_BYTES]
        if len(inner) != 1:
            raise ValueError("no field 1")
        fields = {f: v for f, w, v in pb_decode(inner[0]) if w == _WIRE_FIXED64}
        return fields[1], fields[2]
    except (ValueError, KeyError) as e:
        raise IdError(f"'{text}' is not a Google Maps place_id (ChIJ…): {e}") from None


def cid_from_place_id(place_id: str) -> int:
    """'ChIJ…' -> base64 -> protobuf {1:{1:fixed64,2:fixed64}} (little-endian) -> field 2."""
    return _place_id_halves(place_id)[1]


def ftid_from_place_id(place_id: str) -> str:
    """The same two numbers, written the way google-maps prints them."""
    a, b = _place_id_halves(place_id)
    return f"0x{a:x}:0x{b:x}"


def place_id_from_ftid(ftid: str) -> str:
    """base64url(protobuf {1: {1: fixed64 A, 2: fixed64 B}}), no padding."""
    a, b = _ftid_halves(ftid)
    return _b64url_encode(pb_message(1, pb_fixed64(1, a), pb_fixed64(2, b)))


def token_from_cid(cid: int, kind: int = KIND_HOTEL) -> str:
    """base64url(protobuf {1: {1: varint cid}, 2: 1}), no padding. Hotels only.

    A rental token needs the rental id in field 1.2, which no Maps id carries,
    so `kind` other than KIND_HOTEL is refused rather than encoded wrongly.
    """
    if kind != KIND_HOTEL:
        raise IdError(
            "a vacation-rental token cannot be built from a CID — it needs the "
            "rental id from a pasted Google Hotels link (--token)"
        )
    if not isinstance(cid, int) or isinstance(cid, bool) or not 0 < cid <= _MAX_U64:
        raise IdError(f"CID must be a positive 64-bit integer, got {cid!r}")
    return _b64url_encode(pb_message(1, pb_uint(1, cid)) + pb_uint(2, KIND_HOTEL))


def decode_token(token: str) -> tuple[int | None, int]:
    """-> (cid or None for a rental, kind). Rental tokens carry field 1.2, not 1.1.

    Raises IdError unless the token is {1: {...}, 2: kind} with kind 1 or 2.
    A hotel token whose field 1 has no CID (the KG-id-only form Google
    answers with an unknown entity) decodes to (None, 1); callers that need a
    CID must check.
    """
    text = token.strip()
    try:
        outer = pb_decode(_b64url_decode(text))
    except ValueError as e:
        raise IdError(f"'{text}' is not an entity token: {e}") from None
    inner = [v for f, w, v in outer if f == 1 and w == _WIRE_BYTES]
    kinds = [v for f, w, v in outer if f == 2 and w == _WIRE_VARINT]
    if len(inner) != 1 or len(kinds) != 1 or kinds[0] not in (KIND_HOTEL, KIND_RENTAL):
        raise IdError(f"'{text}' is not an entity token (expected fields 1 and 2, kind 1 or 2)")
    kind = kinds[0]
    try:
        fields = {f: v for f, w, v in pb_decode(inner[0]) if w == _WIRE_VARINT}
    except ValueError as e:
        raise IdError(f"'{text}' is not an entity token: {e}") from None
    if kind == KIND_RENTAL:
        return None, KIND_RENTAL
    cid = fields.get(1)
    if cid is not None and cid > _MAX_U64:
        # A 10-byte varint decodes fine but token_from_cid would refuse it;
        # sending it verbatim only buys an UnknownEntity round trip.
        raise IdError(f"'{text}' is not an entity token (CID exceeds 64 bits)")
    return (cid if cid else None), KIND_HOTEL


def gmaps_command(name: str) -> str:
    """The literal google-maps command a bare name is refused with (04 §3.1)."""
    quoted = name.replace('"', r"\"")
    return f'gmaps.py search --near "<place>" --query "{quoted}" --limit 1 --full --json > hotel.json'


def parse_hotel_id(text: str) -> HotelIds:
    """Accept ftid / place_id / decimal CID / entity token; refuse anything else.

    A bare hotel NAME must raise IdError whose message contains the literal
    gmaps command to run (04 §3.1).
    """
    text = (text or "").strip()
    if _FTID_RE.match(text):
        a, b = _ftid_halves(text)
        return HotelIds(ftid=f"0x{a:x}:0x{b:x}", place_id=place_id_from_ftid(text),
                        cid=b, token=token_from_cid(b), kind=KIND_HOTEL)
    if text[:2].lower() == "0x":
        _ftid_halves(text)  # raises IdError naming the expected shape
    if text.startswith("ChIJ"):
        a, b = _place_id_halves(text)  # raises IdError if malformed
        return HotelIds(ftid=f"0x{a:x}:0x{b:x}", place_id=text.rstrip("="), cid=b,
                        token=token_from_cid(b), kind=KIND_HOTEL)
    if _CID_RE.match(text):
        cid = int(text)
        if not 0 < cid <= _MAX_U64:
            raise IdError(f"'{text}' is not a CID — expected a positive 64-bit integer")
        return HotelIds(ftid=None, place_id=None, cid=cid, token=token_from_cid(cid), kind=KIND_HOTEL)
    try:
        cid, kind = decode_token(text)
    except IdError:
        raise IdError(
            f"'{text}' is not a hotel id (0x…:0x… ftid, ChIJ… place_id, decimal CID or "
            f"entity token). If it is a hotel name, find it with google-maps first: "
            f"{gmaps_command(text)}, then pass --ids-from hotel.json"
        ) from None
    if kind == KIND_RENTAL:
        return HotelIds(ftid=None, place_id=None, cid=None, token=text, kind=KIND_RENTAL)
    if cid is None:
        raise IdError(
            f"'{text}' is an entity token with no hotel id in it (Knowledge Graph id only) — "
            f"Google answers it with an unknown entity; use the ftid, place_id or CID instead"
        )
    # Kept verbatim: a pasted token may also carry the KG id (field 1.3),
    # which Google accepts; the CID-only form is equivalent, not canonical.
    return HotelIds(ftid=None, place_id=None, cid=cid, token=text, kind=KIND_HOTEL)


# --- the gmaps file ---------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _center(raw: Any) -> Center | None:
    """gmaps' `from`: {"query", "name", "lat", "lng"}; None unless both coordinates are numbers."""
    if not isinstance(raw, dict):
        return None
    lat, lng = _num(raw.get("lat")), _num(raw.get("lng"))
    if lat is None or lng is None:
        return None
    label = raw.get("name") or raw.get("query")
    return Center(lat=lat, lng=lng, label=label if isinstance(label, str) else None)


def _entry_ids(entry: dict) -> HotelIds | None:
    """`ftid` first, `place_id` as the fallback; None when neither converts."""
    for key in ("ftid", "place_id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return parse_hotel_id(value)
            except IdError:
                continue
    return None


def read_gmaps_file(path: str) -> tuple[list[Candidate], list[str], Center | None]:
    """Read a `gmaps.py search --full --json` file.

    Returns (candidates with ids, skipped names without ids, the `from` centre).
    Raises IdError naming the file if it has no `results` list.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise IdError(f"cannot read {path}: {e.strerror or e}") from None
    except ValueError as e:
        raise IdError(f"{path} is not JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise IdError(
            f"{path} has no `results` list — expected the output of "
            f"`gmaps.py search … --full --json`"
        )
    candidates: list[Candidate] = []
    skipped: list[str] = []
    for entry in data["results"]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") if isinstance(entry.get("name"), str) else None
        ids = _entry_ids(entry)
        if ids is None:
            skipped.append(name or "(unnamed)")
            continue
        reviews = entry.get("reviews")
        candidates.append(Candidate(
            name=name, ids=ids,
            lat=_num(entry.get("lat")), lng=_num(entry.get("lng")),
            rating=_num(entry.get("rating")),
            reviews=reviews if isinstance(reviews, int) and not isinstance(reviews, bool) else None,
            address=entry.get("address") if isinstance(entry.get("address"), str) else None,
        ))
    return candidates, skipped, _center(data.get("from"))
