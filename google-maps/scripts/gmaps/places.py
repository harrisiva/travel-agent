"""Place search against the Maps RPC. Read-only; nothing here books anything."""

from __future__ import annotations

from gmaps import pb
from gmaps.errors import UsageError
from gmaps.http import Session
from gmaps.model import UNKNOWN, place_from_blob
from gmaps.parse import decode, result_blobs, search_center

ENDPOINT = "https://www.google.com/search"


def _fetch(session: Session, query: str, lat: float, lng: float,
           span_m: int, page_size: int, offset: int, hl: str, gl: str):
    params = {"tbm": "map", "hl": hl, "gl": gl, "q": query,
              "tch": "1", "ech": "1"}
    # pb goes in raw: percent-encoding its "!" delimiters makes Google return
    # an empty result set rather than an error.
    body = session.get_text(ENDPOINT, params,
                            raw_suffix=f"pb={pb.search_pb(lat, lng, span_m, page_size, offset)}")
    return decode(body)


def geocode(session: Session, where: str, hl: str = "en",
            gl: str = "ca") -> tuple[float, float, str]:
    """Resolve a place name or address to coordinates.

    Name -> coordinates is its own step for the same reason ``parks`` is its own
    command in campsite-search: the agent is handed a place name by a human and
    everything downstream wants numbers. Never make the caller hard-code a
    latitude.

    Two independent paths are tried — the response's resolved centre, then the
    top result's own coordinates — because they sit at unrelated indices, so a
    shift in one does not take the command down.
    """
    if not where.strip():
        raise UsageError("empty location")
    # A wide span: we are locating the query itself, not searching around it.
    data = _fetch(session, where, 0.0, 0.0, 20000000, 5, 0, hl, gl)

    center = search_center(data)
    blobs = result_blobs(data)
    if center:
        label = where
        if blobs:
            first = place_from_blob(blobs[0])
            if first:
                label = first["name"]
        return center[0], center[1], label

    for blob in blobs:
        place = place_from_blob(blob)
        if place:
            return place["lat"], place["lng"], place["name"]

    raise UsageError(f"could not locate {where!r}")


def search(session: Session, query: str, lat: float, lng: float,
           span_m: int = 10000, limit: int = 20, hl: str = "en", gl: str = "ca",
           max_requests: int = 5) -> list[dict]:
    """Places matching ``query`` around a point, most relevant first.

    Paging is capped by ``max_requests`` rather than by ``limit`` alone: an
    agent asked for "every restaurant in the city" will otherwise walk offsets
    forever against a courtesy-hosted endpoint.
    """
    if limit < 1:
        raise UsageError("--limit must be at least 1")

    pages = min(max_requests, -(-limit // pb.PAGE_SIZE))
    out: list[dict] = []
    seen: set[str] = set()

    for page in range(pages):
        data = _fetch(session, query, lat, lng, span_m,
                      pb.PAGE_SIZE, page * pb.PAGE_SIZE, hl, gl)
        blobs = result_blobs(data)
        if not blobs:
            break  # a genuinely empty page ends paging; it is not an error
        for blob in blobs:
            place = place_from_blob(blob)
            if not place:
                continue
            key = place["ftid"] or f"{place['name']}@{place['lat']:.5f},{place['lng']:.5f}"
            if key in seen:
                continue
            seen.add(key)
            out.append(place)
        if len(out) >= limit:
            break

    # The WHOLE page, not out[:limit]. Every blob here arrived in the same
    # response and is already parsed, so truncating before the caller's filters
    # run discards candidates that were free — and nothing counts the loss, so
    # the short answer reads as "there is nothing more". Measured on one real
    # 20-result page: --limit 8 --min-rating 4.5 returned 6 places when 14
    # qualifying ones were sitting in the response we already had.
    # The caller truncates after filtering.
    return out


def filter_open(places: list[dict], want_open: bool) -> tuple[list[dict], int]:
    """Split on live status, counting — never discarding silently — the unknowns.

    Absent hours are common (verified in Tokyo). Reporting them as closed is a
    false negative, so they are excluded from an ``--open-now`` result *and*
    counted, so the caller can say "3 more, hours unknown" rather than pretend
    they do not exist.
    """
    if not want_open:
        return places, sum(1 for p in places if p["status"] == UNKNOWN)
    kept = [p for p in places if p["status"] == "open"]
    unknown = sum(1 for p in places if p["status"] == UNKNOWN)
    return kept, unknown
