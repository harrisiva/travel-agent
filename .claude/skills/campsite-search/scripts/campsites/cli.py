"""Command line interface: python3 -m campsites <command> ...

Exit codes (consistent across human and --json output):
    0  found what was asked for
    1  query succeeded, nothing available
    2  usage / lookup error (bad park name, ambiguous match, unknown provider)
    3  network or API error
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from .cache import Cache
from .camis import CamisClient, SpanTooLongError
from .http import CamisHTTPError
from .model import Availability
from .providers import CAMIS_PROVIDERS, OTHER_PROVIDERS

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _fix_console() -> None:
    """Windows consoles default to cp1252 and raise on Indigenous place names
    like "Sx̱ótsaqel" or French accents. Force UTF-8 with replacement."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a date (expected YYYY-MM-DD)")


def _attr(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"--attr expects Name=Value (e.g. 'Service Type=Electric'), got {s!r}"
        )
    key, _, value = s.partition("=")
    return key.strip(), value.strip()


def _client(args) -> CamisClient:
    return CamisClient(
        args.provider,
        use_cache=not getattr(args, "no_cache", False),
        max_requests=getattr(args, "max_requests", 200),
    )


SCHEMA_VERSION = 1


def _emit(args, payload: dict, found: bool) -> int:
    if args.json:
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": True, **payload},
            indent=2, ensure_ascii=False,
        ))
    return EXIT_OK if found else EXIT_NONE


def _fail(args, code: int, message: str) -> int:
    """A --json run emits JSON even on failure, so callers never have to parse
    stderr to distinguish "nothing available" from "the command was wrong"."""
    if getattr(args, "json", False):
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": False,
             "exit_code": code, "error": message},
            indent=2, ensure_ascii=False,
        ))
    print(f"error: {message}", file=sys.stderr)
    return code


# ---------- commands ----------


def cmd_providers(args) -> int:
    if args.json:
        return _emit(
            args,
            {
                "camis": [
                    {"key": p.key, "host": p.host, "name": p.name}
                    for p in CAMIS_PROVIDERS.values()
                ],
                "unsupported": OTHER_PROVIDERS,
            },
            True,
        )
    print("Camis5 (supported):")
    for p in CAMIS_PROVIDERS.values():
        print(f"  {p.key:<14} {p.host:<32} {p.name}")
    print("\nOther platforms (not supported):")
    for key, note in OTHER_PROVIDERS.items():
        print(f"  {key:<14} {note}")
    return EXIT_OK


def cmd_parks(args) -> int:
    client = _client(args)
    parks = client.parks()
    if args.search:
        q = args.search.lower()
        parks = [p for p in parks if q in p.name.lower()]
    if args.json:
        return _emit(
            args,
            {"provider": client.host, "parks": [{"id": p.id, "name": p.name} for p in parks]},
            bool(parks),
        )
    for p in parks:
        print(f"{p.id:>12}  {p.name}")
    print(f"\n{len(parks)} parks on {client.host}", file=sys.stderr)
    return EXIT_OK if parks else EXIT_NONE


def cmd_equipment(args) -> int:
    client = _client(args)
    equip, cats = client.equipment(), client.booking_categories()
    if args.json:
        return _emit(
            args,
            {
                "equipment": [
                    {
                        "category_id": e.category_id,
                        "sub_category_id": e.sub_category_id,
                        "name": e.name,
                        "category": e.category_name,
                    }
                    for e in equip
                ],
                "booking_categories": [{"id": c.id, "name": c.name} for c in cats],
            },
            True,
        )
    print("Equipment (IDs are tenant-specific — never hardcode them):")
    for e in equip:
        print(f"  {e.category_id:>7} / {e.sub_category_id:<8} {e.name}  [{e.category_name}]")
    print("\nBooking categories (--booking-category accepts the name or the id):")
    for c in cats:
        print(f"  {c.id:>7}  {c.name}")
    return EXIT_OK


