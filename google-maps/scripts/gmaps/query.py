"""The place-query pipeline, shared by `search` and `nearby`.

These two commands were 80% the same sequence written twice, and had already
drifted apart. One pipeline, parameterised by a `QuerySpec`, keeps them honest
and makes the business logic testable without building an `argparse.Namespace`.

Order matters, and it is the cheap filters first. Live status and rating come
free in the search response; hours and travel time cost one request *per
place*. Filtering before enrichment rather than after is the difference between
20 requests and 6 on a typical query.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from gmaps import hours as hours_mod
from gmaps import routing
from gmaps.errors import UsageError
from gmaps.fanout import check_budget
from gmaps.http import Session
from gmaps.model import UNKNOWN, haversine_km
from gmaps.places import geocode, search


@dataclass(frozen=True)
class QuerySpec:
    """Everything a place query needs, independent of how it was typed."""

    near: str
    query: str = "restaurants"
    span_m: int = 10000
    limit: int = 20
    min_rating: float | None = None
    open_now: bool = False
    include_closed_businesses: bool = False
    want_hours: bool = False
    open_at: str | None = None
    mode: str = "drive"
    want_travel: bool = False
    within_minutes: float | None = None
    sort: str = "relevance"
    hl: str = "en"
    gl: str = "ca"
    max_search_requests: int = 5
    max_place_requests: int = 25


@dataclass
class QueryResult:
    origin: dict
    spec: QuerySpec
    places: list[dict] = field(default_factory=list)
    hours_unknown: int = 0
    week_unknown: int = 0
    travel_unknown: int = 0
    #: Enrichment lookups that failed for network reasons. A result list
    #: emptied by these is NOT "nothing matched" — see cli._outcome.
    network_errors: int = 0
    #: Places a per-place ceiling stopped us looking up, in EITHER stage.
    #: Distinct from places Google has no data for, and the only signal that a
    #: short answer was truncated rather than complete: an unexplained short
    #: list reads as "there is nothing more", which is a false negative. This
    #: replaces a `truncated_at` field that was declared, emitted in every
    #: payload and rendered as a footnote, but never once assigned — so it read
    #: null forever and the footnote was unreachable.
    skipped: int = 0
    #: Places whose weekly-hours lookup FAILED. Kept apart from `week_unknown`
    #: for the same reason: "we could not ask" is not "Google publishes none".
    hours_failed: int = 0
    #: How many places passed the free filters before `--limit` cut the list.
    #: Without it a capped answer is indistinguishable from an exhausted one.
    matched_before_limit: int = 0


def as_coordinates(text: str) -> tuple[float, float] | None:
    """'lat,lng' -> a point, or None if this is free text.

    Its own function because the *cost* of a location depends on the answer: a
    coordinate is free, free text is one geocode request. A budget that guesses
    differently from the resolver charges for requests that never happen, or
    misses ones that do.
    """
    if not text or "," not in text:
        return None
    head, _, tail = text.partition(",")
    try:
        lat, lng = float(head), float(tail)
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


def resolve(session: Session, text: str, hl: str = "en",
            gl: str = "ca") -> tuple[float, float, str]:
    """`--near` as either 'lat,lng' or free text.

    Coordinates are taken literally and cost nothing; free text costs one
    request. Both are accepted everywhere a location is.
    """
    if not text or not text.strip():
        raise UsageError("a location is required")
    point = as_coordinates(text)
    if point is not None:
        return point[0], point[1], text
    return geocode(session, text, hl, gl)


def run(session: Session, spec: QuerySpec) -> QueryResult:
    """Resolve, search, filter cheaply, then enrich only the survivors."""
    lat, lng, label = resolve(session, spec.near, spec.hl, spec.gl)
    result = QueryResult(
        origin={"query": spec.near, "name": label, "lat": lat, "lng": lng},
        spec=spec)

    rows = search(session, spec.query, lat, lng, spec.span_m, spec.limit,
                  spec.hl, spec.gl, spec.max_search_requests)

    # --- free filters, applied before anything costs a request -------------
    if not spec.include_closed_businesses:
        # A permanently closed listing is still a search result. Offering one
        # as "nearby" is worse than returning nothing.
        rows = [p for p in rows if p.get("business_status") != "closed"]

    result.hours_unknown = sum(1 for p in rows if p["status"] == UNKNOWN)
    # `--open-at` SUPERSEDES the open-now filter rather than intersecting with
    # it. `nearby` filters to currently-open by default, so asking it "what is
    # open Monday 9am" at 10pm on a Tuesday intersected two unrelated questions
    # and answered "nothing" — exit 1, which tells a watch loop to keep
    # waiting — while eight breakfast places really were open Monday 9am.
    if spec.open_now and not spec.open_at:
        rows = [p for p in rows if p["status"] == "open"]

    if spec.min_rating is not None:
        rows = [p for p in rows
                if p.get("rating") is not None and p["rating"] >= spec.min_rating]

    # Truncate AFTER the free filters, never before: `search` returns the whole
    # page so the filters see every candidate the response paid for. Anything
    # dropped here is dropped because the caller asked for fewer, and that is
    # counted so a short list never reads as exhaustion.
    result.matched_before_limit = len(rows)
    rows = rows[:spec.limit]

    for place in rows:
        place["straight_km"] = round(haversine_km((lat, lng),
                                                  (place["lat"], place["lng"])), 2)

    # --- paid enrichment, only on what survived ---------------------------
    # Hours and travel are independent lookups over the same list, so they run
    # at the same time rather than one after the other. Measured: 4.1 s -> 2.7 s
    # on a ten-result query, for no extra requests.
    want_hours = spec.want_hours or bool(spec.open_at)

    # Refuse an oversized plan rather than quietly answering a smaller
    # question — the house rule, and the reason `campsites find` has a ceiling.
    # ONE ceiling covering every per-place request, not one per stage: two
    # independent budgets of N let a documented ceiling of 25 spend 50.
    if want_hours or spec.want_travel:
        per_place = (1 if want_hours else 0) + (1 if spec.want_travel else 0)
        check_budget(len(rows) * per_place, spec.max_place_requests,
                     f"enriching {len(rows)} places")

    # Computed BEFORE the closures below capture it. Late binding made the
    # previous order work by accident; it read as a bug and would become one
    # the moment a stage was invoked any earlier.
    stage_budget = (spec.max_place_requests // 2 if (want_hours and spec.want_travel)
                    else spec.max_place_requests)

    stages = []
    if want_hours:
        stages.append(lambda: hours_mod.annotate(
            session, rows, budget=stage_budget))
    if spec.want_travel:
        stages.append(lambda: routing.annotate(
            session, (lat, lng), rows, spec.mode, spec.hl, spec.gl,
            budget=stage_budget))

    if len(stages) == 1:
        stages[0]()
    elif stages:
        with ThreadPoolExecutor(max_workers=len(stages)) as pool:
            list(pool.map(lambda fn: fn(), stages))

    # Count failures on the ENRICHED list, before any filter removes them —
    # the filters drop exactly the places whose lookups failed, so counting
    # afterwards always finds zero and an outage looks like "nothing matched".
    result.network_errors = sum(
        1 for p in rows if p.get("travel_error") or p.get("hours_error"))

    if want_hours:
        # "Google publishes none" only. A place we skipped for budget and a
        # place whose lookup failed are both different statements, and folding
        # either into this one reports an outage as a fact about the place.
        result.week_unknown = sum(1 for p in rows
                                  if not p.get("hours_week_days")
                                  and not p.get("hours_skipped")
                                  and not p.get("hours_error"))
        result.hours_failed = sum(1 for p in rows if p.get("hours_error"))
    # Both stages skip against the same ceiling, so both are counted here.
    result.skipped = sum(1 for p in rows
                         if p.get("hours_skipped") or p.get("travel_skipped"))
    if spec.open_at:
        rows = _filter_open_at(rows, spec.open_at)
    if spec.want_travel:
        result.travel_unknown = sum(1 for p in rows
                                    if p.get("travel_minutes") is None)
        if spec.within_minutes is not None:
            rows = [p for p in rows
                    if p.get("travel_minutes") is not None
                    and p["travel_minutes"] <= spec.within_minutes]

    result.places = _sort(rows, spec.sort)
    return result


def _filter_open_at(rows: list[dict], when: str) -> list[dict]:
    """Keep places known to be open at `when`; unknown is excluded, not closed."""
    weekday, minute = hours_mod.parse_when(when)
    kept = []
    for place in rows:
        days = place.get("hours_week_days")
        verdict = hours_mod.is_open_at(days, weekday, minute) if days else None
        place["open_at_query"] = when
        place["open_at"] = verdict
        if verdict:
            kept.append(place)
    return kept


def _sort(rows: list[dict], how: str) -> list[dict]:
    if how == "travel":
        return sorted(rows, key=lambda p: (p.get("travel_minutes") is None,
                                           p.get("travel_minutes") or 0))
    if how == "rating":
        return sorted(rows, key=lambda p: -(p.get("rating") or 0))
    if how == "distance":
        return sorted(rows, key=lambda p: p.get("straight_km") or 0)
    return rows  # relevance: Google's own order
