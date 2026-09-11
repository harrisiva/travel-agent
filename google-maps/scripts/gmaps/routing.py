"""Routes from Google's directions endpoint: distance, time, live traffic.

`/maps/preview/directions` serves routes with no API key and no cookies, in
four travel modes. Driving carries a live-traffic duration; the others do not
(there is nothing to be congested by on foot).

Two hazards, both of which produce a *plausible wrong answer* rather than an
error, and both of which are guarded below:

1. **Waypoints are `3d`=latitude, `4d`=longitude — the opposite order from the
   place-search pb**, where `2d` is longitude. Swap them and Google routes
   between two other places perfectly happily.
2. **Unrecognised pb tokens are ignored in silence.** A malformed mode block
   does not 400; it returns a driving route. Verified: declaring the wrong
   token count in `!20m<n>` still returned HTTP 200. So the mode is *checked on
   the way back* — Google echoes it at `route[0][0]` — rather than assumed.
"""

from __future__ import annotations

import json
import urllib.parse

from gmaps.errors import NetworkError, ParseError, UsageError
from gmaps.fanout import fan_out
from gmaps.http import Session
from gmaps.model import haversine_km as _haversine_km

DIRECTIONS = "https://www.google.com/maps/preview/directions"

#: CLI name -> the value of `1e` inside the options block, and what Google
#: echoes back at route[0][0].
MODES = {"drive": 0, "bike": 1, "walk": 2, "transit": 3}
MODE_NAMES = {v: k for k, v in MODES.items()}

#: One request per destination per mode, so a result page is a page of
#: requests. Deliberately low on an internal surface with no published limit.
DEFAULT_BUDGET = 25

#: How many of the nearest results get re-fetched individually for live
#: traffic. Named because a caller sizing a budget has to be able to predict
#: the cost of `annotate` without reading it.
DEFAULT_TRAFFIC_TOP_K = 5


def _endpoint(hl: str, gl: str) -> str:
    """The directions URL with the locale knobs percent-encoded.

    They are interpolated into the URL rather than passed through urlencode,
    because the pb that follows must keep its literal "!" delimiters. That put
    raw user input in a query string: a `--gl` of "ca#x" truncated the entire
    pb into a fragment, and Google answered a completely different question
    with HTTP 200 rather than rejecting it.
    """
    return (f"{DIRECTIONS}?authuser=0"
            f"&hl={urllib.parse.quote(hl, safe='')}"
            f"&gl={urllib.parse.quote(gl, safe='')}")


def _pb(origin: tuple[float, float], dest: tuple[float, float], mode: int) -> str:
    mid_lng = (origin[1] + dest[1]) / 2
    mid_lat = (origin[0] + dest[0]) / 2
    return (
        f"!1m4!3m2!3d{origin[0]}!4d{origin[1]}!6e2"
        f"!1m4!3m2!3d{dest[0]}!4d{dest[1]}!6e2"
        f"!3m12!1m3!1d20000!2d{mid_lng}!3d{mid_lat}"
        "!2m3!1f0.0!2f0.0!3f0.0!3m2!1i1024!2i768!4f13.1"
        f"!6m6!20m5!1e{mode}!2e3!5e2!6b1!14b1"
    )


def _traffic(route: list, route_free_flow_s: float | None = None) -> dict | None:
    """`[[in-traffic s, text], null, level, [free-flow s, text], [best, worst, text]]`.

    Returns None when the block cannot be trusted, which is not merely a
    parsing concern. On some routes — verified on Union Station to Billy Bishop,
    an island airport reached by ferry — the block's "in traffic" figure
    describes only the road portion (13 min) while its own free-flow figure and
    the route duration describe the whole journey (30 and 34 min). Reporting 13
    there is not a rounding error; it is twenty minutes of missing ferry, and a
    missed flight.

    The invariant that catches it: **traffic can never make a trip faster than
    free-flow.** When it appears to, the two numbers are measuring different
    journeys and the block is discarded rather than reconciled.

    The block's own free-flow figure is checked first, and the ROUTE's duration
    second. The second check is not redundant: on the ferry route the block's
    `[3]` happened to be present, but nothing guarantees it is — and with it
    absent there would be no reference left to catch a 16-minute figure
    attached to a 35-minute journey.
    """
    try:
        block = route[1][0][0][10]
        in_traffic = block[0][0] / 60
        free_flow = block[3][0] / 60 if block[3] else None
    except (IndexError, TypeError, KeyError):
        return None

    if free_flow is None and route_free_flow_s is not None:
        free_flow = route_free_flow_s / 60
    if free_flow is not None and in_traffic < free_flow * 0.98:
        return None

    # The 2% window exists so ordinary rounding does not discard a good block —
    # but it must not let the inversion through to the payload. Clamping here
    # keeps the reported pair coherent: --full ships both numbers, and
    # "29.5 in traffic, 30.0 free-flow" states that congestion saved 30
    # seconds, which is the incoherence this whole guard exists to prevent.
    if free_flow is not None:
        in_traffic = max(in_traffic, free_flow)

    return {
        "minutes": round(in_traffic, 1),
        "text": block[0][1],
        "free_flow_minutes": round(free_flow, 1) if free_flow is not None else None,
        "level": block[2] if len(block) > 2 else None,
        "range_text": block[4][2] if len(block) > 4 and block[4] and len(block[4]) > 2 else None,
    }


