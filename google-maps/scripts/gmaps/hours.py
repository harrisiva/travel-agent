"""Full-week opening hours, from Google's own embed endpoint.

The place-search RPC returns today only. The **embed** surface returns all seven
days for the same feature id, in a ~4 KB HTML response, with no API key, no
cookies and — verified — not even a User-Agent:

    GET https://www.google.com/maps/embed?pb=!1m17!1m12!1m3!1d2886!2d0!3d0
        !2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m2!1m1!1s<FTID>
        !5e0!3m2!1sen!2sca!5m2!1sen!2sca

Measured coverage: 20/20 of a live downtown Toronto search. This replaced an
OpenStreetMap fallback that reached only 25% and could be years stale — on
Culture Crust, Kitchener, OSM said 02:00 where Google says 3 a.m.

Notes that are load-bearing:

- The FTID's ``:`` must be percent-encoded; ``place_id`` (``ChIJ…``) does NOT
  work here, only the hex feature id from ``blob[10]``.
- ``hl`` must be ``en`` or the day names come back localised, silently breaking
  any parser that matches English weekday names.
- The ``!1m12`` viewport wrapper is required even though the coordinates in it
  are ignored — ``!2d0!3d0`` works fine. Dropping the wrapper gives HTTP 400.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.parse
from typing import Any

from gmaps.errors import NetworkError, UsageError
from gmaps.fanout import fan_out
from gmaps.http import Session

EMBED = "https://www.google.com/maps/embed"

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")
DAY_INDEX = {d[:2]: i for i, d in enumerate(DAY_NAMES)}

#: One request per place, so a 20-result search is 20 requests. Deliberately
#: low on an internal surface with no published rate limit.
DEFAULT_BUDGET = 25


_WHEN_RE = re.compile(
    r"^\s*([A-Za-z]{2,9})\s+(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.I)


def parse_when(text: str) -> tuple[int, int]:
    """'Fri 20:00' / 'Friday 8pm' / '2026-10-04 21:00' -> (weekday, minutes).

    **The result is in the place's own local time**, because that is the clock
    Google's hours are published in. A traveller asking "will it be open when
    we land at 9pm" means 9pm where they are landing, and this keeps that
    honest without needing the caller's timezone at all.

    An ISO date is accepted so a trip planner can say which Friday it means;
    only the weekday and time are used, since Google publishes a weekly
    pattern rather than dated exceptions.
    """
    raw = (text or "").strip()
    if not raw:
        raise UsageError("--open-at needs a time")

    # ISO first: unambiguous, and free via the standard library.
    for candidate in (raw, raw.replace(" ", "T")):
        try:
            when = datetime.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return when.weekday(), when.hour * 60 + when.minute

    match = _WHEN_RE.match(raw)
    if not match:
        raise UsageError(
            f"--open-at {text!r} not understood. Use 'Fri 20:00', 'Friday 8pm', "
            "or an ISO time like '2026-10-04 21:00'.")
    # Match the whole token against real weekday names, not just its first two
    # letters: "Summer 8pm" shares a two-letter prefix with Sunday and would
    # otherwise be answered confidently about the wrong day.
    token = match.group(1).lower()
    day_index = next((i for i, name in enumerate(DAY_NAMES)
                      if name.lower().startswith(token)), None)
    if day_index is None:
        raise UsageError(f"--open-at: {match.group(1)!r} is not a weekday")

    hour = int(match.group(2))
    minute = int(match.group(3) or 0)
    suffix = (match.group(4) or "").replace(".", "").lower()

    # A bare hour of 1-11 with no am/pm and no colon is genuinely ambiguous.
    # Defaulting to morning answers a dinner question about breakfast, so this
    # refuses rather than guessing.
    if not suffix and match.group(3) is None and 1 <= hour <= 11:
        raise UsageError(
            f"--open-at {text!r} is ambiguous: {hour} could be am or pm. "
            f"Write '{hour}pm', or use 24-hour time like '{hour + 12}:00'.")
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise UsageError(f"--open-at: {text!r} is not a valid time")
    return day_index, hour * 60 + minute


def _pb(ftid: str) -> str:
    return (
        "!1m17!1m12!1m3!1d2886!2d0!3d0!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1"
        f"!3m2!1m1!1s{urllib.parse.quote(ftid, safe='')}"
        "!5e0!3m2!1sen!2sca!5m2!1sen!2sca"
    )


def _find_place(node: Any) -> list | None:
    """Locate the place record structurally, not by a fixed index.

    Its head is ``[ftid, "name, address", [lat, lng], cid]`` — distinctive
    enough to match on, and it survives the surrounding array moving around.
    """
    if isinstance(node, list):
        head = node[0] if node else None
        if (isinstance(head, list) and len(head) == 4 and isinstance(head[0], str)
                and head[0].startswith("0x") and isinstance(head[2], list)):
            return node
        for child in node:
            found = _find_place(child)
            if found is not None:
                return found
    return None


def _day_records(place: list) -> list | None:
    """The seven day records, found by shape rather than by index.

    They sat at index 39 on every place tested, but scanning for the shape
    costs nothing and does not break when Google renumbers.
    """
    for field in place:
        if (isinstance(field, list) and field and isinstance(field[0], list)
                and len(field[0]) == 7
                and all(isinstance(d, list) and d and d[0] in DAY_NAMES
                        for d in field[0])):
            return field[0]
    return None


def _clock(part: Any, *, end: bool) -> int | None:
    """A ragged ``[h, m]`` clock time in minutes.

    Google omits what it considers obvious, and the omissions are **not** zero:

    ==================  ==========================  =================
    raw                 display                     meaning
    ==================  ==========================  =================
    ``[[17], []]``      "5 p.m.-12 a.m."            end is midnight
    ``[[], [23]]``      "12 a.m.-11 p.m."           start is midnight
    ``[[], []]``        "Open 24 hours"             both ends
    ``[[8], [23, 30]]`` "8 a.m.-11:30 p.m."         fully specified
    ==================  ==========================  =================

    So an empty list is midnight — 0 at the start of a span, 1440 at the end.
    Reading it as 0 in both positions turns "5pm till midnight" into a negative
    span, which then reads as closed: a false negative that looks plausible.
    """
    if not isinstance(part, list):
        return None
    if not part:
        return 1440 if end else 0
    hour = part[0]
    minute = part[1] if len(part) > 1 else 0
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def parse_days(records: list) -> list[dict]:
    """Seven day records -> a list of dicts in Monday-first order."""
    by_name: dict[str, dict] = {}
    for rec in records:
        name = rec[0]
        intervals = rec[3] if len(rec) > 3 and isinstance(rec[3], list) else []
        display: list[str] = []
        spans: list[tuple[int, int]] = []
        for item in intervals:
            if not isinstance(item, list) or not item:
                continue
            label = item[0] if isinstance(item[0], str) else None
            if label:
                display.append(label)
            pair = item[1] if len(item) > 1 else None
            if not isinstance(pair, list) or len(pair) != 2:
                continue  # "Closed" carries no pair at all
            start = _clock(pair[0], end=False)
            end = _clock(pair[1], end=True)
            if start is not None and end is not None:
                spans.append((start, end))
        closed = bool(display) and not spans and display[0].strip().lower() == "closed"
        by_name[name] = {"day": name, "display": display, "spans": spans,
                         "closed": closed}
    return [by_name[d] for d in DAY_NAMES if d in by_name]


def week_index(days: list[dict]) -> dict[int, list[tuple[int, int]]]:
    """{weekday 0=Mon: [(start_min, end_min)]}, with midnight spill resolved.

    A span whose end is at or before its start crosses midnight, so its tail
    lands on the following day. A bar open 17:00-02:00 on Friday really is open
    at 1am on Saturday.
    """
    week: dict[int, list[tuple[int, int]]] = {i: [] for i in range(7)}
    for i, day in enumerate(days):
        for start, end in day["spans"]:
            if end <= start:
                week[i].append((start, 1440))
                week[(i + 1) % 7].append((0, end))
            else:
                week[i].append((start, end))
    return {i: sorted(set(v)) for i, v in week.items()}


def is_open_at(days: list[dict], weekday: int, minutes: int) -> bool | None:
    if not days:
        return None
    return any(s <= minutes < e for s, e in week_index(days).get(weekday, []))


def describe(days: list[dict]) -> list[str] | None:
    """One line per day, Monday first, using Google's own display strings."""
    if not days:
        return None
    out = []
    for day in days:
        if day["display"]:
            out.append(f"{day['day']}: " + ", ".join(day["display"]))
        elif day["spans"]:
            # Real opening spans but no display string from Google. Printing
            # "Closed" here contradicted is_open_at() on the same record — the
            # JSON said open and the human line said shut.
            out.append(f"{day['day']}: " + ", ".join(
                f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                for s, e in sorted(day["spans"])))
        else:
            out.append(f"{day['day']}: Closed")
    return out


