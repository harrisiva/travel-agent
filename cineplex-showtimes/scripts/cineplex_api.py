#!/usr/bin/env python3
"""Reusable client library for Cineplex's theatrical + ticketing APIs.

Pure data layer: every function returns plain Python structures (dicts / lists)
— no printing, no argparse. The CLI driver (``cineplex_showtimes.py``) and any
AI skill import from here.

The endpoints at apis.cineplex.com are fronted by Azure API Management and only
require one static header, ``Ocp-Apim-Subscription-Key``. Cineplex ships that
key publicly in its own JS bundle, so no browser session, cookies, or login are
needed. ``get_subscription_key`` scrapes the current key at runtime, caches it
in ``.cineplex_key``, and falls back to a known value if scraping fails.

Endpoints covered
-----------------
  fetch_showtimes          /cpx/theatrical/api/v1/showtimes
  fetch_theatres           /cpx/theatrical/api/v1/theatres        (near a place)
  fetch_all_theatres       /cpx/theatrical/api/v1/theatres        (no filmId)
  fetch_movies             /cpx/theatrical/api/v1/movies          (full catalogue)
  fetch_seat_layout        /ticketing/api/v1/theatre/{t}/showtime/{s}/seat-layout
  fetch_seat_availability  /ticketing/api/v1/theatre/{t}/showtime/{s}/seat-availability

Higher-level helpers
--------------------
  flatten_showtimes    -> flat list of session records
  flatten_theatres     -> flat list of theatres (with optional name filter)
  flatten_movies       -> flat list of films (with optional name filter)
  summarize_seats      -> availability summary for a showtime
  filter_seats         -> seats matching row / "middle" / availability filters
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import requests

API_ROOT = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"
TICKETING_ROOT = "https://apis.cineplex.com/prod/ticketing/api/v1"
SHOWTIMES_URL = f"{API_ROOT}/showtimes"
THEATRES_URL = f"{API_ROOT}/theatres"
MOVIES_URL = f"{API_ROOT}/movies"
HOME_URL = "https://www.cineplex.com/"
DEFAULT_KEY = "dcdac5601d864addbc2675a2e96cb1f8"
KEY_CACHE = Path(__file__).with_name(".cineplex_key")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

KEY_RE = re.compile(r'Ocp-Apim-Subscription-Key"\s*:\s*"([0-9a-f]{32})"')
CHUNK_RE = re.compile(r'src="([^"]*/_next/static/chunks/[^"]+\.js)"')

# The public surface — what `from cineplex_api import *` and callers should use.
__all__ = [
    "new_session", "get_subscription_key", "format_api_date", "to_12h",
    "UsageError", "ApiError", "KNOWN_EXPERIENCES", "normalize_experience",
    "parse_experiences", "check_experiences", "filter_showtimes_by_experience",
    "theatre_experience_codes",
    "fetch_showtimes", "fetch_theatres", "fetch_all_theatres", "fetch_movies",
    "fetch_seat_layout", "fetch_seat_availability",
    "flatten_showtimes", "flatten_theatres", "flatten_movies",
    "summarize_seats", "filter_seats",
]


# --------------------------------------------------------------------------- #
# Subscription key + transport
# --------------------------------------------------------------------------- #
def _scrape_key(session: requests.Session) -> str | None:
    """Fetch the Cineplex homepage, walk its JS chunks, and extract the key."""
    try:
        html = session.get(HOME_URL, timeout=15).text
    except requests.RequestException:
        return None

    chunks = CHUNK_RE.findall(html)
    # The key lives in the theatrical-api chunk (currently 9026-*); try those
    # first, then fall back to scanning every chunk on the page.
    chunks.sort(key=lambda u: (0 if "/9026-" in u else 1, u))

    for url in chunks:
        try:
            js = session.get(url, timeout=15).text
        except requests.RequestException:
            continue
        match = KEY_RE.search(js)
        if match:
            return match.group(1)
    return None


def get_subscription_key(session: requests.Session, force_refresh: bool = False) -> str:
    """Return a usable subscription key, using cache -> scrape -> default."""
    if not force_refresh and KEY_CACHE.exists():
        try:
            cached = KEY_CACHE.read_text().strip()
        except (OSError, UnicodeDecodeError):
            cached = ""  # unreadable cache: fall through to a fresh scrape
        if re.fullmatch(r"[0-9a-f]{32}", cached):
            return cached

    key = _scrape_key(session)
    if key:
        try:
            KEY_CACHE.write_text(key)
        except OSError:
            pass
        return key

    return DEFAULT_KEY


def new_session() -> requests.Session:
    """A pre-configured requests session (reuse across calls to pool sockets)."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _get(url: str, params: dict, session: requests.Session):
    """GET with automatic key resolution and a single 401 re-scrape retry.

    A 401 means the cached key is stale: drop it and retry once with a fresh
    scrape. Any other error, or a second 401, raises via ``raise_for_status``.
    """
    for attempt in range(2):
        key = get_subscription_key(session, force_refresh=(attempt == 1))
        resp = session.get(
            url, params=params,
            headers={"Ocp-Apim-Subscription-Key": key}, timeout=20,
        )
        if resp.status_code == 401 and attempt == 0:
            try:
                KEY_CACHE.unlink(missing_ok=True)  # stale key -> drop & re-scrape
            except OSError:
                pass  # read-only filesystem: the forced re-scrape still runs
            continue
        resp.raise_for_status()
        # A date with no showtimes (or an unknown locationId) answers 204 with
        # an empty body. That is "no data", not a failure.
        if resp.status_code == 204 or not resp.content.strip():
            return None
        return resp.json()