def _transit_times(route: list) -> dict | None:
    """Departure/arrival clock times, present only on transit routes."""
    try:
        block = route[0][5]
        depart, arrive = block[0], block[1]
        return {"depart": depart[2], "arrive": arrive[2], "timezone": depart[1]}
    except (IndexError, TypeError, KeyError):
        return None


def routes(session: Session, origin: tuple[float, float],
           dest: tuple[float, float], mode: str = "drive",
           hl: str = "en", gl: str = "ca") -> list[dict]:
    """Every route Google offers between two points, best first.

    The body is `)]}'\\n` + plain JSON — NOT the chunked `{"c":..,"d":..}`
    envelope the search endpoint uses. Do not reuse the search decoder.
    """
    if mode not in MODES:
        raise UsageError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
    code = MODES[mode]

    body = session.get_text(
        _endpoint(hl, gl), raw_suffix=f"pb={_pb(origin, dest, code)}")

    newline = body.find("\n")
    if newline == -1:
        raise NetworkError("directions response had no anti-hijack prefix")
    try:
        data = json.loads(body[newline + 1:])
    except ValueError as exc:
        raise NetworkError(f"directions response was not JSON: {exc}") from exc

    try:
        candidates = data[0][1] or []
    except (IndexError, TypeError, KeyError):
        # A missing route container is a SHAPE CHANGE, not "no route exists".
        # parse.result_blobs raises here for the same reason: returning [] makes
        # a Google renumbering indistinguishable from a genuine empty result,
        # and lands it on exit 1 — the code that tells a watch loop to wait.
        raise ParseError(
            "no route container in the directions response — the payload shape "
            "has changed, this is not 'no route exists'") from None

    out: list[dict] = []
    wrong_mode: set[int] = set()
    for route in candidates:
        head = route[0] if route else None
        if not head or len(head) < 4 or not head[2] or not head[3]:
            continue
        # The mode Google actually used. Because a malformed options block is
        # ignored rather than rejected, this is the only thing standing between
        # "43 minute walk" and a 13 minute drive reported as one.
        # Fail CLOSED. A non-int here means Google stopped echoing the mode —
        # and a renumbering is exactly the event that would move this slot. The
        # old form degraded to "trust it", which is fail-OPEN in the one place
        # the invariant demands the opposite.
        echoed = head[0]
        if not isinstance(echoed, int) or echoed != code:
            # Resolve the NAME here: `echoed` is whatever Google put in that
            # slot, and a list or dict would make set.add() raise TypeError —
            # turning a guard into a crash. Storing the display string keeps
            # the set hashable and the error message readable.
            wrong_mode.add(MODE_NAMES.get(echoed, repr(echoed))
                           if isinstance(echoed, int) else repr(echoed))
            continue
        out.append({
            "mode": mode,
            "via": head[1],
            "km": round(head[2][0] / 1000, 2),
            "distance_text": head[2][1],
            "free_flow_minutes": round(head[3][0] / 60, 1),
            "duration_text": head[3][1],
            "traffic": _traffic(route, head[3][0] if head[3] else None),
            "transit": _transit_times(route) if mode == "transit" else None,
        })

    # An empty list here means "no route exists", and a caller turns that into
    # exit 1 — "keep waiting". But a list emptied because every route came back
    # in the WRONG mode is a different statement: Google answered a question
    # nobody asked. That is a failure to report, not an absence to believe.
    if not out and wrong_mode:
        raise NetworkError(
            f"asked for {mode} and every route came back as "
            + ", ".join(sorted(wrong_mode))
            + " — the mode was not honoured, so there is no answer to give")
    return out


def _waypoint(point: tuple[float, float]) -> str:
    return f"!1m4!3m2!3d{point[0]}!4d{point[1]}!6e2"


