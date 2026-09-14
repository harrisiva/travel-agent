"""Turning a positional place blob into a named record.

Index positions live in `fields.py`; this module is the small amount of
*interpretation* that a lookup table cannot express — reading Google's
open/closed wording, and the tri-state that follows from it.
"""

from __future__ import annotations

import math
from typing import Any

from gmaps import fields

#: Live status. `unknown` is a first-class answer, never folded into `closed`:
#: plenty of places publish no hours, and telling someone a place is shut when
#: Google simply doesn't know sends them away from an open restaurant.
OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"


def _status(blob: Any) -> str:
    raw = fields.dig(blob, fields.STATUS_PATH)
    if not isinstance(raw, str):
        return UNKNOWN
    low = raw.strip().lower()
    # "Closes soon" and "Closing soon" mean OPEN, and "Opens soon" means
    # CLOSED. Both wordings are live in the neighbouring status-detail field
    # ([203][1][4][0]), so a naive prefix match on this one is a plausible
    # wrong answer waiting for Google to move a string one slot. Settle the
    # two lookalikes before the prefixes get a chance to.
    if low.startswith(("closes", "closing")):
        return OPEN
    if low.startswith("opens"):
        return CLOSED
    if low.startswith("open"):
        return OPEN
    if low.startswith("clos") or "temporarily" in low or "permanently" in low:
        return CLOSED
    return UNKNOWN


def _business_status(blob: Any) -> str:
    value = fields.dig(blob, fields.BUSINESS_STATUS_PATH)
    code = value[0] if isinstance(value, list) and value else None
    return fields.BUSINESS_STATUS.get(code, "unknown")


def place_from_blob(blob: Any) -> dict | None:
    """Extract one place, or None if it fails its shape checks."""
    record = fields.extract(blob)
    if record is None:
        return None

    record["status"] = _status(blob)
    record["business_status"] = _business_status(blob)
    lat, lng = record["lat"], record["lng"]
    record["maps_url"] = (
        f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
        + (f"&query_place_id={record['place_id']}" if record.get("place_id") else "")
    )
    return record


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance. Free, offline, and always shorter than the road."""
    radius = 6371.0088
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(h))
