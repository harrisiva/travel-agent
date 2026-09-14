"""Decoder for the Maps RPC response.

The body is not JSON. It is a run of ``{"c":<n>,"d":"<chunk>"}`` objects,
optionally separated by ``/*""*/``, whose ``d`` values concatenate into a
``)]}'``-prefixed JSON array.

A single ``json.loads`` of the whole body succeeds on small responses and
raises ``Extra data`` on large ones, which is exactly the kind of bug that
passes every test until someone searches a busy city. Always run the loop.
"""

from __future__ import annotations

import json
from typing import Any

from gmaps.errors import ParseError

_DECODER = json.JSONDecoder()
_SEPARATOR = '/*""*/'


def decode(body: str) -> Any:
    """Chunked-envelope body -> parsed JSON array."""
    parts: list[str] = []
    first_error: str | None = None
    i = 0
    while i < len(body):
        if body.startswith(_SEPARATOR, i):
            i += len(_SEPARATOR)
            continue
        if body[i] != "{":
            i += 1
            continue
        try:
            obj, i = _DECODER.raw_decode(body, i)
        except ValueError as exc:
            # Do NOT give up here. A block page or a consent interstitial is
            # HTML, and its inline scripts contain braces that are not JSON;
            # bailing out on the first one reported "malformed chunk at byte
            # 35" and sent a maintainer hunting a parser regression that did
            # not exist. Keep scanning and let the end classify the body.
            if first_error is None:
                first_error = f"first bad brace at byte {i}: {exc}"
            i += 1
            continue
        if isinstance(obj, dict) and "d" in obj:
            parts.append(obj["d"])

    if not parts:
        reason = _no_chunks_reason(body)
        if first_error and "HTML" not in reason and "Google served" not in reason:
            reason = f"{reason} ({first_error})"
        raise ParseError(reason)

    payload = "".join(parts)
    newline = payload.find("\n")
    if newline == -1:
        raise ParseError("no anti-hijack prefix in payload")
    try:
        return json.loads(payload[newline + 1:])
    except ValueError as exc:
        raise ParseError(f"payload is not JSON: {exc}") from exc


def _no_chunks_reason(body: str) -> str:
    """Say WHICH failure this is.

    An HTML consent page, a CAPTCHA or a rate-limit notice all arrive as a 200
    with no chunks, and reporting them as "the endpoint shape may have changed"
    sends a maintainer hunting a parser regression that does not exist. Google
    is more likely to serve these to a datacenter IP than to a laptop.
    """
    head = body[:4000].lower()
    if "sorry/index" in head or "unusual traffic" in head or "captcha" in head:
        return ("Google served a CAPTCHA / rate-limit page instead of data. "
                "The request rate or the source IP is being challenged.")
    if "consent." in head or "before you continue" in head:
        return "Google served a consent interstitial instead of data."
    if "<html" in head or "<!doctype" in head:
        return (f"expected data, got an HTML page ({len(body)} bytes). "
                "Usually a block page or an interstitial, not a parser fault.")
    return "no data chunks in response — the endpoint shape may have changed"


def result_blobs(data: Any) -> list[list]:
    """Yield the place blobs from a decoded response.

    Every slot is scanned rather than indexed from a fixed offset: a 20-result
    search puts the header at slot 0 and results from slot 1, but a
    single-result query puts its result at slot 0. Hard-coding either one
    silently drops or mis-reads a result.
    """
    # A genuinely empty search still returns the container — `data[0][1]` is a
    # list holding the header slot and no place blobs. A payload where the
    # container is missing entirely is a SHAPE CHANGE, not an empty result, and
    # the two must not share an answer: returning [] for both makes a Google
    # schema change indistinguishable from "nothing matched", which is the one
    # exit code that tells a watch loop to keep waiting.
    try:
        slots = data[0][1]
    except (IndexError, TypeError, KeyError):
        raise ParseError(
            "no result container in the response — the payload shape has "
            "changed, this is not an empty result") from None
    if not isinstance(slots, list):
        raise ParseError(
            f"result container is a {type(slots).__name__}, expected a list — "
            "the payload shape has changed")

    # Every slot Google sends is a list, including the header slot of a
    # genuinely empty search (verified: a no-match query returns exactly one
    # 19-element list whose [14] is null). So slots that are strings or nulls
    # are a SHAPE CHANGE, and returning [] for them reported a broken payload
    # as "nothing matched" — the one exit code that means "keep waiting".
    if slots and not any(isinstance(slot, list) for slot in slots):
        raise ParseError(
            f"{len(slots)} result slots and not one is a list — the payload "
            "shape has changed, this is not an empty result")

    blobs = []
    for slot in slots:
        if not isinstance(slot, list) or len(slot) <= 14:
            continue
        blob = slot[14]
        if isinstance(blob, list) and len(blob) > 11 and isinstance(blob[11], str):
            blobs.append(blob)
    return blobs


def search_center(data: Any) -> tuple[float, float] | None:
    """The coordinates Google resolved the query's location text to.

    Free geocoding: it rides along on the search response, so resolving a place
    name costs no extra request.
    """
    try:
        loc = data[0][1][0][10][3]
        lng, lat = loc[1][1], loc[1][2]
    except (IndexError, TypeError, KeyError):
        return None
    # Range-checked, not merely type-checked. `geocode` prefers this value over
    # the validated blob coordinates, so a swapped or shifted pair silently
    # becomes the origin of every downstream search and route — Google accepts
    # it, searches another hemisphere, and nothing else looks wrong.
    if (isinstance(lat, (int, float)) and isinstance(lng, (int, float))
            and -90 <= lat <= 90 and -180 <= lng <= 180):
        return float(lat), float(lng)
    return None