def star_legs(session: Session, origin: tuple[float, float],
              dests: list[tuple[float, float]], mode: str = "drive",
              hl: str = "en", gl: str = "ca") -> list[dict | None]:
    """Distance and time to many destinations in ONE request.

    The endpoint accepts a long waypoint chain and reports every leg. Chaining
    `O, D0, O, D1, O, ...` therefore makes each **even-indexed** leg exactly an
    origin-to-destination route. Verified identical to the standalone two-point
    call for the same pair, at roughly a third of the wall time for four
    destinations.

    **Star legs carry no traffic block** — the per-leg traffic slots come back
    empty. This is free-flow only, which is why `annotate` uses it to rank and
    then re-prices just the handful it will actually report.

    Returns one entry per destination, in order, with None where a leg could
    not be read — so the caller's indices stay aligned with its own list.
    """
    if not dests:
        return []
    if mode not in MODES:
        raise UsageError(f"unknown mode {mode!r}")

    chain: list[tuple[float, float]] = [origin]
    for dest in dests:
        chain += [dest, origin]
    chain = chain[:-1]  # no need to return home after the last stop

    mid_lat = sum(p[0] for p in chain) / len(chain)
    mid_lng = sum(p[1] for p in chain) / len(chain)
    pb = ("".join(_waypoint(p) for p in chain)
          + f"!3m12!1m3!1d20000!2d{mid_lng}!3d{mid_lat}"
          "!2m3!1f0.0!2f0.0!3f0.0!3m2!1i1024!2i768!4f13.1"
          f"!6m6!20m5!1e{MODES[mode]}!2e3!5e2!6b1!14b1")

    body = session.get_text(_endpoint(hl, gl), raw_suffix=f"pb={pb}")
    newline = body.find("\n")
    if newline == -1:
        raise NetworkError("directions response had no anti-hijack prefix")
    try:
        data = json.loads(body[newline + 1:])
        chained = data[0][1][0]
        legs = chained[1]
    except (ValueError, IndexError, TypeError) as exc:
        raise NetworkError(f"could not read leg list: {exc}") from exc

    # The same echo guard `routes()` applies, for the same reason: an
    # unrecognised mode token is IGNORED rather than rejected, so a walk
    # request can come back as a driving chain with HTTP 200. This pass had no
    # guard at all, and it is the pass that prices every place — a 12-minute
    # drive labelled "12 min walk" is the wrong answer twice over.
    # Raising rather than returning None is deliberate: `annotate` falls back
    # to the per-place `routes()` call, which verifies the mode itself, so the
    # answer degrades to slower-but-correct instead of fast-and-wrong.
    # Fail closed here too: a missing or non-int echo discards the star pass
    # and falls back to per-place routes(), which verifies its own mode.
    echoed = chained[0][0] if (chained and chained[0]) else None
    if not isinstance(echoed, int) or echoed != MODES[mode]:
        raise NetworkError(
            f"asked for {mode} and Google answered with "
            f"{MODE_NAMES.get(echoed, echoed)!r}; discarding the star pass")

    # The chain is O,D0,O,D1,...,D_last — 2n waypoints minus the omitted trip
    # home, so exactly 2n-1 legs. Checking the COUNT is what actually catches a
    # dropped or inserted leg.
    #
    # A distance check cannot: in a star chain an odd-numbered shift maps every
    # destination onto its own RETURN leg (Di->O instead of O->Di), which has
    # the same two endpoints and therefore the same distance. Verified against
    # a three-destination fixture — a haversine floor rejected 0 of 3
    # misattributed legs. It is kept below only as an honest sanity floor on a
    # leg that is impossibly short, and claims nothing about alignment.
    expected_legs = 2 * len(dests) - 1
    if len(legs) != expected_legs:
        raise NetworkError(
            f"directions returned {len(legs)} legs for {len(dests)} "
            f"destinations, expected {expected_legs} — the chain is "
            "misaligned, so every distance would belong to the wrong place")

    out: list[dict | None] = []
    for i in range(len(dests)):
        leg = legs[i * 2] if len(legs) > i * 2 else None
        # Sanity floor only: a road route shorter than the straight line is
        # impossible. This does NOT detect misalignment — see above.
        if leg is not None:
            try:
                straight = _haversine_km(origin, dests[i])
                if leg[0][2][0] / 1000 < straight * 0.9:
                    leg = None
            except (IndexError, TypeError, KeyError, ZeroDivisionError):
                leg = None
        try:
            head = leg[0]
            out.append({
                "km": round(head[2][0] / 1000, 2),
                "distance_text": head[2][1],
                "free_flow_minutes": round(head[3][0] / 60, 1),
                "duration_text": head[3][1],
            })
        except (IndexError, TypeError, KeyError):
            out.append(None)
    return out