def format_api_date(value) -> str:
    """Format a date as M/D/YYYY (no zero-padding), matching the site.

    Raises ``UsageError`` on anything unparseable rather than passing it
    through — the API would answer with a 400 or, worse, an empty 204."""
    if isinstance(value, (datetime, date)):
        return f"{value.month}/{value.day}/{value.year}"
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return f"{parsed.month}/{parsed.day}/{parsed.year}"
        except ValueError:
            continue
    raise UsageError(f"unrecognised date {text!r}; use M/D/YYYY or YYYY-MM-DD")


# --------------------------------------------------------------------------- #
# Experiences (IMAX, 70mm, UltraAVX, ...)
# --------------------------------------------------------------------------- #
# The server-side `experiences` filter matches only undocumented lowercase
# codes: `imax` works, but `IMAX`, `ultraavx` and `dolby atmos` silently return
# nothing. So showtimes are fetched unfiltered and filtered here, against the
# `experienceTypes` labels each session actually carries.
#
# Every label seen across all 152 theatres (surveyed 2026-09-10, showtimes
# for 2026-09-12). Used only to tell a
# typo from a real format that happens not to be playing that day.
KNOWN_EXPERIENCES = (
    "Regular", "Recliner", "UltraAVX", "Dolby Atmos", "D-BOX", "3D",
    "Laser Projection", "VIP 19+", "VIP 18+", "IMAX", "ScreenX", "70mm",
    "4DX", "Clubhouse",
)
# Short forms people type -> the normalised label they mean.
EXPERIENCE_ALIASES = {"avx": "ultraavx", "atmos": "dolbyatmos",
                      "laser": "laserprojection", "standard": "regular"}
# The theatres endpoint has no per-theatre experience data, so its filter has
# to go to the server. These codes were each verified to narrow the result;
# Dolby Atmos and Clubhouse have no known code.
THEATRE_EXPERIENCE_CODES = {
    "regular": "regular", "recliner": "recliner", "ultraavx": "avx",
    "dbox": "dbox", "3d": "3d", "laserprojection": "laser", "vip": "vip",
    "imax": "imax", "screenx": "screenx", "70mm": "70mm", "4dx": "4dx",
}


class UsageError(ValueError):
    """Bad input the API would not reject cleanly (CLI exit code 2)."""


class ApiError(RuntimeError):
    """The API answered, but with something unusable (CLI exit code 3)."""


def normalize_experience(label) -> str:
    """'VIP 19+' -> 'vip', 'D-BOX' -> 'dbox', 'Dolby Atmos' -> 'dolbyatmos'.

    Case, spaces and punctuation are dropped, a trailing age limit ("19+") is
    ignored, and short aliases ('avx', 'atmos') map to the full name."""
    text = re.sub(r"\s*\d+\+$", "", str(label).strip())
    key = re.sub(r"[^0-9a-z]", "", text.lower())
    return EXPERIENCE_ALIASES.get(key, key)