def cmd_attrs(args) -> int:
    client = _client(args)
    park = client.find_park(args.park)
    facets = client.facets(park.id)
    if args.json:
        return _emit(args, {"park": park.name, "facets": facets}, bool(facets))
    print(f"{park.name} — filterable site attributes (use with --attr 'Name=Value')\n")
    for name, values in facets.items():
        top = ", ".join(f"{v} ({n})" for v, n in list(values.items())[:6])
        print(f"  {name:<26} {top}")
    return EXIT_OK if facets else EXIT_NONE


def cmd_window(args) -> int:
    client = _client(args)
    park = client.find_park(args.park)
    windows = client.booking_window(park.id)
    if args.json:
        return _emit(args, {"park": park.name, "windows": windows}, bool(windows))
    print(f"{park.name} ({park.timezone})")
    for w in windows:
        print(f"  {w['schedule'] or '(unnamed)'}: {w['start']} .. {w['end']}   opens: {w['go_live'] or '—'}")
    if not windows:
        print("  no upcoming seasons on file")
    return EXIT_OK if windows else EXIT_NONE


def cmd_horizon(args) -> int:
    client = _client(args)
    data = client.horizon(
        args.park, equipment=args.equipment, party_size=args.party,
        booking_category=args.booking_category,
    )
    if args.json:
        return _emit(args, data, bool(data.get("last_date_in_booking_window")))
    print(f"{data['park']}  (probed: {', '.join(data.get('probed_maps') or ['—'])})")
    print(f"  booking window reaches : {data['last_date_in_booking_window']} "
          f"({data['days_out']} days out)" if data["days_out"] else "  booking window: none detected")
    print(f"  last date with a free site: {data['last_date_with_availability'] or '—'}")
    for w in data.get("booking_windows", []):
        print(f"    season {w['start']} .. {w['end']}   opens: {w['go_live'] or '—'}")
    return EXIT_OK if data.get("last_date_in_booking_window") else EXIT_NONE


def cmd_site(args) -> int:
    client = _client(args)
    info, map_name, cal = client.site_calendar(
        args.park, args.site, args.start, args.end,
        equipment=args.equipment, party_size=args.party,
        booking_category=args.booking_category,
    )
    free = [d for d, s in cal if s is Availability.AVAILABLE]
    if args.json:
        return _emit(
            args,
            {
                "site": info.name, "area": map_name, "description": info.description,
                "max_capacity": info.max_capacity, "attributes": info.attributes,
                "calendar": [{"date": d.isoformat(), "status": s.name} for d, s in cal],
                "free_nights": [d.isoformat() for d in free],
            },
            bool(free),
        )
    print(f"Site {info.name} — {map_name}")
    if info.description:
        print(f"  {info.description}")
    if info.attributes:
        keys = ("Service Type", "Electrical Service", "Privacy", "Site Shade", "Pull-through")
        shown = [f"{k}: {info.attributes[k]}" for k in keys if k in info.attributes]
        if shown:
            print(f"  {' | '.join(shown)}")
    print()
    for d, s in cal:
        mark = "free" if s is Availability.AVAILABLE else s.name.lower()
        print(f"  {d}  {d.strftime('%a')}  {mark}")
    print(f"\n{len(free)} free nights of {len(cal)} dates.")
    return EXIT_OK if free else EXIT_NONE


