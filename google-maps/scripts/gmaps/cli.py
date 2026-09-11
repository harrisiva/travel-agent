"""Argument parsing, dispatch, and the exception-to-exit-code mapping.

Business logic lives in `query.py`, rendering in `render.py`. This file is the
boundary: it turns flags into a `QuerySpec`, and turns exceptions into the exit
codes documented in `errors.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from gmaps import hours as hours_mod
from gmaps import render, routing
from gmaps.errors import (EMPTY, FOUND, INTERRUPTED, NETWORK, USAGE,
                          GmapsError, UsageError)
from gmaps.fanout import check_budget
from gmaps.fields import project
from gmaps.http import new_session
from gmaps.model import haversine_km
from gmaps.query import QuerySpec, as_coordinates, resolve, run

MODES = tuple(routing.MODES)


#: Serialised bytes above which we drop results rather than emit a document
#: that the caller's output cap will corrupt. Sits below the ~30 KB Bash cap
#: with room for the surrounding tool-result framing.
MAX_JSON_BYTES = 25000


def _emit(payload, as_json: bool, render_fn) -> None:
    if not as_json:
        render_fn(payload)
        return

    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_JSON_BYTES and isinstance(payload, dict) \
            and isinstance(payload.get("results"), list):
        # Shed whole results until it fits. Truncating the TEXT would hand back
        # invalid JSON, which is what the caller's own cap would have done —
        # the point is to return a short valid document and say so.
        rows = payload["results"]
        matched = len(rows)
        while rows and len(text) > MAX_JSON_BYTES:
            # Shed ~20% each pass, but always at least one, and never skip
            # straight past 1 — a two-row payload one byte over should come
            # back as one row, not none.
            rows = rows[:max(0, len(rows) - max(1, len(rows) // 5))]
            payload = dict(payload, results=rows, shown=len(rows),
                           matched=matched, output_truncated=True,
                           count=len(rows))  # count must agree with the array
            text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write(text + "\n")


def _validate(args) -> None:
    """Reject values that cannot mean anything, rather than answering oddly."""
    if getattr(args, "span", 1) <= 0:
        raise UsageError("--span must be a positive number of metres")
    if getattr(args, "limit", 1) < 1:
        raise UsageError("--limit must be at least 1")
    if args.min_rating is not None and not (0 <= args.min_rating <= 5):
        raise UsageError("--min-rating must be between 0 and 5")
    if getattr(args, "within", None) is not None and args.within <= 0:
        raise UsageError("--within must be a positive number of minutes")
    if not (getattr(args, "query", "") or "").strip():
        raise UsageError("--query cannot be blank")
    # A ceiling of 0 issued no requests at all and the empty result came back
    # as exit 1 — "the query worked, nothing matched" — from a flag that meant
    # the query never ran. A bad flag is exit 2; only a real search can be
    # exit 1.
    if getattr(args, "max_requests", 1) < 1:
        raise UsageError("--max-requests must be at least 1")
    if getattr(args, "max_place_requests", 1) < 1:
        raise UsageError("--max-place-requests must be at least 1")
    if getattr(args, "concurrency", None) is not None and args.concurrency < 1:
        raise UsageError("--concurrency must be at least 1")


def _spec(args) -> QuerySpec:
    """One place where flags become a query, so the two search commands agree."""
    _validate(args)
    return QuerySpec(
        near=args.near, query=args.query, span_m=args.span, limit=args.limit,
        min_rating=args.min_rating,
        open_now=getattr(args, "open_now", False),
        include_closed_businesses=getattr(args, "include_permanently_closed", False),
        want_hours=getattr(args, "with_hours", False),
        open_at=getattr(args, "open_at", None),
        mode=getattr(args, "mode", "drive"),
        want_travel=getattr(args, "_travel", False),
        within_minutes=getattr(args, "within", None),
        sort=getattr(args, "sort", "relevance"),
        hl=args.hl, gl=args.gl,
        max_search_requests=args.max_requests,
        max_place_requests=args.max_place_requests,
    )


def _payload(result, full: bool = False) -> dict:
    spec = result.spec
    return {
        "from": result.origin,
        "query": spec.query,
        "mode": spec.mode if spec.want_travel else None,
        # The EFFECTIVE filter, not the flag: --open-at supersedes open-now,
        # and a payload that claims both were applied misreports the answer.
        "open_now": spec.open_now and not spec.open_at,
        "open_at": spec.open_at,
        "hours_unknown": result.hours_unknown,
        "week_unknown": result.week_unknown,
        # Distinct from week_unknown on purpose: a failed lookup is not a
        # place without published hours.
        "hours_failed": result.hours_failed,
        # Named for what it counts: places skipped in EITHER enrichment stage
        # against the one per-place ceiling. The per-place `hours_skipped`
        # field on a result is the hours-only statement and keeps its name.
        "skipped": result.skipped,
        "network_errors": result.network_errors,
        "travel_unknown": result.travel_unknown,
        "traffic_aware": bool(result.places) and all(
            p.get("traffic_aware") for p in result.places),
        "matched_before_limit": result.matched_before_limit,
        "count": len(result.places),
        "results": [project(p, full) for p in result.places],
    }


def _outcome(result) -> int:
    """FOUND, EMPTY, or NETWORK — and never EMPTY because of an outage.

    An empty list has two very different causes: nothing matched, or the
    lookups that would have populated it failed. Exit 1 tells a watch loop to
    keep waiting, so reporting an outage as 1 makes it wait forever. The
    failure is already known by this point; this is where it stops being
    discarded.
    """
    if result.places:
        return FOUND
    if result.network_errors:
        return NETWORK
    if result.skipped:
        # An empty list where places were never looked up is not "nothing
        # matched". Exit 1 tells a watch loop to keep waiting and tells a
        # person the search was exhaustive; neither is true when a ceiling
        # stopped us short. Exit 2 points at the flag that fixes it.
        return USAGE
    return EMPTY


def cmd_geocode(session, args) -> int:
    lat, lng, label = resolve(session, args.where, args.hl, args.gl)
    _emit({"query": args.where, "name": label, "lat": lat, "lng": lng},
          args.json, render.location)
    return FOUND


def cmd_search(session, args) -> int:
    args._travel = False
    result = run(session, _spec(args))
    _emit(_payload(result, getattr(args, "full", False)), args.json,
          lambda p: render.places(p["results"], result))
    return _outcome(result)


def cmd_nearby(session, args) -> int:
    args._travel = True
    if args.sort is None:
        args.sort = "travel"
    result = run(session, _spec(args))
    _emit(_payload(result, getattr(args, "full", False)), args.json,
          lambda p: render.places(p["results"], result))
    return _outcome(result)


def cmd_hours(session, args) -> int:
    from gmaps.places import search
    where = args.near or args.place
    lat, lng, _ = resolve(session, where, args.hl, args.gl)
    rows = search(session, args.place, lat, lng, 5000, 1, args.hl, args.gl, 1)
    if not rows:
        # On stdout, not just stderr. Under --json an empty stdout is the exact
        # ambiguity `_fail` exists to remove: a caller cannot tell "nothing
        # matched" from "it broke" without parsing stderr, and every other
        # command answers a non-zero outcome on stdout.
        return _fail(f"no place matching {args.place!r}", EMPTY, args.json)
    place = rows[0]
    hours_mod.annotate(session, [place])
    # Google answers a name query with its best match, which is not always the
    # place that was asked for — "Zzqqx Nonexistent Bistro" came back as "Muse
    # Bistro + Bar" with a full schedule and exit 0. The result is still the
    # useful one to return, but the caller has to be able to see that it is a
    # nearest match rather than the thing it named.
    place["matched_query"] = args.place
    _emit(place, args.json, render.place)
    # An hours lookup that failed is not a place with no hours. Exit 3 so a
    # caller retries rather than recording "this place publishes nothing".
    return NETWORK if place.get("hours_error") else FOUND


def cmd_travel(session, args) -> int:
    if args.max_place_requests < 1:
        raise UsageError("--max-place-requests must be at least 1")
    # ONE ceiling over the whole command, not one per phase. Each free-text
    # --to costs a geocode BEFORE any routing happens, and routing then costs a
    # star request plus a traffic re-check for the nearest few. Charging only
    # the geocodes let 20 destinations spend 26 against a ceiling of 25.
    geocodes = sum(1 for t in [args.origin] + list(args.to)
                   if as_coordinates(t) is None)
    # One star request prices every destination, but each one that needs an
    # individual traffic re-check is another request — and annotate() truncates
    # past the budget rather than refusing, so the ceiling has to know the real
    # destination count or 40 --to values silently become 15 "no route found".
    routing_cost = 1 + len(args.to)
    check_budget(geocodes + routing_cost, args.max_place_requests,
                 f"routing to {len(args.to)} destinations")

    olat, olng, olabel = resolve(session, args.origin, args.hl, args.gl)
    dests = []
    for text in args.to:
        dlat, dlng, dlabel = resolve(session, text, args.hl, args.gl)
        dests.append({"query": text, "name": dlabel, "lat": dlat, "lng": dlng,
                      "straight_km": round(haversine_km((olat, olng),
                                                        (dlat, dlng)), 2)})
    routing.annotate(session, (olat, olng), dests, args.mode, args.hl, args.gl,
                     budget=max(1, args.max_place_requests - geocodes))
    payload = {"from": {"query": args.origin, "name": olabel,
                        "lat": olat, "lng": olng},
               "mode": args.mode,
               # all(), not any(): a mixed set is not "traffic aware", and the
               # per-place flag carries the truth either way.
               "traffic_aware": bool(dests) and all(
                   d.get("traffic_aware") for d in dests),
               "count": len(dests), "results": dests}
    _emit(payload, args.json, render.routes)
    if any(d.get("travel_minutes") is not None for d in dests):
        return FOUND
    # Every destination unrouted: an outage, not "there is no route".
    return NETWORK if any(d.get("travel_error") for d in dests) else EMPTY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gmaps",
        description="Read-only Google Maps: places, opening hours, travel times.")
    parser.add_argument("--json", action="store_true",
                        help="structured output for chaining")
    parser.add_argument("--hl", default="en", help="language (default en)")
    parser.add_argument("--gl", default="ca", help="point of sale (default ca)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_locale(sp):
        """--hl/--gl are global, but every documented example puts flags after
        the subcommand. Accept both positions, as --json already does."""
        sp.add_argument("--hl", default=argparse.SUPPRESS, help="language")
        sp.add_argument("--gl", default=argparse.SUPPRESS,
                        help="point of sale; changes WHICH places surface")

    def add_json(sp):
        """--json is global, but a caller will naturally append it after the
        subcommand. Accept both; SUPPRESS stops the subparser copy from
        overriding a global --json back to False when it is absent."""
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="structured output for chaining")

    def add_search_flags(sp):
        sp.add_argument("--near", required=True, help="'lat,lng' or free text")
        sp.add_argument("--query", default="restaurants", help="what to look for")
        sp.add_argument("--span", type=int, default=10000,
                        help="search radius in metres (default 10000)")
        sp.add_argument("--limit", type=int, default=20)
        sp.add_argument("--min-rating", type=float)
        sp.add_argument("--with-hours", action="store_true",
                        help="attach the full week (one request per place)")
        sp.add_argument("--open-at", metavar="WHEN",
                        help="only places open then, in the place's local time: "
                             "'Fri 20:00', 'Friday 8pm' or '2026-10-04 21:00'")
        sp.add_argument("--include-permanently-closed", action="store_true",
                        help="keep listings Google marks as closed down")
        sp.add_argument("--max-requests", type=int, default=5,
                        help="ceiling on search pages (default 5)")
        sp.add_argument("--max-place-requests", type=int, default=25,
                        help="ceiling on per-place lookups (default 25)")
        sp.add_argument("--full", action="store_true",
                        help="every field, including ids, coordinates and URLs "
                             "(the default payload is trimmed to fit output caps)")
        sp.add_argument("--concurrency", type=int, default=None,
                        metavar="N", help="parallel requests (default 12); "
                                          "lower it if you see rate limiting")

    g = sub.add_parser("geocode", help="a place name or address to coordinates")
    g.add_argument("where")
    g.set_defaults(func=cmd_geocode)
    add_json(g); add_locale(g)

    s = sub.add_parser(
        "search", help="places near a location, no travel times",
        epilog="search returns EVERYTHING unless you pass --open-now. "
               "nearby is the opposite: it filters to currently-open places "
               "and needs --include-closed to stop. Same query, different "
               "result set — check which one you are running.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_search_flags(s)
    s.add_argument("--open-now", action="store_true",
                   help="only places open right now")
    s.add_argument("--sort", choices=("relevance", "rating", "distance"),
                   default="relevance")
    s.set_defaults(func=cmd_search, mode="drive")
    add_json(s); add_locale(s)

    n = sub.add_parser(
        "nearby", help="what is open around here, ranked by travel time",
        epilog="nearby filters to currently-OPEN places by default; pass "
               "--include-closed to keep the rest. (search is the opposite — "
               "it keeps everything unless you pass --open-now.) "
               "--mode changes the answer completely: 8 minutes is a very "
               "different distance driving and walking.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_search_flags(n)
    n.add_argument("--mode", choices=MODES, default="drive",
                   help="how you are travelling (default drive)")
    n.add_argument("--within", type=float, metavar="MIN",
                   help="drop anything further than this many minutes away")
    n.add_argument("--include-closed", dest="open_now", action="store_false",
                   help="do not filter to currently-open places")
    # Default is None, not "relevance": nearby sorts by travel time unless
    # asked otherwise, and a sentinel is the only way to tell "the user chose
    # relevance" from "the user chose nothing".
    n.add_argument("--sort", choices=("travel", "rating", "distance", "relevance"),
                   default=None)
    n.set_defaults(func=cmd_nearby, open_now=True)
    # Fewer than search: nobody picks dinner from 20, and this is the
    # multiplier on every per-place request and every output byte.
    n.set_defaults(limit=8)
    add_json(n); add_locale(n)

    h = sub.add_parser("hours", help="full weekly opening hours for one place")
    h.add_argument("place")
    h.add_argument("--near", help="disambiguate by location, e.g. 'Toronto'")
    h.set_defaults(func=cmd_hours, max_place_requests=25)
    add_json(h); add_locale(h)

    t = sub.add_parser("travel",
                       help="time and distance to one or more places")
    t.add_argument("--from", dest="origin", required=True)
    t.add_argument("--to", action="append", required=True,
                   help="repeatable; all destinations are priced in one "
                        "request, then the nearest few re-checked for traffic")
    t.add_argument("--mode", choices=MODES, default="drive")
    t.add_argument("--max-place-requests", type=int, default=25)
    t.set_defaults(func=cmd_travel)
    add_json(t); add_locale(t)

    return parser


#: Options whose value may legitimately begin with "-" — a southern-hemisphere
#: latitude or a western longitude.
_COORD_OPTIONS = ("--near", "--from", "--to", "--min-rating", "--within",
                  "--span", "--limit")

_NUMERIC_VALUE = re.compile(r"^-\d")


def _join_negative_values(argv: list[str]) -> list[str]:
    """Rewrite `--near -33.9,151.2` to `--near=-33.9,151.2`.

    argparse reads any token starting with "-" as an option, so a negative
    coordinate is parsed as an unknown flag and the command dies with a usage
    error. That silently rules out the whole southern hemisphere in the
    space-separated form everyone actually types — including the form used in
    this skill's own documentation.
    """
    out: list[str] = []
    skip = False
    for i, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if (token in _COORD_OPTIONS and i + 1 < len(argv)
                and _NUMERIC_VALUE.match(argv[i + 1])):
            out.append(f"{token}={argv[i + 1]}")
            skip = True
        else:
            out.append(token)
    return out


def _force_utf8_output() -> None:
    """Make stdout able to carry the answer, whatever the container's locale.

    Place names carry accents, the rating column prints a star and transit
    lines print an arrow. On a POSIX/C-locale container — a very ordinary place
    for this skill to run — stdout defaults to ASCII and *every* search died
    with `UnicodeEncodeError`, reported as exit 3. The data was fine; only the
    encoder was not. `errors="replace"` is the belt to the utf-8 braces: a
    stream that cannot be reconfigured must still not take down the answer.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # already wrapped (a test buffer) or not reconfigurable


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    argv = _join_negative_values(list(argv) if argv is not None else sys.argv[1:])
    wants_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse writes its own usage text and exits. Under --json that
        # leaves stdout empty, which is the very ambiguity _fail exists to
        # prevent: the caller cannot tell a usage error from "nothing matched".
        code = exc.code if isinstance(exc.code, int) else USAGE
        if wants_json and code != 0:
            json.dump({"ok": False, "exit_code": USAGE,
                       "error": "invalid arguments; run --help"},
                      sys.stdout, indent=2)
            sys.stdout.write("\n")
        return code
    as_json = getattr(args, "json", False)
    if getattr(args, "concurrency", None):
        from gmaps import fanout
        fanout.DEFAULT_WORKERS = max(1, args.concurrency)
    session = new_session()
    try:
        return args.func(session, args)
    except GmapsError as exc:
        return _fail(exc, exc.exit_code, as_json)
    except KeyboardInterrupt:
        return INTERRUPTED
    except Exception as exc:  # noqa: BLE001
        # Anything unforeseen — a shifted index, a shape change — is reported
        # as a failure, NEVER as exit 1. Exit 1 means "the query worked and
        # nothing matched", and a watch loop treats it as "keep waiting".
        return _fail(f"unexpected {type(exc).__name__}: {exc}", NETWORK, as_json)


def _fail(message, code: int, as_json: bool) -> int:
    """Report an error on the same channel the caller asked for.

    Under --json a bare stderr line leaves stdout empty, so a caller cannot
    tell "nothing matched" from "it broke" without parsing stderr.
    """
    text = str(message)
    if as_json:
        json.dump({"ok": False, "exit_code": code, "error": text},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    print(f"error: {text}", file=sys.stderr)
    return code