def parse_experiences(value) -> list[str]:
    """'70mm, IMAX' or ['70mm', 'IMAX'] -> ['70mm', 'imax'] (normalised, deduped).

    Raises ``UsageError`` when something was given but nothing survives
    normalising ('19+', ',', ' '): silently dropping the filter would answer
    with every screening."""
    if value is None or value == "" or value == []:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for p in parts:
        key = normalize_experience(p)
        if key and key not in out:
            out.append(key)
    if not out:
        raise UsageError(f"--experiences {value!r} names no experience")
    return out


def check_experiences(experiences, responses) -> None:
    """Raise ``UsageError`` for a token that is neither a built-in label nor a
    label on any session in ``responses`` (a list of showtimes responses).

    Checking against the responses too means a label Cineplex adds later is
    accepted on any day it is actually playing."""
    valid = {normalize_experience(e) for e in KNOWN_EXPERIENCES}
    valid |= {normalize_experience(t) for data in responses
              for rec in flatten_showtimes(data) for t in rec["experience"]}
    unknown = [w for w in parse_experiences(experiences) if w not in valid]
    if unknown:
        raise UsageError(
            f"unknown experience {', '.join(unknown)}; known: "
            + ", ".join(KNOWN_EXPERIENCES))


def filter_showtimes_by_experience(data, experiences, strict=True):
    """Keep only experience blocks carrying ANY of ``experiences`` (OR).

    Matching is on normalised labels, so '70MM', 'vip', 'dolby atmos' and
    'D-BOX' all work. With ``strict``, a token that is neither a known label
    nor present in this response raises ``UsageError`` — a typo would
    otherwise read as "no screenings" forever."""
    wanted = parse_experiences(experiences)
    if not wanted:
        return data
    # Validate even when there is no data, or a typo on an empty date slips by.
    if strict:
        check_experiences(experiences, [data])
    if not data:
        return data
    out = []
    for theatre in data:
        dates = []
        for day in theatre.get("dates") or []:
            movies = []
            for movie in day.get("movies") or []:
                exps = [e for e in movie.get("experiences") or []
                        if {normalize_experience(t) for t in
                            e.get("experienceTypes") or []} & set(wanted)]
                if exps:
                    movies.append({**movie, "experiences": exps})
            if movies:
                dates.append({**day, "movies": movies})
        if dates:
            out.append({**theatre, "dates": dates})
    return out


def theatre_experience_codes(experiences) -> str | None:
    """Translate experiences into the server codes the theatres filter wants.

    Raises ``UsageError`` for a format with no verified code rather than
    sending it and getting back a silent empty list."""
    wanted = parse_experiences(experiences)
    if not wanted:
        return None
    missing = [w for w in wanted if w not in THEATRE_EXPERIENCE_CODES]
    if missing:
        raise UsageError(
            f"theatres cannot filter on {', '.join(missing)} (no server code); "
            "run showtimes --experiences per theatre instead. Supported here: "
            + ", ".join(sorted(THEATRE_EXPERIENCE_CODES)))
    # The server ORs a comma-separated list.
    return ",".join(dict.fromkeys(THEATRE_EXPERIENCE_CODES[w] for w in wanted))


# --------------------------------------------------------------------------- #
# Endpoint fetchers
# --------------------------------------------------------------------------- #
def fetch_showtimes(location_id, date_value, film_id=None, experiences=None,
                    language="en", session=None) -> list:
    """Showtimes for a theatre + date. If ``film_id`` is None, every film
    playing at that theatre is returned. Returns a list of theatre blocks, or
    None when the date has no showtimes; use ``flatten_showtimes`` for a flat
    list of session records.

    ``experiences`` is applied client-side (see
    ``filter_showtimes_by_experience``), never sent to the server."""
    session = session or new_session()
    params = {"language": language, "locationId": location_id,
              "date": format_api_date(date_value)}
    if film_id:
        params["filmId"] = film_id
    return filter_showtimes_by_experience(
        _get(SHOWTIMES_URL, params, session), experiences)


