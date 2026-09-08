"""Client for the Camis5 reservation API used across Canadian park systems.

The API is unauthenticated and returns JSON. All IDs are large negative int32
values and are **tenant-specific** — equipment, booking-category and attribute
IDs differ between Parks Canada, Ontario Parks, GRCA, etc., so they are
discovered at runtime rather than hardcoded.

The core primitive is `daily()`: one request per map returns a per-night status
array for every site over an arbitrary span (up to 367 days). Point searches,
date sweeps and site calendars are all computed from that one matrix, so a
whole-season sweep costs exactly what a single weekend search costs.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Iterable, Sequence

from .cache import Cache
from .http import Transport
from .model import (
    MAX_SPAN_DAYS,
    PARTY_SIZE_CAPACITY_CATEGORY_ID,
    AttributeDef,
    Availability,
    BOOKING_ALIASES,
    BookingCategory,
    Equipment,
    Opening,
    Park,
    SearchResult,
    ResourceCategory,
    ResourceType,
    Site,
    SiteInfo,
    en,
)
from .providers import resolve


class SpanTooLongError(ValueError):
    """Raised rather than letting the API silently return an empty body."""


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


class CamisClient:
    """Read-only client for one Camis5 tenant."""

    def __init__(
        self,
        provider: str,
        timeout: int = 60,
        use_cache: bool = True,
        max_requests: int = 200,
    ):
        self.host = resolve(provider)
        self._http = Transport(self.host, timeout=timeout)
        self._cache = Cache(enabled=use_cache)
        self._mem: dict[str, object] = {}
        self.max_requests = max_requests
        self.requests = 0

    # ---------- plumbing ----------

    def _reference(self, key: str, path: str, **params) -> object:
        """Fetch reference data, memoised in-process and on disk."""
        if key in self._mem:
            return self._mem[key]
        cached = self._cache.get(self.host, key)
        if cached is None:
            cached = self._http.get(path, **params)
            self._cache.set(self.host, key, cached)
        self._mem[key] = cached
        return cached

    def _spend(self, n: int = 1) -> None:
        if self.requests + n > self.max_requests:
            raise RuntimeError(
                f"request ceiling reached ({self.max_requests}). Narrow the search "
                f"(fewer maps, shorter span) or raise --max-requests."
            )
        self.requests += n

    # ---------- reference data ----------

    def parks(self) -> list[Park]:
        raw = self._reference("parks", "/api/resourceLocation")
        return sorted((Park.parse(r) for r in raw), key=lambda p: p.name)

    def find_park(self, query: str) -> Park:
        q = query.strip().lower()
        parks = self.parks()
        exact = [p for p in parks if p.name.lower() == q]
        if exact:
            return exact[0]
        matches = [p for p in parks if q in p.name.lower()]
        if not matches:
            raise LookupError(f"No park on {self.host} matching {query!r}")
        if len(matches) > 1:
            names = "\n  ".join(p.name for p in matches[:20])
            more = "" if len(matches) <= 20 else f"\n  ... and {len(matches) - 20} more"
            raise LookupError(
                f"{len(matches)} parks match {query!r}; be more specific:\n  {names}{more}"
            )
        return matches[0]

    def find_parks(self, query: str) -> list[Park]:
        """Every park matching a substring — the plural of `find_park`.

        "Algonquin" is 17 separate resourceLocationIds on Ontario Parks, so a
        user asking about "Algonquin" is asking about all of them. An empty
        query means every park on the tenant.
        """
        q = query.strip().lower()
        if not q:
            return self.parks()
        matches = [p for p in self.parks() if q in p.name.lower()]
        if not matches:
            raise LookupError(f"No park on {self.host} matching {query!r}")
        return matches

    def equipment(self) -> list[Equipment]:
        raw = self._reference("equipment", "/api/equipment")
        return [
            Equipment(
                category_id=cat["equipmentCategoryId"],
                sub_category_id=sub["subEquipmentCategoryId"],
                category_name=en(cat.get("localizedValues"), "name"),
                name=en(sub.get("localizedValues"), "name"),
            )
            for cat in raw
            for sub in cat.get("subEquipmentCategories", [])
        ]

    def find_equipment(self, query: str) -> Equipment:
        q = query.strip().lower()
        options = self.equipment()
        exact = [e for e in options if e.name.lower() == q]
        if exact:
            return exact[0]
        matches = [e for e in options if q in e.name.lower()]
        if not matches:
            listing = "\n  ".join(str(e) for e in options)
            raise LookupError(f"No equipment matching {query!r}. Options:\n  {listing}")
        return matches[0]

    def booking_categories(self) -> list[BookingCategory]:
        raw = self._reference("bookingcategories", "/api/bookingcategories")
        return [
            BookingCategory(
                c["bookingCategoryId"],
                en(c.get("localizedValues"), "name"),
                c.get("bookingModel"),
            )
            for c in raw
        ]

    def resource_categories(self) -> dict[int, ResourceCategory]:
        """What each bookable thing *is* — oTENTik, Yurt, Rustic Cabin, ..."""
        raw = self._reference("resourcecategory", "/api/resourcecategory")
        out = {}
        for c in raw:
            try:
                rtype = ResourceType(c.get("resourceType", 0))
            except ValueError:
                rtype = ResourceType.ONSITE
            out[c["resourceCategoryId"]] = ResourceCategory(
                c["resourceCategoryId"], en(c.get("localizedValues"), "name"), rtype
            )
        return out

    def find_booking_category(self, query: str | int) -> int:
        """Resolve an id, an exact name, a substring, or a portable alias.

        Aliases exist because the IDs are tenant-specific: "roofed" is
        `Parks Canada Accommodation` (1) on Parks Canada, `Roofed
        Accommodation` (2) on Ontario and `Cabin` (2) on BC.
        """
        if isinstance(query, int):
            return query
        if query.lstrip("-").isdigit():
            return int(query)
        q = query.strip().lower()
        cats = self.booking_categories()

        for c in cats:                                    # exact name
            if c.name.lower() == q:
                return c.id
        for keyword in BOOKING_ALIASES.get(q, ()):        # portable alias
            for c in cats:
                if keyword == c.name.lower():
                    return c.id
            for c in cats:
                if keyword in c.name.lower():
                    return c.id
        for c in cats:                                    # plain substring
            if q in c.name.lower():
                return c.id
        listing = ", ".join(f"{c.name} ({c.id})" for c in cats)
        raise LookupError(
            f"No booking category matching {query!r} on {self.host}.\n"
            f"  aliases: {', '.join(sorted(BOOKING_ALIASES))}\n"
            f"  this tenant offers: {listing}"
        )

    def stay_types(self, park_id: int | None = None) -> dict[str, int]:
        """Bookable stay types and how many of each exist.

        Tenant-wide by default; pass a park id to scope it to one park.
        """
        cats = self.resource_categories()
        counts: dict[str, int] = {}
        parks = [park_id] if park_id is not None else [p.id for p in self.parks()]
        for pid in parks:
            for info in self.site_info(pid).values():
                if info.category:
                    counts[info.category] = counts.get(info.category, 0) + 1
        if park_id is None and not counts:  # fall back to the catalogue itself
            counts = {c.name: 0 for c in cats.values()}
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def attributes(self) -> dict[int, AttributeDef]:
        raw = self._reference("attributes", "/api/attribute/filterable")
        return {int(k): AttributeDef.parse(v) for k, v in raw.items()}

    def maps(self, park_id: int, leaf_only: bool = True) -> list[dict]:
        """Maps for a park. Leaf maps are the ones that contain campsites."""
        raw = self._reference(
            f"maps:{park_id}", "/api/maps", resourceLocationId=park_id
        )
        return [m for m in raw if m.get("mapResources")] if leaf_only else list(raw)

    def site_info(self, park_id: int) -> dict[int, SiteInfo]:
        """resource_id -> static metadata, including decoded attributes."""
        raw = self._reference(
            f"resources:{park_id}",
            "/api/resourcelocation/resources",
            resourceLocationId=park_id,
        )
        defs = self.attributes()
        cats = self.resource_categories()
        out: dict[int, SiteInfo] = {}
        for rid, r in raw.items():
            attrs: dict[str, str] = {}
            for a in r.get("definedAttributes") or []:
                d = defs.get(a.get("attributeDefinitionId"))
                if not d:
                    continue
                if d.values:
                    labels = [d.values[v] for v in (a.get("values") or []) if v in d.values]
                    if labels:
                        attrs[d.name] = ", ".join(labels)
                elif a.get("value") is not None:
                    attrs[d.name] = str(a["value"])
            cat = cats.get(r.get("resourceCategoryId"))
            photos = tuple(
                p["photoUrlResult"]["url"]
                for p in (r.get("photos") or [])
                if (p.get("photoUrlResult") or {}).get("url")
            )
            out[int(rid)] = SiteInfo(
                resource_id=int(rid),
                name=en(r.get("localizedValues"), "name"),
                description=en(r.get("localizedValues"), "description"),
                category=cat.name if cat else "",
                category_id=r.get("resourceCategoryId"),
                max_capacity=r.get("maxCapacity"),
                max_stay=r.get("maxStay"),
                photo_count=len(photos),
                photos=photos,
                attributes=attrs,
                allowed_equipment=frozenset(
                    (e["equipmentCategoryId"], e["subEquipmentCategoryId"])
                    for e in (r.get("allowedEquipment") or [])
                ),
            )
        return out

    def facets(self, park_id: int) -> dict[str, dict[str, int]]:
        """Attribute -> value -> number of sites, for discovering filters."""
        out: dict[str, dict[str, int]] = {}
        for info in self.site_info(park_id).values():
            for key, value in info.attributes.items():
                out.setdefault(key, {})
                out[key][value] = out[key].get(value, 0) + 1
        return {k: dict(sorted(v.items(), key=lambda kv: -kv[1])) for k, v in sorted(out.items())}

    def booking_window(self, park_id: int, upcoming_only: bool = True) -> list[dict]:
        """Operating seasons and `goLiveDate` (when booking opens).

        Camis retains every past season, so by default only seasons that have
        not finished yet are returned.
        """
        raw = self._http.get(
            "/api/dateschedule/resourcelocationid", resourceLocationId=park_id
        )
        self._spend()
        today = date.today().isoformat()
        out = []
        for sched in raw.values():
            for window in sched.get("reservableDates", []):
                dates = window.get("reservableDates") or {}
                end = dates.get("end")
                if upcoming_only and end and end[:10] < today:
                    continue
                out.append(
                    {
                        "schedule": en(sched.get("onlineLocalizedValues"), "name"),
                        "start": dates.get("start"),
                        "end": end,
                        "go_live": window.get("goLiveDate"),
                    }
                )
        return sorted(out, key=lambda w: (w["start"] or "", w["schedule"]))

    def alerts(self) -> list[dict]:
        raw = self._http.get("/api/parkalert/all")
        self._spend()
        return raw if isinstance(raw, list) else []

    # ---------- the core primitive ----------

    def daily(
        self,
        park: Park | str,
        start: date | str,
        end: date | str,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
        maps_filter: Iterable[str] | None = None,
    ) -> tuple[Park, Equipment, dict[int, tuple[str, list[Availability]]]]:
        """Per-date availability for every site in a park.

        Returns {resource_id: (map_name, [Availability per date])} where the
        list covers start..end INCLUSIVE — so it has one more entry than the
        number of nights. The final entry is the departure date and must be
        ignored when deciding whether a stay is bookable.

        Cost: exactly one HTTP request per leaf map.
        """
        if isinstance(park, str):
            park = self.find_park(park)
        if isinstance(equipment, str):
            equipment = self.find_equipment(equipment)
        category_id = self.find_booking_category(booking_category)
        start_d, end_d = _as_date(start), _as_date(end)
        if end_d <= start_d:
            raise ValueError(f"end ({end_d}) must be after start ({start_d})")
        span = (end_d - start_d).days
        if span > MAX_SPAN_DAYS:
            raise SpanTooLongError(
                f"span of {span} days exceeds the API maximum of {MAX_SPAN_DAYS}; "
                f"the server returns an empty body rather than an error. Split the range."
            )

        maps = self.maps(park.id)
        if maps_filter:
            maps = [
                m
                for m in maps
                if any(f.lower() in _map_name(m).lower() for f in maps_filter)
            ]
            if not maps:
                raise LookupError(
                    f"No maps in {park.name} match {list(maps_filter)}. "
                    f"Available: {', '.join(_map_name(m) for m in self.maps(park.id))}"
                )

        # Refuse up front rather than discovering the ceiling mid-sweep, which
        # would leave a partial grid that under-reports availability.
        if self.requests + len(maps) > self.max_requests:
            raise RuntimeError(
                f"this search needs {len(maps)} requests (one per map) and would "
                f"exceed the ceiling of {self.max_requests}. Narrow it with --map, "
                f"or raise --max-requests."
            )

        expected = span + 1
        out: dict[int, tuple[str, list[Availability]]] = {}
        for m in maps:
            self._spend()
            raw = self._http.get(
                "/api/availability/map",
                mapId=m["mapId"],
                bookingCategoryId=category_id,
                equipmentCategoryId=equipment.category_id,
                subEquipmentCategoryId=equipment.sub_category_id,
                cartUid="",
                cartTransactionUid="",
                bookingUid="",
                groupHoldUid="",
                startDate=start_d.isoformat(),
                endDate=end_d.isoformat(),
                getDailyAvailability="true",
                isReserving="true",
                filterData="[]",
                boatLength=0,
                boatDraft=0,
                boatWidth=0,
                peopleCapacityCategoryCounts=json.dumps(
                    [
                        {
                            "capacityCategoryId": PARTY_SIZE_CAPACITY_CATEGORY_ID,
                            "subCapacityCategoryId": None,
                            "count": party_size,
                        }
                    ]
                ),
                numEquipment=1,
                seed=f"{start_d.isoformat()}T00:00:00.000Z",
            )
            name = _map_name(m)
            for rid, nights in (raw.get("resourceAvailabilities") or {}).items():
                if not nights:
                    continue
                codes = [Availability(n["availability"]) for n in nights]
                if len(codes) != expected:
                    raise RuntimeError(
                        f"map {name}: expected {expected} daily entries for "
                        f"{start_d}..{end_d}, got {len(codes)} — API contract changed"
                    )
                out[int(rid)] = (name, codes)
        return park, equipment, out

    # ---------- derived queries ----------

    def search(
        self,
        park: Park | str,
        start: date | str,
        end: date | str,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
        maps_filter: Iterable[str] | None = None,
        attrs: dict[str, str] | None = None,
        types: Iterable[str] | None = None,
    ) -> SearchResult:
        """Check every site in a park for one date range.

        `end` is the departure date; the night of `end` is not part of the stay
        and its status is deliberately ignored.
        """
        before = self.requests
        park_obj, equip, matrix = self.daily(
            park, start, end, equipment, party_size, booking_category, maps_filter
        )
        start_d, end_d = _as_date(start), _as_date(end)
        nights = (end_d - start_d).days
        info = self.site_info(park_obj.id)

        result = SearchResult(park_obj, start_d, end_d, equip, party_size)
        for rid, (map_name, codes) in matrix.items():
            si = info.get(rid)
            if attrs and not (si and si.matches(attrs)):
                continue
            if types and not _type_match(si, types):
                continue
            result.sites.append(
                Site(
                    resource_id=rid,
                    name=si.name if si else str(rid),
                    map_name=map_name,
                    nights=tuple(codes[:nights]),  # drop the departure date
                    info=si,
                )
            )
        result.sites.sort(key=lambda s: (s.map_name, _natural(s.name)))
        result.requests = self.requests - before
        return result

    def sweep(
        self,
        park: Park | str,
        start: date | str,
        end: date | str,
        nights: int = 2,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
        maps_filter: Iterable[str] | None = None,
        attrs: dict[str, str] | None = None,
        types: Iterable[str] | None = None,
        weekends_only: bool = False,
        weekdays: Sequence[int] | None = None,
    ) -> list[Opening]:
        """Every (site, check-in date) that is bookable for `nights` nights.

        Costs the same as one `search` — the whole window comes back in a
        single request per map and the runs are computed locally.
        """
        park_obj, _, matrix = self.daily(
            park, start, end, equipment, party_size, booking_category, maps_filter
        )
        start_d = _as_date(start)
        info = self.site_info(park_obj.id)
        openings: list[Opening] = []

        for rid, (map_name, codes) in matrix.items():
            si = info.get(rid)
            if attrs and not (si and si.matches(attrs)):
                continue
            if types and not _type_match(si, types):
                continue
            # codes covers start..end inclusive; a stay checking in on index i
            # occupies nights i .. i+nights-1.
            for i in range(len(codes) - nights + 1):
                window = codes[i : i + nights]
                if not all(c.bookable for c in window):
                    continue
                checkin = start_d + timedelta(days=i)
                if weekdays and checkin.weekday() not in weekdays:
                    continue
                opening = Opening(
                    site_id=rid,
                    site_name=si.name if si else str(rid),
                    map_name=map_name,
                    start=checkin,
                    nights=nights,
                    info=si,
                )
                if weekends_only and not opening.weekend:
                    continue
                openings.append(opening)
        openings.sort(key=lambda o: (o.start, o.map_name, _natural(o.site_name)))
        return openings

    def sweep_many(
        self,
        parks: Sequence[Park],
        start: date | str,
        end: date | str,
        nights: int = 2,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
        attrs: dict[str, str] | None = None,
        types: Iterable[str] | None = None,
        weekends_only: bool = False,
        weekdays: Sequence[int] | None = None,
        on_progress=None,
    ) -> tuple[dict[int, list[Opening]], dict[int, str]]:
        """Sweep several parks. Returns (openings by park id, errors by park id).

        A park that fails — no maps, equipment not offered, backcountry-only —
        is recorded and skipped rather than aborting the whole search, because
        a 17-park group like Algonquin always contains a few oddities.
        """
        found: dict[int, list[Opening]] = {}
        errors: dict[int, str] = {}
        for park in parks:
            if on_progress:
                on_progress(park)
            try:
                openings = self.sweep(
                    park, start, end, nights=nights, equipment=equipment,
                    party_size=party_size, booking_category=booking_category,
                    attrs=attrs, types=types, weekends_only=weekends_only,
                    weekdays=weekdays,
                )
                if openings:
                    found[park.id] = openings
            except (LookupError, ValueError) as e:
                errors[park.id] = str(e)
            except RuntimeError:
                raise  # request ceiling — must stop, not silently under-report
        return found, errors

    def plan_requests(self, parks: Sequence[Park]) -> int:
        """How many availability requests a multi-park sweep will cost."""
        return sum(len(self.maps(p.id)) for p in parks)

    def site_calendar(
        self,
        park: Park | str,
        site_name: str,
        start: date | str,
        end: date | str,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
    ) -> tuple[SiteInfo, str, list[tuple[date, Availability]]]:
        """Day-by-day status for one named site (e.g. "285")."""
        if isinstance(park, str):
            park = self.find_park(park)
        info = self.site_info(park.id)
        wanted = site_name.strip().lower()
        matches = [i for i in info.values() if i.name.lower() == wanted]
        if not matches:
            matches = [i for i in info.values() if wanted in i.name.lower()]
        if not matches:
            raise LookupError(f"No site named {site_name!r} in {park.name}")
        if len(matches) > 1:
            names = ", ".join(sorted(i.name for i in matches)[:20])
            raise LookupError(f"{len(matches)} sites match {site_name!r}: {names}")
        target = matches[0]

        # Restrict to the map holding this site so we issue one request, not N.
        maps = [
            m
            for m in self.maps(park.id)
            if any(r["resourceId"] == target.resource_id for r in m["mapResources"])
        ]
        _, _, matrix = self.daily(
            park,
            start,
            end,
            equipment,
            party_size,
            booking_category,
            maps_filter=[_map_name(maps[0])] if maps else None,
        )
        if target.resource_id not in matrix:
            raise LookupError(
                f"site {target.name} returned no availability data — it may not "
                f"accept this equipment or booking category"
            )
        map_name, codes = matrix[target.resource_id]
        start_d = _as_date(start)
        return target, map_name, [
            (start_d + timedelta(days=i), c) for i, c in enumerate(codes)
        ]

    def horizon(
        self,
        park: Park | str,
        equipment: Equipment | str = "tent",
        party_size: int = 2,
        booking_category: int | str = 0,
        days: int = MAX_SPAN_DAYS,
        probe_maps: int = 4,
    ) -> dict:
        """The furthest date this park currently exposes for booking.

        Probes a single map over a long span and reports the last date that is
        not simply "beyond the window". NOT_OPERATING (season closed) is
        reported separately so it is not mistaken for the window edge.
        """
        if isinstance(park, str):
            park = self.find_park(park)
        maps = self.maps(park.id)
        if not maps:
            raise LookupError(f"{park.name} has no bookable maps")
        start = date.today()

        # Probe the biggest maps first and stop at the first that returns real
        # booking-window data. Probing only maps[0] reported "no booking
        # window" for Pinery, whose first map is a group campground that
        # answers INVALID to every ordinary campsite query.
        candidates = sorted(maps, key=lambda m: -len(m.get("mapResources") or []))
        matrix: dict[int, tuple[str, list[Availability]]] = {}
        probed: list[str] = []
        for m in candidates[:probe_maps]:
            probed.append(_map_name(m))
            _, _, matrix = self.daily(
                park,
                start,
                start + timedelta(days=min(days, MAX_SPAN_DAYS)),
                equipment,
                party_size,
                booking_category,
                maps_filter=[_map_name(m)],
            )
            if any(
                c in (Availability.AVAILABLE, Availability.UNAVAILABLE)
                for _, codes in matrix.values()
                for c in codes
            ):
                break
        if not matrix:
            return {
                "park": park.name,
                "probed_maps": probed,
                "error": "no availability data returned",
            }
        length = len(next(iter(matrix.values()))[1])
        last_bookable = last_known = None
        for i in range(length):
            day = start + timedelta(days=i)
            codes = [c[1][i] for c in matrix.values()]
            if any(c is Availability.AVAILABLE for c in codes):
                last_bookable = day
            if any(
                c in (Availability.AVAILABLE, Availability.UNAVAILABLE) for c in codes
            ):
                last_known = day
        return {
            "park": park.name,
            "probed_maps": probed,
            "from": start.isoformat(),
            "last_date_with_availability": last_bookable.isoformat() if last_bookable else None,
            "last_date_in_booking_window": last_known.isoformat() if last_known else None,
            "days_out": (last_known - start).days if last_known else None,
            "booking_windows": self.booking_window(park.id),
        }


def _type_match(info: SiteInfo | None, types: Iterable[str]) -> bool:
    """Match a site's resource category, e.g. "otentik", "yurt", "cabin"."""
    if not info or not info.category:
        return False
    got = info.category.lower()
    return any(t.strip().lower() in got for t in types)


def _map_name(m: dict) -> str:
    return en(m.get("localizedValues"), "title", "name") or str(m.get("mapId"))


def _natural(name: str) -> tuple:
    """Sort site "9" before "10", and "39" before "Y3".

    Every element is a (kind, number, text) triple so comparisons never cross
    types. Parks that mix numeric and lettered site names in one result set —
    Killarney (Y3, C1), Sandbanks (A209), Pinery (RA 6) — used to raise
    TypeError here and take `search`/`find` down with them.
    """
    import re

    return tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p.lower())
        for p in re.split(r"(\d+)", name)
        if p
    )
