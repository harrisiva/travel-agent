"""Data model for the Camis5 reservation API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import IntEnum


class Availability(IntEnum):
    """Values of `resourceAvailabilities[][].availability`.

    Decoded from the Camis5 Angular bundle.
    """

    AVAILABLE = 0
    UNAVAILABLE = 1          # reservable, but already booked
    NOT_OPERATING = 2        # outside the park's operating season
    NON_RESERVABLE = 3       # first-come-first-served / not bookable online
    CLOSED = 4
    INVALID = 5              # equipment or party size does not fit this site
    INVALID_BOOKING_CATEGORY = 6
    PARTIALLY_AVAILABLE = 7
    HELD = 8

    @property
    def bookable(self) -> bool:
        return self is Availability.AVAILABLE


#: The API silently returns an empty body for spans longer than this.
MAX_SPAN_DAYS = 367


class ResourceType(IntEnum):
    """`resourceCategory.resourceType` — what kind of thing is being booked."""

    ONSITE = 0       # campsites, cabins, yurts, oTENTiks, cottages
    MARINE = 1       # boat slips
    ACTIVITY = 2     # shuttles, guided hikes, parking, day-use permits
    BACKCOUNTRY = 3  # backcountry sites, zones, huts, shelters
    RENTAL = 4       # gear: skis, boots, adapters


#: Booking-category IDs are TENANT-SPECIFIC — id 2 is "Group Campsite" on Parks
#: Canada, "Roofed Accommodation" on Ontario Parks and "Cabin" on BC Parks. So
#: an alias resolves by matching these keywords against each tenant's own
#: category names at runtime, in order, rather than hardcoding a number.
BOOKING_ALIASES: dict[str, tuple[str, ...]] = {
    "campsite": ("campsite", "camping"),
    "roofed": ("parks canada accommodation", "roofed accommodation", "cabin",
               "accommodation"),
    "cabin": ("cabin", "roofed accommodation", "parks canada accommodation"),
    "group": ("group campsite", "group campground", "group"),
    "backcountry": ("backcountry campsite", "backcountry reservation",
                    "backcountry registration", "backcountry", "wilderness"),
    "seasonal": ("seasonal",),
    "dayuse": ("day use", "daily vehicle permit", "parking"),
    "paddling": ("paddling", "canoe"),
    "hiking": ("hiking", "west coast trail", "chilkoot"),
}

#: Roofed / hard-sided stay types seen across tenants, for `--type` discovery.
#: Purely descriptive — the authoritative list comes from /api/resourcecategory.
KNOWN_STAY_TYPES: tuple[str, ...] = (
    "oTENTik", "Ôasis", "Yurt", "MicrOcube", "Teepee", "Prospector Tent",
    "Rustic Cabin", "Cabin", "Cottage", "Equipped Camping", "Trailer Equipped",
    "Soft-sided Shelter", "Backcountry Cabin", "Backcountry Yurt",
    "Backcountry Zone Shelter", "Elfin Lakes Shelter", "Group Campsite",
    "Campsite", "Overflow",
)

#: Party-size capacity category. The one Camis constant stable across tenants.
PARTY_SIZE_CAPACITY_CATEGORY_ID = -32768


def en(localized: list[dict] | None, *keys: str) -> str:
    """Pull the English value out of a Camis `localizedValues` list.

    Never index [0] directly — Ontario Parks returns French first for some
    records ("Parc Provincial Pinery").
    """
    if not localized:
        return ""
    entry = next(
        (v for v in localized if str(v.get("cultureName", "")).startswith("en")),
        localized[0],
    )
    for key in keys:
        if entry.get(key):
            return str(entry[key])
    return ""


@dataclass(frozen=True)
class Park:
    id: int
    name: str
    short_name: str
    timezone: str

    @classmethod
    def parse(cls, raw: dict) -> "Park":
        lv = raw.get("localizedValues")
        return cls(
            id=raw["resourceLocationId"],
            name=en(lv, "fullName", "shortName"),
            short_name=en(lv, "shortName", "fullName"),
            timezone=raw.get("ianaTimeZone", ""),
        )


@dataclass(frozen=True)
class Equipment:
    category_id: int
    sub_category_id: int
    category_name: str
    name: str

    def __str__(self) -> str:
        return f"{self.name} ({self.category_name})"


@dataclass(frozen=True)
class BookingCategory:
    id: int
    name: str
    model: int | None = None


@dataclass(frozen=True)
class ResourceCategory:
    """What a bookable thing *is* — "oTENTik", "Yurt", "Rustic Cabin"."""

    id: int
    name: str
    type: ResourceType = ResourceType.ONSITE

    @property
    def roofed(self) -> bool:
        """Hard- or soft-sided accommodation rather than a bare campsite."""
        import re

        n = self.name.lower()
        if "campsite" in n or n in ("overflow", "group", "other"):
            return False
        # Word-boundary matching: a plain substring test makes "hut" match
        # "s-hut-tle" and "tent" match unrelated names.
        return any(
            re.search(rf"\b{re.escape(k)}", n)
            for k in ("cabin", "yurt", "otentik", "ôasis", "oasis", "cottage",
                      "teepee", "microcube", "shelter", "tent", "equipped",
                      "hut", "hostel", "dome")
        )


@dataclass(frozen=True)
class AttributeDef:
    """A site attribute definition, e.g. "Service Type" -> {0: Non-Electric, 1: Electric}."""

    id: int
    name: str
    values: dict[int, str] = field(default_factory=dict)  # enumValue -> label
    filterable: bool = False
    min_value: float | None = None
    max_value: float | None = None

    @property
    def numeric(self) -> bool:
        return not self.values

    @classmethod
    def parse(cls, raw: dict) -> "AttributeDef":
        return cls(
            id=raw["attributeDefinitionId"],
            name=en(raw.get("localizedValues"), "displayName", "name"),
            values={
                v["enumValue"]: en(v.get("localizedValues"), "displayName")
                for v in (raw.get("values") or [])
                if v.get("isActive", True)
            },
            filterable=bool(raw.get("isFilterable")),
            min_value=raw.get("minValue"),
            max_value=raw.get("maxValue"),
        )


@dataclass(frozen=True)
class SiteInfo:
    """Static metadata for one campsite, from /api/resourcelocation/resources.

    Everything here is free — it rides along in a payload the search already
    fetches, so attribute filtering costs no extra requests.
    """

    resource_id: int
    name: str
    description: str = ""
    category: str = ""            # "oTENTik", "Yurt", "Campsite", ...
    category_id: int | None = None
    max_capacity: int | None = None
    max_stay: int | None = None
    photo_count: int = 0
    photos: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    allowed_equipment: frozenset[tuple[int, int]] = frozenset()

    def matches(self, wanted: dict[str, str]) -> bool:
        """Case-insensitive attribute match, e.g. {"Service Type": "Electric"}."""
        lowered = {k.lower(): v.lower() for k, v in self.attributes.items()}
        for key, value in wanted.items():
            got = lowered.get(key.lower())
            if got is None or value.lower() not in got:
                return False
        return True


@dataclass(frozen=True)
class Site:
    """One campsite's status over the nights of a stay."""

    resource_id: int
    name: str
    map_name: str
    nights: tuple[Availability, ...]
    info: SiteInfo | None = None

    @property
    def available(self) -> bool:
        return bool(self.nights) and all(n.bookable for n in self.nights)

    @property
    def free_nights(self) -> int:
        return sum(1 for n in self.nights if n.bookable)

    @property
    def partial(self) -> bool:
        return not self.available and self.free_nights > 0

    @property
    def status(self) -> Availability:
        """A single collapsed status for display."""
        distinct = set(self.nights)
        if len(distinct) == 1:
            return next(iter(distinct))
        if self.free_nights:
            return Availability.PARTIALLY_AVAILABLE
        # No free nights and mixed reasons — report the most common blocker.
        return max(distinct, key=lambda s: self.nights.count(s))

    @property
    def description(self) -> str:
        return self.info.description if self.info else ""


@dataclass(frozen=True)
class Opening:
    """A bookable (site, check-in date, nights) triple found by a sweep."""

    site_id: int
    site_name: str
    map_name: str
    start: date
    nights: int
    info: SiteInfo | None = None

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.nights)

    @property
    def weekend(self) -> bool:
        """True if the stay covers a Friday or Saturday night."""
        return any(
            (self.start + timedelta(days=i)).weekday() in (4, 5)
            for i in range(self.nights)
        )


@dataclass
class SearchResult:
    park: Park
    start: date
    end: date
    equipment: Equipment
    party_size: int
    sites: list[Site] = field(default_factory=list)
    requests: int = 0

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    @property
    def available(self) -> list[Site]:
        return [s for s in self.sites if s.available]

    @property
    def partial(self) -> list[Site]:
        return [s for s in self.sites if s.partial]

    def histogram(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.sites:
            out[s.status.name] = out.get(s.status.name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