def fetch_theatres(film_id, city=None, region=None, region_code=None,
                   country="Canada", latitude=None, longitude=None,
                   postal_code=None, accuracy_km=5, experiences=None,
                   language="en", session=None) -> dict:
    """Theatres showing a film near a place. Returns
    {favouriteTheatres, nearbyTheatres, otherTheatres}; use
    ``flatten_theatres`` for a flat list."""
    session = session or new_session()
    experiences = theatre_experience_codes(experiences)

    params = {"language": language, "filmId": film_id, "country": country,
              "accuracyKm": accuracy_km}
    for key, val in (("city", city), ("region", region),
                     ("regionCode", region_code), ("latitude", latitude),
                     ("longitude", longitude), ("postalCode", postal_code),
                     ("experiences", experiences)):
        if val is not None:
            params[key] = val
    return _get(THEATRES_URL, params, session)


def fetch_movies(language="en", session=None) -> dict:
    """The full film catalogue: {"items": [...], "totalCount": N}.
    Use ``flatten_movies`` for a flat list of {id, name, ...}."""
    session = session or new_session()
    return _get(MOVIES_URL, {"language": language}, session)


def fetch_all_theatres(language="en", session=None) -> dict:
    """Every theatre, no film needed. Same shape as ``fetch_theatres``
    ({favouriteTheatres, nearbyTheatres, otherTheatres})."""
    session = session or new_session()
    return _get(THEATRES_URL, {"language": language}, session)


def fetch_seat_availability(theatre_id, showtime_id, session=None) -> dict:
    """Seat statuses for a showtime, wrapped as
    {"seatAvailabilities": {seat_id: status, ...}} where status is e.g.
    'Available' / 'Occupied' / 'Broken'."""
    session = session or new_session()
    url = f"{TICKETING_ROOT}/theatre/{theatre_id}/showtime/{showtime_id}/seat-availability"
    return _get(url, {}, session)


def fetch_seat_layout(theatre_id, showtime_id, session=None) -> dict:
    """The physical seat layout (rows/columns/labels) for a showtime."""
    session = session or new_session()
    url = f"{TICKETING_ROOT}/theatre/{theatre_id}/showtime/{showtime_id}/seat-layout"
    return _get(url, {}, session)


# --------------------------------------------------------------------------- #
# Higher-level helpers (pure transforms over the raw responses)
# --------------------------------------------------------------------------- #
def to_12h(dt_str: str) -> str:
    """'2026-07-19T15:00:00' -> '3:00 PM'; returns the input unchanged if unparseable."""
    try:
        t = datetime.strptime(str(dt_str).split("T")[-1][:8], "%H:%M:%S")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(dt_str)


def flatten_showtimes(data) -> list[dict]:
    """Flatten the nested showtimes response into a list of session records.

    Each record: {theatreId, theatre, date, filmId, movie, experience (list),
    auditorium, time (12h), showStartDateTime, sessionId, seatsRemaining,
    isSoldOut}. ``sessionId`` feeds ``fetch_seat_*``.
    """
    out = []
    for theatre in data or []:
        for day in theatre.get("dates") or []:
            day_label = (day.get("startDate") or "").split("T")[0]
            for movie in day.get("movies") or []:
                for exp in movie.get("experiences") or []:
                    exp_types = exp.get("experienceTypes") or []
                    for s in exp.get("sessions") or []:
                        out.append({
                            "theatreId": theatre.get("theatreId"),
                            "theatre": theatre.get("theatre"),
                            "date": day_label,
                            "filmId": movie.get("id"),
                            "movie": movie.get("name"),
                            "experience": exp_types,
                            "auditorium": s.get("auditorium"),
                            "time": to_12h(s.get("showStartDateTime", "")),
                            "showStartDateTime": s.get("showStartDateTime"),
                            "sessionId": s.get("vistaSessionId"),
                            "seatsRemaining": s.get("seatsRemaining"),
                            "isSoldOut": s.get("isSoldOut"),
                        })
    return out


