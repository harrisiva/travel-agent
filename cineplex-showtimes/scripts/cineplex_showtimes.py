#!/usr/bin/env python3
"""CLI driver for the Cineplex API client.

Thin presentation + argument-parsing layer over ``cineplex_api`` (the reusable
data library). Every subcommand supports ``--json`` for clean, parseable output
so this can be dropped into an AI skill as a tool.

  showtimes   Times for a theatre + date(s), optionally filtered to one film.
  theatres    Theatres showing a film near a place (with --name search).
  seats       Seat availability for one showtime (--rows / --middle / --all / --map).
  movies      All films in the catalogue (name -> filmId, with --name search).
  locations   All theatres, no film needed (with --name search).

Exit codes (identical with --json):
  0  found something
  1  query worked, nothing found (no showtimes; seats sold out, or no open
     seat matched --rows/--middle)
  2  usage or lookup error (bad date, unknown experience/row, unknown id)
  3  network or API error
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import requests

import cineplex_api as api
# Re-export the library surface so `from cineplex_showtimes import fetch_*` works.
from cineplex_api import (  # noqa: F401
    fetch_showtimes, fetch_theatres, fetch_seat_layout, fetch_seat_availability,
    fetch_movies, fetch_all_theatres, summarize_seats, filter_seats,
    flatten_showtimes, flatten_theatres, flatten_movies,
)

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _render_table(headers, rows, indent="  ") -> str:
    """Render an aligned text table. Empty rows -> header + '(none)'."""
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))

    def fmt(vals):
        return indent + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(vals))

    lines = [fmt(headers),
             indent + "  ".join("─" * widths[i] for i in range(cols))]
    lines += [fmt(r) for r in rows] if rows else [indent + "(none)"]
    return "\n".join(lines)


def format_showtimes(data) -> str:
    """Render showtimes as per-experience tables (12-hour clock + auditorium)."""
    if not data:
        return "No showtimes found."

    blocks: list[str] = []
    for theatre in data:
        header = (f"\U0001f3ad {theatre.get('theatre', 'Unknown theatre')} "
                  f"({theatre.get('theatreId')})")
        for day in theatre.get("dates", []):
            start = day.get("startDate", "")
            day_label = start.split("T")[0] if "T" in start else start
            blocks.append(f"{header}  —  {day_label}")
            for movie in day.get("movies", []):
                runtime = movie.get("runtimeInMinutes")
                rt = f" ({runtime} min)" if runtime else ""
                for exp in movie.get("experiences", []):
                    types = " · ".join(exp.get("experienceTypes", [])) or "Standard"
                    blocks.append(f"\n{movie.get('name', '?')}{rt}  [{types}]")
                    rows = []
                    for s in exp.get("sessions", []):
                        seats = ("SOLD OUT" if s.get("isSoldOut")
                                 else s.get("seatsRemaining", ""))
                        rows.append([
                            api.to_12h(s.get("showStartDateTime", "")),
                            s.get("auditorium", "") or "",
                            "" if seats is None else seats,
                            s.get("vistaSessionId", ""),
                        ])
                    blocks.append(_render_table(
                        ["Time", "Auditorium", "Seats", "Session"], rows)
                        if rows else "  (no sessions)")
    return "\n".join(blocks) if blocks else "No showtimes found."


def format_theatres(data) -> str:
    """Render the theatres response grouped by favourite / nearby / other."""
    if not isinstance(data, dict):
        return "No theatres found."
    groups = [("Favourite", data.get("favouriteTheatres") or []),
              ("Nearby", data.get("nearbyTheatres") or []),
              ("Other", data.get("otherTheatres") or [])]
    blocks: list[str] = []
    for label, theatres in groups:
        if not theatres:
            continue
        blocks.append(f"\n{label} theatres ({len(theatres)})")
        rows = []
        for t in theatres:
            loc = t.get("location") or {}
            meters = loc.get("distanceToOriginInMeters")
            dist = f"{meters / 1000:.1f} km" if isinstance(meters, (int, float)) else ""
            rows.append([t.get("theatreId", ""), t.get("theatreName", ""),
                         loc.get("city", ""), dist])
        blocks.append(_render_table(["ID", "Theatre", "City", "Distance"], rows))
    return "\n".join(blocks) if blocks else "No theatres found for that query."


def _format_flat_theatres(flat) -> str:
    if not flat:
        return "No matching theatres."
    return _render_table(
        ["ID", "Theatre", "City", "Distance"],
        [[t["theatreId"], t["name"], t["city"] or "",
          f"{t['distanceKm']} km" if t["distanceKm"] is not None else ""] for t in flat])


def _render_seatmap(layout, availability) -> str:
    """A visual grid: '.' available, '#' taken, ' ' aisle/gap.

    Uses ``api._iter_layout_rows`` so the row order matches the summary table.
    """
    avail_map = (availability or {}).get("seatAvailabilities", {}) or {}
    rows = list(api._iter_layout_rows(layout))
    # Prefer the layout's declared width; fall back to the widest seen column so
    # the map still renders if totalColumns is missing/zero.
    max_col = max((seat.get("column") or 0
                   for _, row in rows for seat in row.get("seats") or []),
                  default=0)
    total_cols = max(layout.get("totalColumns") or 0, max_col)

    lines = ["  Legend:  . available   # taken       (screen is at top)"]
    for _section, row in rows:
        # One char per grid column; gaps (aisles) stay blank.
        grid = [" "] * (total_cols + 1)
        for seat in row.get("seats") or []:
            col = seat.get("column") or 0  # physical column index in the grid
            status = avail_map.get(seat.get("id"), "Unknown")
            if 0 <= col < len(grid):
                grid[col] = "." if status == "Available" else "#"
        lines.append(f"  {str(row.get('label', '')).rjust(3)} {''.join(grid)}")
    return "\n".join(lines)


def format_seats(summary, theatre_id, showtime_id,
                 show_map=False, layout=None, availability=None) -> str:
    verdict = ("SOLD OUT" if summary["isSoldOut"]
               else f"{summary['available']} of {summary['totalSeats']} seats available")
    blocks = [
        f"Theatre {theatre_id} · Showtime {showtime_id}",
        f"{verdict}  ({summary['percentFull']}% full)",
        "Status: " + ", ".join(f"{k}: {v}" for k, v in sorted(summary["byStatus"].items())),
        "",
        _render_table(["Row", "Available"],
                      [[r["label"], f"{r['available']}/{r['total']}"] for r in summary["rows"]]),
    ]
    if show_map and layout is not None:
        blocks += ["", _render_seatmap(layout, availability)]
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
def _run_showtimes(args) -> int:
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        dates = [args.date if args.date else date.today()]
    # Reject every bad date up front, before spending a request on the good ones.
    for d in dates:
        api.format_api_date(d)

    session = api.new_session()
    results = [api.fetch_showtimes(
        location_id=args.location, date_value=d, film_id=args.film,
        language=args.language, session=session)
        for d in dates]
    # Check tokens only now, against built-in labels plus every label in the
    # responses: a format Cineplex adds later is accepted on days it plays.
    api.check_experiences(args.experiences, results)
    results = [api.filter_showtimes_by_experience(data, args.experiences, strict=False)
               for data in results]
    records = [rec for data in results for rec in api.flatten_showtimes(data)]

    # An unknown locationId answers 204, exactly like a date with nothing on.
    # Only when every date came back empty is it worth one call to tell them apart.
    if not records:
        known = {t["theatreId"] for t in api.flatten_theatres(
            api.fetch_all_theatres(session=session))}
        if str(args.location) not in {str(k) for k in known}:
            raise api.UsageError(
                f"no theatre with id {args.location}; resolve it with `locations --name`")

    if args.json:
        _print_json(records)
    else:
        print("\n\n".join(
            format_showtimes(data) if data
            else f"No showtimes on {api.format_api_date(d)}."
            for d, data in zip(dates, results)))
    return EXIT_OK if records else EXIT_NONE


def _run_theatres(args) -> int:
    data = api.fetch_theatres(
        film_id=args.film, city=args.city, region=args.region,
        region_code=args.region_code, country=args.country,
        latitude=args.latitude, longitude=args.longitude,
        postal_code=args.postal, accuracy_km=args.accuracy,
        experiences=args.experiences, language=args.language)
    flat = api.flatten_theatres(data, name=args.name)

    # JSON is always the flat list; human output is grouped unless --name filters.
    if args.json:
        _print_json(flat)
    elif args.name:
        print(_format_flat_theatres(flat))
    else:
        print(format_theatres(data))
    return EXIT_OK if flat else EXIT_NONE


def _run_movies(args) -> int:
    flat = api.flatten_movies(api.fetch_movies(language=args.language), name=args.name)
    if args.json:
        _print_json(flat)
    elif not flat:
        print("No matching films.")
    else:
        print(_render_table(
            ["ID", "Title", "Runtime", "Released"],
            [[m["id"], m["name"],
              f"{m['runtimeInMinutes']} min" if m["runtimeInMinutes"] else "",
              m["releaseDate"] or ""] for m in flat]))
    return EXIT_OK if flat else EXIT_NONE


def _run_locations(args) -> int:
    flat = api.flatten_theatres(api.fetch_all_theatres(language=args.language),
                                name=args.name)  # reuse the theatre flattener
    if args.json:
        _print_json(flat)
    else:
        print(_format_flat_theatres(flat))
    return EXIT_OK if flat else EXIT_NONE


def seats_exit_code(summary, matched) -> int:
    """0 if an open seat is there to be had, else 1.

    With a filter (--rows/--middle), only an *open* matched seat counts, so
    `--rows G,H --all` still exits 1 when G and H are full."""
    if summary["isSoldOut"] or not summary["available"]:
        return EXIT_NONE
    if matched is not None and not any(m["status"] == "Available" for m in matched):
        return EXIT_NONE
    return EXIT_OK


def _run_seats(args) -> int:
    session = api.new_session()
    layout = api.fetch_seat_layout(args.theatre, args.showtime, session=session)
    availability = api.fetch_seat_availability(args.theatre, args.showtime, session=session)
    # A showtime that has started comes back with an empty availability map,
    # which would otherwise read as "every seat taken".
    if (availability or {}).get("isPostShowtime"):
        raise api.UsageError(f"showtime {args.showtime} has already started or ended")
    summary = api.summarize_seats(layout, availability)
    # Otherwise an empty layout or availability map is not "sold out" — every
    # seat would be "Unknown" and a watch would keep waiting on a broken answer.
    if not summary["totalSeats"]:
        raise api.ApiError(f"showtime {args.showtime}: seat layout came back empty")
    if not (availability or {}).get("seatAvailabilities"):
        raise api.ApiError(f"showtime {args.showtime}: seat availability came back empty")

    # Compute a filtered seat list when the user asked for one. --all on its own
    # means "list every seat" (with statuses), so it also triggers the filter.
    rows = [r.strip() for r in args.rows.split(",") if r.strip()] if args.rows else None
    if rows:
        # A row that doesn't exist would match nothing forever — refuse it.
        present = {str(r["label"]).upper() for r in summary["rows"]}
        missing = [r for r in rows if r.upper() not in present]
        if missing:
            raise api.UsageError(
                f"no row {', '.join(missing)} in this auditorium; rows are "
                + ", ".join(r["label"] for r in summary["rows"]))
    matched = None
    if rows or args.middle or args.all:
        matched = api.filter_seats(layout, availability, rows=rows,
                                   middle=args.middle, available_only=not args.all)
        summary = {**summary, "matched": matched}  # attach for --json consumers

    if args.json:
        _print_json(summary)
    else:
        print(format_seats(summary, args.theatre, args.showtime,
                           show_map=args.map, layout=layout, availability=availability))
        if matched is not None:
            print(f"\nMatched seats ({len(matched)}):")
            print(_render_table(["Row", "Seat", "Status"],
                                [[m["row"], m["seat"], m["status"]] for m in matched])
                  if matched else "  (none)")
    return seats_exit_code(summary, matched)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cineplex theatrical API client.")
    sub = parser.add_subparsers(dest="command")

    st = sub.add_parser("showtimes", help="Times for a theatre + date(s)")
    st.add_argument("--location", required=True, help="locationId (theatre), e.g. 7408")
    st.add_argument("--date", default=None, help="M/D/YYYY or YYYY-MM-DD. Default: today")
    st.add_argument("--dates", default=None, help="Comma-separated dates (overrides --date)")
    st.add_argument("--film", default=None, help="filmId. Omit to list all films")
    st.add_argument("--experiences", default=None,
                    help="Comma-separated, ANY of (case-insensitive), e.g. imax,ultraavx,vip")
    st.add_argument("--language", default="en")
    st.add_argument("--json", action="store_true", help="Flat JSON session records")
    st.set_defaults(func=_run_showtimes)

    th = sub.add_parser("theatres", help="Theatres showing a film near a place")
    th.add_argument("--film", required=True, help="filmId, e.g. 37617")
    th.add_argument("--city", default=None)
    th.add_argument("--region", default=None, help="e.g. Ontario")
    th.add_argument("--region-code", default=None, help="e.g. ON")
    th.add_argument("--country", default="Canada")
    th.add_argument("--latitude", type=float, default=None)
    th.add_argument("--longitude", type=float, default=None)
    th.add_argument("--postal", default=None, help="postalCode, e.g. N2T")
    th.add_argument("--accuracy", type=int, default=5, help="accuracyKm. Default: 5")
    th.add_argument("--name", default=None, help="Filter theatres by name substring, e.g. Vaughan")
    th.add_argument("--experiences", default=None,
                    help="Comma-separated, ANY of, e.g. imax,70mm (no Dolby Atmos/Clubhouse)")
    th.add_argument("--language", default="en")
    th.add_argument("--json", action="store_true", help="Flat JSON theatre records")
    th.set_defaults(func=_run_theatres)

    se = sub.add_parser("seats", help="Seat availability for one showtime")
    se.add_argument("--theatre", required=True, help="theatreId, e.g. 7268")
    se.add_argument("--showtime", required=True, help="showtimeId / vistaSessionId, e.g. 274215")
    se.add_argument("--rows", default=None, help="Filter to row labels, e.g. G,H")
    se.add_argument("--middle", action="store_true", help="Keep only central seats of each row")
    se.add_argument("--all", action="store_true", help="Include taken seats in --rows/--middle matches")
    se.add_argument("--map", action="store_true", help="Also print a visual seat map")
    se.add_argument("--json", action="store_true", help="Structured summary JSON")
    se.set_defaults(func=_run_seats)

    mv = sub.add_parser("movies", help="List all films (name -> filmId)")
    mv.add_argument("--name", default=None, help="Filter by title substring, e.g. Odyssey")
    mv.add_argument("--language", default="en")
    mv.add_argument("--json", action="store_true", help="Flat JSON film records")
    mv.set_defaults(func=_run_movies)

    lo = sub.add_parser("locations", help="List all theatres (no film needed)")
    lo.add_argument("--name", default=None, help="Filter by theatre-name substring, e.g. Vaughan")
    lo.add_argument("--language", default="en")
    lo.add_argument("--json", action="store_true", help="Flat JSON theatre records")
    lo.set_defaults(func=_run_locations)

    return parser


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Backward-compatible default: a bare flag-style invocation (e.g.
    # `... --location 7408`) implies the `showtimes` subcommand. A bad
    # subcommand name (no leading dash) still reaches argparse for a proper
    # "invalid choice" error, and -h/--help stay at the top level.
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["showtimes"] + argv

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return args.func(args)
    except api.UsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except requests.HTTPError as exc:
        return http_error_exit(exc)
    except requests.RequestException as exc:  # includes bad JSON from the API
        print(f"Network error: {exc}", file=sys.stderr)
        return EXIT_NET
    except api.ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return EXIT_NET
    except Exception as exc:  # response shape drift etc. — never exit 1 ("keep waiting")
        print(f"Unexpected error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return EXIT_NET


def http_error_exit(exc) -> int:
    """404 (unknown film/showtime id) and 400 (rejected input) are lookup
    errors; everything else — 401 after the re-scrape, 429, 5xx — is the API."""
    status = getattr(exc.response, "status_code", None)
    if status in (400, 404):
        body = (exc.response.text or "").strip()[:200]
        print(f"Error: {status} from Cineplex: {body}", file=sys.stderr)
        return EXIT_USAGE
    print(f"API error: {exc}", file=sys.stderr)
    return EXIT_NET


if __name__ == "__main__":
    raise SystemExit(main())
