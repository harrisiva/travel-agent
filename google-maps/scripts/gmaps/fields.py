"""The index map for Google's positional place blobs — one table, one place.

Google's payloads are undocumented positional arrays. Nothing is named; a field
is wherever it happens to sit. That makes a shifted index the characteristic
failure of this whole package, and it is a *silent* one: reading the wrong slot
yields a plausible value, not an exception.

So every index lives here rather than scattered through the code as
`_dig(blob, 203, 1, 8, 0)`. Three things follow:

- The table mirrors the field map in `NOTES.md`; when Google moves something,
  one file changes.
- Each field declares a validator, so a shifted index is *caught* rather than
  passed on. A rating of 4.7 is plausible; a rating of "Old Toronto" is not.
- The self-check asserts the whole table against real captured payloads at
  once, so one test covers every field.
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple

FTID_RE = re.compile(r"^0x[0-9a-f]+:0x[0-9a-f]+$")


def _is_text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_rating(v: Any) -> bool:
    return isinstance(v, (int, float)) and 0 <= v <= 5


def _is_lat(v: Any) -> bool:
    return isinstance(v, (int, float)) and -90 <= v <= 90


def _is_lng(v: Any) -> bool:
    return isinstance(v, (int, float)) and -180 <= v <= 180


def _is_ftid(v: Any) -> bool:
    return isinstance(v, str) and bool(FTID_RE.match(v))


def _is_place_id(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("ChIJ")


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_tz(v: Any) -> bool:
    return isinstance(v, str) and "/" in v


class Field(NamedTuple):
    """One extractable field: where it is, and what a sane value looks like."""

    name: str
    path: tuple[int, ...]
    valid: Callable[[Any], bool]
    required: bool = False


#: Verified against live search and place-detail payloads. Paths are relative
#: to the place blob — `data[0][1][n][14]` for search, `data[6]` for detail.
PLACE_FIELDS: tuple[Field, ...] = (
    Field("name", (11,), _is_text, required=True),
    Field("lat", (9, 2), _is_lat, required=True),
    Field("lng", (9, 3), _is_lng, required=True),
    Field("address", (18,), _is_text),
    Field("ftid", (10,), _is_ftid),
    Field("place_id", (78,), _is_place_id),
    Field("rating", (4, 7), _is_rating),
    Field("categories", (13,), _is_str_list),
    Field("timezone", (30,), _is_tz),
    Field("website", (7, 0), _is_text),
    Field("phone", (178, 0, 3), _is_text),
    Field("status_detail", (203, 1, 4, 0), _is_text),
    Field("hours_today", (203, 0, 0, 3, 0, 0), _is_text),
    Field("editorial", (32, 0, 1), _is_text),
    Field("neighbourhood", (14,), _is_text),
    Field("city_region", (166,), _is_text),
)

#: Live open/closed. Kept out of the table because it needs interpreting, not
#: just validating — see `model._status`.
STATUS_PATH = (203, 1, 8, 0)

#: Operational state: [9] operating, [10] permanently/temporarily closed
#: listing, [0] a non-business result. A closed restaurant offered as "nearby"
#: is worse than no answer, so this is read on every place.
BUSINESS_STATUS_PATH = (146,)
BUSINESS_STATUS = {9: "operating", 10: "closed"}


def dig(blob: Any, path: tuple[int, ...]) -> Any:
    """Walk a positional path, returning None rather than raising."""
    cur = blob
    for step in path:
        if not isinstance(cur, list) or len(cur) <= step:
            return None
        cur = cur[step]
    return cur


def extract(blob: Any) -> dict | None:
    """Pull every declared field, or None if a required one fails validation.

    Validation is what turns a shifted index from a wrong answer into no
    answer. Optional fields that fail simply come back None; a required field
    that fails means this is not a place blob at all.
    """
    out: dict = {}
    for field in PLACE_FIELDS:
        value = dig(blob, field.path)
        if field.valid(value):
            out[field.name] = value
        elif field.required:
            return None
        else:
            out[field.name] = None
    return out


# --- output shaping ----------------------------------------------------------
#
# The JSON payload has to fit through the caller's output cap. A Bash tool
# result is truncated in the MIDDLE — head and tail kept — so an oversized
# document does not arrive short, it arrives *unparseable*. Measured:
# `nearby --with-hours --json --limit 20` is ~66 KB against a ~30 KB cap, and
# the truncated remains fail json.loads outright.
#
# So the default payload carries what a decision needs, and everything else
# moves behind --full. SKILL.md already tells Claude not to report coordinates,
# feature ids and URLs; the payload should not ship them by default either.

#: What a human actually decides on.
SUMMARY_FIELDS = (
    "name", "address", "rating", "status", "status_detail", "hours_today",
    "editorial", "neighbourhood", "phone",
    "travel_minutes", "travel_km", "travel_mode", "traffic_aware",
    # The departure and arrival ARE the transit answer, and straight_km is what
    # the table prints when a place has no travel time. Trimming either left
    # `nearby --mode transit` with no clock times unless --full was passed.
    "transit", "straight_km",
    "hours_week", "open_at", "business_status",
    # Failures ride in the DEFAULT payload, not behind --full. A consumer that
    # cannot see "this lookup failed" reads a missing schedule as "no hours
    # published", which is the false negative the whole package guards against.
    "hours_error", "hours_skipped", "travel_error", "travel_skipped",
    "matched_query",
)

#: Identifiers, geometry and duplicated representations. Useful for chaining,
#: dead weight in a report.
FULL_ONLY_FIELDS = (
    "lat", "lng", "ftid", "place_id", "maps_url", "website", "timezone",
    "city_region",
    "categories", "route_via", "free_flow_minutes",
    "traffic_range", "hours_week_days", "hours_week_source",
    "open_at_query",
)


def project(place: dict, full: bool) -> dict:
    """Trim a place record for output.

    `hours_week` (seven display lines) and `hours_week_days` (the structured
    spans) are the same information twice; only the display form survives by
    default, which is most of the with-hours cost on its own.
    """
    if full:
        return place
    return {k: v for k, v in place.items()
            if k in SUMMARY_FIELDS and v is not None}