def cmd_search(args) -> int:
    client = _client(args)
    result = client.search(
        args.park, args.start, args.end, equipment=args.equipment,
        party_size=args.party, booking_category=args.booking_category,
        maps_filter=args.map, attrs=dict(args.attr or []), types=args.types,
    )
    available, partial = result.available, result.partial
    # A partial counts as a hit only when the caller asked for partials —
    # otherwise a watch loop would fire the moment it was set up.
    found = bool(available or (args.include_partial and partial))
    if args.json:
        return _emit(
            args,
            {
                "provider": client.host, "park": result.park.name,
                "start": str(result.start), "end": str(result.end),
                "nights": result.nights, "equipment": result.equipment.name,
                "party_size": result.party_size, "requests": result.requests,
                "available": [_site_json(s) for s in available],
                "partial": [
                    dict(_site_json(s), free_nights=s.free_nights,
                         nights=[n.name for n in s.nights])
                    for s in partial
                ],
                "counts": result.histogram(),
            },
            found,
        )

    print(f"{result.park.name} — {result.start} to {result.end} "
          f"({result.nights} nights) — {result.equipment.name}, party of {result.party_size}")
    if not available:
        print("\nNo sites available for the full stay.")
    else:
        _print_grouped(available, args.limit)
        print(f"\n{len(available)} sites available of {len(result.sites)} checked.")
    if partial and (args.include_partial or not available):
        print(f"\n{len(partial)} sites are free for SOME of those nights:")
        if args.include_partial:
            for s in partial[: args.limit]:
                nights = " ".join("." if n.bookable else "x" for n in s.nights)
                print(f"    {s.map_name} / site {s.name}  [{nights}]  {s.free_nights}/{result.nights} nights")
            if len(partial) > args.limit:
                print(f"    ... and {len(partial) - args.limit} more (raise --limit)")
        else:
            print("    re-run with --include-partial to list them")
    if args.verbose:
        print(f"\nstatus counts: {result.histogram()}  ({result.requests} requests)",
              file=sys.stderr)
    return EXIT_OK if found else EXIT_NONE


def cmd_sweep(args) -> int:
    client = _client(args)
    weekdays = [WEEKDAYS[d] for d in args.weekday] if args.weekday else None
    openings = client.sweep(
        args.park, args.start, args.end, nights=args.nights,
        equipment=args.equipment, party_size=args.party,
        booking_category=args.booking_category, maps_filter=args.map,
        attrs=dict(args.attr or []), types=args.types,
        weekends_only=args.weekends, weekdays=weekdays,
    )
    by_date: dict[date, list] = {}
    for o in openings:
        by_date.setdefault(o.start, []).append(o)

    if args.json:
        return _emit(
            args,
            {
                "provider": client.host, "park": args.park,
                "window": {"start": str(args.start), "end": str(args.end)},
                "nights": args.nights, "requests": client.requests,
                "dates": [
                    {
                        "check_in": d.isoformat(),
                        "check_out": (d + timedelta(days=args.nights)).isoformat(),
                        "weekday": d.strftime("%a"),
                        "site_count": len(v),
                        "sites": [
                            {"site": o.site_name, "area": o.map_name, "resource_id": o.site_id}
                            for o in v[: args.limit]
                        ],
                    }
                    for d, v in sorted(by_date.items())
                ],
            },
            bool(openings),
        )

    label = f"{args.nights}-night stays"
    if args.weekends:
        label += " covering a Fri/Sat night"
    print(f"{args.park} — {label} between {args.start} and {args.end}")
    if not openings:
        print("\nNothing available.")
        return EXIT_NONE
    for d, v in sorted(by_date.items())[: args.limit]:
        out = d + timedelta(days=args.nights)
        names = ", ".join(f"{o.map_name}/{o.site_name}" for o in v[:6])
        more = f" +{len(v) - 6} more" if len(v) > 6 else ""
        print(f"\n  {d} {d.strftime('%a')} -> {out}   {len(v)} sites")
        print(f"    {names}{more}")
    if len(by_date) > args.limit:
        print(f"\n  ... and {len(by_date) - args.limit} more check-in dates (raise --limit)")
    print(f"\n{len(openings)} openings across {len(by_date)} check-in dates "
          f"({client.requests} requests).")
    return EXIT_OK