def annotate(session: Session, origin: tuple[float, float], places: list[dict],
             mode: str = "drive", hl: str = "en", gl: str = "ca",
             budget: int = DEFAULT_BUDGET,
             traffic_top_k: int = DEFAULT_TRAFFIC_TOP_K) -> int:
    """Attach travel time and distance to each place. Returns the number done.

    Two passes, because the cheap call and the accurate call are different
    calls. One star request prices and ranks everything for free; then only the
    `traffic_top_k` nearest — the ones a human will actually be shown — are
    re-fetched individually to pick up live traffic. At ten destinations that is
    six requests instead of ten, and the numbers that get read are still
    traffic-aware.

    `traffic_aware` is set per place, honestly: places priced only by the star
    pass keep the free-flow figure and say so.
    """
    if not places:
        return 0
    targets = places[:budget]
    # The star call is itself a request, so it comes out of the same budget.
    # Charging only the re-pricing let a documented ceiling of 20 issue 21 when
    # the star pass failed and every place had to be priced individually.
    # `budget` is TOTAL requests for this stage, and the star call is one of
    # them — hence the -1. The places this leaves unpriced are marked skipped,
    # and `cli._outcome` treats "we never looked" as distinct from "nothing
    # matched" so they cannot silently become exit 1.
    remaining = max(0, budget - 1)

    try:
        legs = star_legs(session, origin, [(p["lat"], p["lng"]) for p in targets],
                         mode, hl, gl)
    except (NetworkError, UsageError):
        legs = [None] * len(targets)

    for place, leg in zip(targets, legs):
        place["travel_mode"] = mode
        place["traffic_aware"] = False
        if leg:
            place["travel_km"] = leg["km"]
            place["travel_minutes"] = leg["free_flow_minutes"]
            place["free_flow_minutes"] = leg["free_flow_minutes"]

    # Re-price the ones that will be reported, nearest first, to pick up
    # traffic — concurrently, or the second pass costs more than it saves.
    ranked = sorted((p for p in targets if p.get("travel_minutes") is not None),
                    key=lambda p: p["travel_minutes"])
    unpriced = [p for p in targets if p.get("travel_minutes") is None]
    repriced = (unpriced + ranked[:traffic_top_k])[:remaining]
    if repriced:
        fan_out(repriced, lambda pl: _price_one(session, origin, pl, mode, hl, gl),
                budget=len(repriced))

    # A place the star pass could not price and the budget could not reach is
    # unknown, and must say so rather than sit there with a null time that
    # reads as "no route exists".
    # Identity, not equality: two places can be equal dicts, and `in` would
    # then mark the wrong one as reached.
    attempted = {id(p) for p in repriced}
    for place in unpriced:
        if id(place) not in attempted:
            place["travel_skipped"] = "over the request budget"

    return len(targets)


def _price_one(session: Session, origin: tuple[float, float], target: dict,
               mode: str, hl: str, gl: str) -> None:
    """One two-point call, which unlike a star leg does carry live traffic."""
    try:
        found = routes(session, origin, (target["lat"], target["lng"]),
                       mode, hl, gl)
    except Exception as exc:  # noqa: BLE001
        # ANY failure — see the matching comment in hours.annotate. A silently
        # dropped routing failure renders as "no route found", which is a claim
        # about the road network rather than about our connection.
        target["travel_error"] = f"{type(exc).__name__}: {exc}"
        return
    if not found:
        return

    best = found[0]
    traffic = best["traffic"]
    target["travel_mode"] = mode
    target["travel_km"] = best["km"]
    target["travel_minutes"] = (traffic["minutes"] if traffic
                                else best["free_flow_minutes"])
    # Report free-flow from the SAME source as the in-traffic figure. The
    # traffic block carries its own baseline (block[3]) and that is what the
    # "traffic cannot be faster than free-flow" guard checked; the route head
    # is a separate Google estimate. Mixing them produced pairs like
    # "5.5 min in traffic, 5.9 min free-flow" — not wrong enough to trip the
    # guard, but incoherent to anyone reading both numbers.
    target["free_flow_minutes"] = (
        traffic["free_flow_minutes"] if traffic and traffic.get("free_flow_minutes")
        is not None else best["free_flow_minutes"])
    target["traffic_aware"] = bool(traffic)
    target["traffic_range"] = traffic["range_text"] if traffic else None
    target["route_via"] = best["via"]
    if best["transit"]:
        target["transit"] = best["transit"]