def fetch_week(session: Session, ftid: str) -> list[dict] | None:
    """All seven days for one feature id, or None when Google publishes none."""
    if not ftid:
        return None
    # The pb keeps its literal "!" delimiters — hence raw_suffix rather than
    # params. Percent-encoding them to %21 makes Google return an empty map
    # instead of an error, which reads as "no hours" rather than as a bug.
    body = session.get_text(EMBED, raw_suffix=f"pb={_pb(ftid)}")
    marker = body.find("initEmbed(")
    if marker == -1:
        return None
    try:
        start = body.index("[", marker)
        data, _ = json.JSONDecoder().raw_decode(body, start)
    except ValueError:
        return None

    place = _find_place(data)
    if place is None:
        return None
    records = _day_records(place)
    if not records:
        return None
    days = parse_days(records)
    return days if len(days) == 7 else None


def annotate(session: Session, places: list[dict],
             budget: int = DEFAULT_BUDGET) -> int:
    """Attach a full week to each place. Returns the number actually fetched.

    A place Google publishes no hours for keeps `hours_week` of None, which
    downstream must treat as *unknown* rather than as closed.
    """
    def work(place: dict) -> None:
        try:
            days = fetch_week(session, place.get("ftid") or "")
        except Exception as exc:  # noqa: BLE001
            # ANY failure, not just NetworkError. fan_out swallows what the
            # worker lets escape, so a worker that records only one exception
            # type loses every other kind — and a lost failure is rendered as
            # "Google publishes no hours for this place", a confident claim
            # manufactured out of an outage.
            place["hours_error"] = f"{type(exc).__name__}: {exc}"
            place["hours_week"] = None
            place["hours_week_days"] = None
            return
        place["hours_week_days"] = days
        place["hours_week"] = describe(days) if days else None
        place["hours_week_source"] = "google-maps-embed" if days else None

    def skipped(place: dict) -> None:
        # NOT the same as "Google publishes no hours". We simply never asked.
        # Conflating them reports a budget ceiling as a fact about the place.
        place["hours_week"] = None
        place["hours_week_days"] = None
        place["hours_skipped"] = True

    return fan_out(places, work, budget=budget, on_skipped=skipped)