def cmd_find(args) -> int:
    """Sweep every park matching a pattern — "anything in Algonquin"."""
    client = _client(args)
    parks = client.find_parks(args.park)
    weekdays = [WEEKDAYS[d] for d in args.weekday] if args.weekday else None

    planned = client.plan_requests(parks)
    if planned > args.max_requests:
        return _fail(args, EXIT_USAGE,
            f"{len(parks)} parks match {args.park!r} and would cost {planned} "
            f"requests, over the ceiling of {args.max_requests}. Narrow the "
            f"pattern or raise --max-requests.")
    if not args.json:
        print(f"Searching {len(parks)} parks matching {args.park!r} "
              f"(~{planned} requests)...", file=sys.stderr)

    found, errors = client.sweep_many(
        parks, args.start, args.end, nights=args.nights,
        equipment=args.equipment, party_size=args.party,
        booking_category=args.booking_category, attrs=dict(args.attr or []),
        types=args.types, weekends_only=args.weekends, weekdays=weekdays,
        on_progress=None if args.json else
            (lambda p: print(f"  · {p.name}", file=sys.stderr)),
    )
    by_park = {p.id: p for p in parks}
    ranked = sorted(found.items(), key=lambda kv: -len(kv[1]))

    if args.json:
        return _emit(args, {
            "provider": client.host, "pattern": args.park,
            "parks_searched": len(parks), "requests": client.requests,
            "nights": args.nights,
            "window": {"start": str(args.start), "end": str(args.end)},
            "parks": [
                {
                    "park": by_park[pid].name, "park_id": pid,
                    "opening_count": len(ops),
                    "check_in_dates": sorted({o.start.isoformat() for o in ops}),
                    "sites": [
                        {"site": o.site_name, "area": o.map_name,
                         "check_in": o.start.isoformat(), "resource_id": o.site_id}
                        for o in ops[: args.limit]
                    ],
                }
                for pid, ops in ranked
            ],
            "skipped": [
                {"park": by_park[pid].name, "reason": r} for pid, r in errors.items()
            ],
        }, bool(found))

    if not found:
        print(f"\nNothing available across {len(parks)} parks.")
    else:
        print(f"\n{args.nights}-night stays, {args.start} to {args.end}\n")
        for pid, ops in ranked[: args.limit]:
            dates = sorted({o.start for o in ops})
            shown = ", ".join(d.strftime("%b %-d") for d in dates[:8])
            more = f" +{len(dates) - 8} more" if len(dates) > 8 else ""
            print(f"  {by_park[pid].name}")
            print(f"    {len(ops)} openings on {len(dates)} dates: {shown}{more}")
    if errors and args.verbose:
        print(f"\nskipped {len(errors)} parks:", file=sys.stderr)
        for pid, r in errors.items():
            print(f"  {by_park[pid].name}: {r[:90]}", file=sys.stderr)
    elif errors:
        print(f"\n({len(errors)} parks skipped — rerun with -v for why)")
    print(f"\n{sum(len(v) for v in found.values())} openings across "
          f"{len(found)} of {len(parks)} parks ({client.requests} requests).")
    return EXIT_OK if found else EXIT_NONE