def flatten_theatres(data, name=None) -> list[dict]:
    """Flatten the theatres response; optional case-insensitive name filter.

    Each record: {theatreId, name, city, group (favourite/nearby/other),
    distanceKm (None if no origin was supplied)}.
    """
    out = []
    for group in ("favouriteTheatres", "nearbyTheatres", "otherTheatres"):
        for t in ((data or {}).get(group) or []):
            if name and name.lower() not in (t.get("theatreName", "") or "").lower():
                continue
            loc = t.get("location") or {}
            meters = loc.get("distanceToOriginInMeters")
            out.append({
                "theatreId": t.get("theatreId"),
                "name": t.get("theatreName"),
                "city": loc.get("city"),
                "group": group.replace("Theatres", ""),
                "distanceKm": round(meters / 1000, 1)
                if isinstance(meters, (int, float)) else None,
            })
    return out


def flatten_movies(data, name=None) -> list[dict]:
    """Flatten the /movies response; optional case-insensitive name filter.

    Each record: {id (filmId), name, runtimeInMinutes, releaseDate}.
    """
    out = []
    for m in (data or {}).get("items", []):
        if name and name.lower() not in (m.get("name", "") or "").lower():
            continue
        # releaseDate comes as an ISO datetime; keep just the date part.
        released = (m.get("releaseDate") or "").split("T")[0]
        out.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "runtimeInMinutes": m.get("runtimeInMinutes"),
            "releaseDate": released,
        })
    return out


def _iter_layout_rows(layout):
    """Yield (section, row) for every populated seating section, in seating
    order (front standard seats first, then D-BOX, then balcony)."""
    for section in ("standardSeats", "dboxSeats", "balconySeats"):
        sec = (layout or {}).get(section) or {}
        for row in sec.get("rows") or []:
            if row.get("seats"):
                yield section, row


def summarize_seats(layout, availability) -> dict:
    """Merge layout + availability into a compact, tool-friendly summary."""
    avail_map = (availability or {}).get("seatAvailabilities", {}) or {}
    by_status: dict[str, int] = {}
    rows_out, available_seats, total = [], [], 0

    for section, row in _iter_layout_rows(layout):
        r_avail = r_total = 0
        for seat in row.get("seats") or []:
            # seat["id"] (e.g. "1_14_25" = area_row_column) is the key used by
            # the availability map. Seats absent from that map are "Unknown".
            status = avail_map.get(seat.get("id"), "Unknown")
            by_status[status] = by_status.get(status, 0) + 1
            total += 1
            r_total += 1
            if status == "Available":
                r_avail += 1
                available_seats.append(seat.get("label") or seat.get("id"))
        rows_out.append({"label": row.get("label", ""), "section": section,
                         "available": r_avail, "total": r_total})

    available = by_status.get("Available", 0)
    unavailable = total - available
    return {
        "totalSeats": total,
        "available": available,
        "unavailable": unavailable,
        "percentFull": round(100 * unavailable / total, 1) if total else 0.0,
        "isSoldOut": available == 0 and total > 0,
        "byStatus": by_status,
        "availableSeats": available_seats,
        "rows": rows_out,
    }


def filter_seats(layout, availability, rows=None, middle=False,
                 available_only=True, middle_fraction=1 / 3) -> list[dict]:
    """Return seats matching filters.

    rows            iterable of row labels (case-insensitive), e.g. ['G', 'H'].
    middle          keep only the central ``middle_fraction`` of each row.
    available_only  drop seats that are not 'Available'.

    Each returned seat: {section, row, seat (label), column, id, status}.
    """
    avail_map = (availability or {}).get("seatAvailabilities", {}) or {}
    want_rows = {str(r).strip().upper() for r in rows} if rows else None
    matched = []

    for section, row in _iter_layout_rows(layout):
        label = str(row.get("label", ""))
        if want_rows and label.upper() not in want_rows:
            continue
        # Order seats left-to-right so "middle" is geometrically central.
        # `or 0` guards against a null column value (sorting None vs int fails).
        seats = sorted(row.get("seats") or [], key=lambda s: s.get("column") or 0)
        if middle and seats:
            # Keep the central slice: e.g. 1/3 of the row, centered.
            n = len(seats)
            keep = max(1, round(n * middle_fraction))
            start = (n - keep) // 2
            seats = seats[start:start + keep]
        for seat in seats:
            status = avail_map.get(seat.get("id"), "Unknown")
            if available_only and status != "Available":
                continue
            matched.append({
                "section": section,
                "row": label,
                "seat": seat.get("label") or seat.get("id"),
                "column": seat.get("column"),
                "id": seat.get("id"),
                "status": status,
            })
    return matched
