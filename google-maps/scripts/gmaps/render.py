"""Human-readable output. Every `print` in the package lives here.

Two audiences share one format. A person skims it in a terminal; Claude reads
it when it did not ask for `--json`. Both are served by leading with the answer
— name, how far, when it shuts — and keeping identifiers out of the way.
"""

from __future__ import annotations

STATUS_MARK = {"open": "OPEN", "closed": "CLOSED", "unknown": "hours ?"}

MODE_VERB = {"drive": "drive", "walk": "walk", "bike": "cycle",
             "transit": "transit"}


def _travel_label(place: dict) -> str | None:
    minutes = place.get("travel_minutes")
    if minutes is None:
        return None
    mode = place.get("travel_mode", "drive")
    label = f"{minutes:.0f} min {MODE_VERB.get(mode, mode)}"
    if place.get("travel_km") is not None:
        label += f" / {place['travel_km']:.1f} km"
    if place.get("traffic_aware"):
        label += " (traffic)"
    return label


def places(rows: list[dict], result=None) -> None:
    if not rows:
        print("nothing found")
        _footnotes(rows, result)
        return

    for i, place in enumerate(rows, 1):
        bits = [f"{i:2}. {place['name']}"]
        if place.get("rating") is not None:
            bits.append(f"{place['rating']}★")
        bits.append(f"[{STATUS_MARK.get(place['status'], place['status'])}]")
        travel = _travel_label(place)
        if travel:
            bits.append(travel)
        elif place.get("straight_km") is not None:
            bits.append(f"{place['straight_km']:.1f} km direct")
        print("  ".join(bits))

        detail = place.get("status_detail") or (
            f"hours today {place['hours_today']}" if place.get("hours_today") else None)
        if detail:
            print(f"      {detail}")
        if place.get("editorial"):
            print(f"      {place['editorial']}")
        if place.get("address"):
            print(f"      {place['address']}")
        if place.get("transit"):
            t = place["transit"]
            print(f"      depart {t['depart']} → arrive {t['arrive']}")
        if place.get("hours_week"):
            print("      full week:")
            for line in place["hours_week"]:
                print(f"        {line}")
    _footnotes(rows, result)


def _footnotes(rows: list[dict], result) -> None:
    """State what was excluded and why. An unexplained short list reads as a
    confident 'there is nothing', which is often not what happened."""
    notes = []
    unknown = sum(1 for p in rows if p.get("status") == "unknown")
    if unknown:
        notes.append(f"{unknown} publish no hours — status unknown, not closed")
    if result is not None:
        if getattr(result, "week_unknown", 0):
            notes.append(f"{result.week_unknown} have no published weekly schedule")
        if getattr(result, "hours_failed", 0):
            notes.append(
                f"{result.hours_failed} hours lookups FAILED — those schedules "
                "are unknown, not absent")
        if getattr(result, "skipped", 0):
            notes.append(
                f"{result.skipped} were not looked up (per-place ceiling) — "
                "raise --max-place-requests to include them")
        # Only claim --limit cut them when --limit actually did. This count is
        # taken before the hours and travel filters run, so after an --open-at
        # or --within pass the difference is mostly places that were filtered
        # out — blaming --limit sends the user to raise a number that will
        # change nothing.
        limit = getattr(getattr(result, "spec", None), "limit", None)
        more = getattr(result, "matched_before_limit", 0) - len(rows)
        if more > 0 and limit is not None and len(rows) >= limit:
            notes.append(f"{more} more matched but were cut by --limit")
        if getattr(result, "travel_unknown", 0):
            notes.append(f"{result.travel_unknown} could not be routed")
    if any(p.get("traffic_aware") for p in rows):
        notes.append("drive times include live traffic")
    elif any(p.get("travel_minutes") is not None for p in rows):
        notes.append("travel times are free-flow; no live traffic was available")
    for note in notes:
        print(f"  ({note})")


def place(one: dict) -> None:
    print(one["name"])
    asked = one.get("matched_query")
    if asked and asked.strip().lower() not in one["name"].strip().lower():
        print(f"  (nearest match for {asked!r} — check this is the right place)")
    if one.get("address"):
        print(f"  {one['address']}")
    if one.get("editorial"):
        print(f"  {one['editorial']}")
    if one.get("phone"):
        print(f"  {one['phone']}")
    print(f"\n  right now: {one.get('status_detail') or one['status']}")
    if one.get("hours_today"):
        print(f"  today:     {one['hours_today']}")
    if one.get("hours_week"):
        print("\n  full week:")
        for line in one["hours_week"]:
            print(f"    {line}")
    elif one.get("hours_error"):
        # NOT "Google publishes none": the lookup never completed. Saying the
        # place has no published hours here is a confident claim about the
        # place made out of a network failure.
        print("\n  full week: LOOKUP FAILED, hours unknown — not 'no hours'.")
        print(f"             {one['hours_error']}")
    else:
        print("\n  full week: Google publishes none for this place.")
        if one.get("website"):
            print(f"             Try {one['website']}")
    if one.get("timezone"):
        print(f"\n  (hours are local time — {one['timezone']})")


def routes(payload: dict) -> None:
    origin = payload["from"]["name"]
    mode = payload.get("mode", "drive")
    print(f"from {origin} by {MODE_VERB.get(mode, mode)}")
    for dest in payload["results"]:
        if dest.get("travel_minutes") is None:
            direct = dest.get("straight_km")
            extra = f" ({direct} km direct)" if direct is not None else ""
            # "no route found" is a claim about the road network. Say which of
            # the three things actually happened instead.
            if dest.get("travel_error"):
                print(f"  {dest['name']}: LOOKUP FAILED — {dest['travel_error']}")
            elif dest.get("travel_skipped"):
                print(f"  {dest['name']}: not looked up ({dest['travel_skipped']}) "
                      f"— raise --max-place-requests")
            else:
                print(f"  {dest['name']}: no route found{extra}")
            continue
        line = (f"  {dest['name']}: {dest['travel_minutes']:.0f} min, "
                f"{dest['travel_km']:.1f} km")
        if dest.get("traffic_range"):
            line += f"  (traffic {dest['traffic_range']})"
        if dest.get("route_via"):
            line += f"  via {dest['route_via']}"
        print(line)
        if dest.get("transit"):
            t = dest["transit"]
            print(f"      depart {t['depart']} → arrive {t['arrive']} ({t['timezone']})")
    if any(d.get("traffic_aware") for d in payload["results"]):
        print("  (includes live traffic, current conditions only)")


def location(payload: dict) -> None:
    print(f"{payload['name']}: {payload['lat']}, {payload['lng']}")