def cmd_stays(args) -> int:
    """What can actually be booked here — cabins, yurts, oTENTiks, huts."""
    client = _client(args)
    cats = client.resource_categories()
    booking = client.booking_categories()
    park = client.find_park(args.park) if args.park else None
    counts = client.stay_types(park.id if park else None) if park else {}

    if args.json:
        return _emit(args, {
            "provider": client.host,
            "park": park.name if park else None,
            "booking_categories": [
                {"id": c.id, "name": c.name,
                 "alias": _alias_for(c.name)} for c in booking
            ],
            "stay_types": [
                {"name": c.name, "type": c.type.name.lower(), "roofed": c.roofed,
                 "count_in_park": counts.get(c.name)}
                for c in sorted(cats.values(), key=lambda x: (x.type, x.name))
            ],
        }, True)

    print(f"{client.host}" + (f" — {park.name}" if park else ""))
    print("\nBooking categories (pass to --booking-category):")
    for c in booking:
        alias = _alias_for(c.name)
        tag = f"   [alias: {alias}]" if alias else ""
        print(f"  {c.id:>3}  {c.name}{tag}")
    print("\nStay types (pass to --type):")
    by_type: dict = {}
    for c in cats.values():
        by_type.setdefault(c.type.name.lower(), []).append(c)
    for tname, items in sorted(by_type.items()):
        roofed = sorted(c.name for c in items if c.roofed)
        plain = sorted(c.name for c in items if not c.roofed)
        if roofed:
            line = ", ".join(
                f"{n} ({counts[n]})" if counts.get(n) else n for n in roofed
            )
            print(f"  {tname:<12} roofed: {line}")
        if plain:
            print(f"  {tname:<12} other:  {', '.join(plain[:10])}")
    if park:
        present = {k: v for k, v in counts.items() if v}
        print(f"\nIn {park.name}: " + (
            ", ".join(f"{k} ({v})" for k, v in present.items()) or "nothing listed"))
    return EXIT_OK


def _alias_for(name: str) -> str:
    from .model import BOOKING_ALIASES
    low = name.lower()
    for alias, keywords in BOOKING_ALIASES.items():
        if any(k == low for k in keywords):
            return alias
    for alias, keywords in BOOKING_ALIASES.items():
        if any(k in low for k in keywords):
            return alias
    return ""


def cmd_alerts(args) -> int:
    client = _client(args)
    alerts = client.alerts()
    if args.json:
        return _emit(args, {"alerts": alerts}, bool(alerts))
    for a in alerts[: args.limit]:
        print(json.dumps(a, ensure_ascii=False)[:300])
    print(f"\n{len(alerts)} alerts", file=sys.stderr)
    return EXIT_OK if alerts else EXIT_NONE


def cmd_cache(args) -> int:
    n = Cache().clear()
    print(f"cleared {n} cached files from {Cache().dir}")
    return EXIT_OK


def _site_json(s) -> dict:
    return {
        "resource_id": s.resource_id, "site": s.name, "area": s.map_name,
        "description": s.description,
        "type": s.info.category if s.info else "",
        "photos": list(s.info.photos) if s.info else [],
        "attributes": s.info.attributes if s.info else {},
        "max_capacity": s.info.max_capacity if s.info else None,
    }


def _print_grouped(sites, limit: int) -> None:
    area = None
    shown = 0
    for s in sites:
        if shown >= limit:
            print(f"\n    ... and {len(sites) - shown} more (raise --limit)")
            return
        if s.map_name != area:
            area = s.map_name
            print(f"\n  {area}")
        desc = f"  — {s.description}" if s.description else ""
        print(f"    site {s.name}{desc}")
        shown += 1


# ---------- argument wiring ----------


def _add_common(p, dates: bool = True) -> None:
    p.add_argument("provider")
    p.add_argument("park")
    if dates:
        p.add_argument("--start", required=True, type=_date, help="arrival YYYY-MM-DD")
        p.add_argument("--end", required=True, type=_date, help="departure YYYY-MM-DD")
    p.add_argument("--equipment", default="tent", help="equipment name substring (see `equipment`)")
    p.add_argument("--party", type=int, default=2, help="party size (default 2)")
    p.add_argument("--booking-category", default=0,
                   help="name or id; default 0 = Campsite (see `equipment`)")


def _add_output(p) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--limit", type=int, default=40, help="max rows to print (default 40)")
    p.add_argument("--no-cache", action="store_true", help="bypass the on-disk reference cache")
    p.add_argument("--max-requests", type=int, default=200, help="hard ceiling on HTTP requests")
    p.add_argument("-v", "--verbose", action="store_true")


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = argparse.ArgumentParser(
        prog="campsites", description="Search Canadian campground availability."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("providers", help="list known reservation systems")
    _add_output(p)
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("parks", help="list parks for a provider")
    p.add_argument("provider")
    p.add_argument("--search", help="filter by name substring")
    _add_output(p)
    p.set_defaults(func=cmd_parks)

    p = sub.add_parser("equipment", help="list equipment and booking category IDs")
    p.add_argument("provider")
    _add_output(p)
    p.set_defaults(func=cmd_equipment)

    p = sub.add_parser("attrs", help="list filterable site attributes for a park")
    p.add_argument("provider")
    p.add_argument("park")
    _add_output(p)
    p.set_defaults(func=cmd_attrs)

    p = sub.add_parser("window", help="operating seasons and when booking opens")
    p.add_argument("provider")
    p.add_argument("park")
    _add_output(p)
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("horizon", help="how far ahead this park can be booked")
    _add_common(p, dates=False)
    _add_output(p)
    p.set_defaults(func=cmd_horizon)

    p = sub.add_parser("search", help="check availability for exact dates")
    _add_common(p)
    p.add_argument("--map", action="append", help="restrict to matching areas")
    p.add_argument("--attr", action="append", type=_attr, metavar="NAME=VALUE",
                   help="filter by site attribute (see `attrs`); repeatable")
    p.add_argument("--type", action="append", dest="types", metavar="TYPE",
                   help="stay type: oTENTik, Yurt, Cabin, Cottage... (see `stays`)")
    p.add_argument("--include-partial", action="store_true",
                   help="also list sites free for only some of the nights")
    _add_output(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("sweep", help="find any opening across a date range")
    _add_common(p)
    p.add_argument("--nights", type=int, default=2, help="stay length (default 2)")
    p.add_argument("--weekends", action="store_true", help="only stays covering Fri/Sat")
    p.add_argument("--weekday", action="append", choices=sorted(WEEKDAYS),
                   help="restrict check-in weekday; repeatable")
    p.add_argument("--map", action="append", help="restrict to matching areas")
    p.add_argument("--attr", action="append", type=_attr, metavar="NAME=VALUE")
    p.add_argument("--type", action="append", dest="types", metavar="TYPE")
    _add_output(p)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser(
        "find", help="sweep EVERY park matching a pattern (e.g. all of Algonquin)"
    )
    _add_common(p)
    p.add_argument("--nights", type=int, default=2, help="stay length (default 2)")
    p.add_argument("--weekends", action="store_true", help="only stays covering Fri/Sat")
    p.add_argument("--weekday", action="append", choices=sorted(WEEKDAYS))
    p.add_argument("--attr", action="append", type=_attr, metavar="NAME=VALUE")
    p.add_argument("--type", action="append", dest="types", metavar="TYPE")
    _add_output(p)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("site", help="day-by-day calendar for one named site")
    _add_common(p)
    p.add_argument("--site", required=True, help="site number/name, e.g. 285")
    _add_output(p)
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("stays", help="what's bookable: cabins, yurts, oTENTiks, huts")
    p.add_argument("provider")
    p.add_argument("park", nargs="?", help="optional: count types in one park")
    _add_output(p)
    p.set_defaults(func=cmd_stays)

    p = sub.add_parser("alerts", help="park alerts and closures")
    p.add_argument("provider")
    _add_output(p)
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("cache-clear", help="empty the on-disk reference cache")
    _add_output(p)
    p.set_defaults(func=cmd_cache)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (LookupError, SpanTooLongError, ValueError) as e:
        return _fail(args, EXIT_USAGE, str(e))
    except CamisHTTPError as e:
        return _fail(args, EXIT_NET, str(e))
    except RuntimeError as e:
        # Request-ceiling and contract violations: the caller must change the
        # command, so these are usage errors, not transient ones.
        return _fail(args, EXIT_USAGE, str(e))
    except Exception as e:  # never let a crash masquerade as "nothing available"
        return _fail(args, EXIT_NET, f"unexpected {type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
