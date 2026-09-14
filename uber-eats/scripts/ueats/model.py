"""Frozen data model — the contract every lane codes against (design 02 §4–§7).

Nothing here touches the network. All money is carried as raw cents (which
Uber can send as a fraction, e.g. 1087.5) plus a half-up rounded display
string, so no float noise ever reaches a user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

SCHEMA_VERSION = 1

DealType = Literal["bogo", "percent", "dollar", "free_delivery", "other"]

CURRENCY_SYMBOLS = {"CAD": "$", "USD": "$", "GBP": "£", "EUR": "€", "AUD": "$", "NZD": "$", "MXN": "$", "JPY": "¥"}


# --- money -------------------------------------------------------------------

@dataclass(frozen=True)
class Money:
    cents: float          # raw from Uber; may be fractional (1087.5)
    currency: str         # "CAD"

    @property
    def amount(self) -> str:
        """Half-EVEN to 2 places — the rule Uber's own taglines follow: 1087.5 →
        $10.88, 1012.5 → $10.12, 1181.25 → $11.81 (store_pctoff capture). Half-up
        would show a cent more than Uber on 1012.5."""
        return str((Decimal(str(self.cents)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))

    @property
    def value(self) -> Decimal:
        return Decimal(str(self.cents)) / Decimal(100)

    def display(self) -> str:
        sym = CURRENCY_SYMBOLS.get(self.currency, "")
        return f"{sym}{self.amount}" if sym else f"{self.amount} {self.currency}"

    def to_dict(self) -> dict:
        return {"cents": self.cents, "amount": self.amount, "currency": self.currency}


# --- deals -------------------------------------------------------------------

@dataclass(frozen=True)
class Deal:
    text: str                       # stripped, as Uber shows it
    type: DealType
    percent: int | None = None      # "20% off" -> 20
    amount: str | None = None       # "$5 off" -> "5.00"
    min_spend: str | None = None    # "$5 off $20+" -> "20.00"
    select_items: bool = False      # "… select items"
    raw_type: str | None = None     # Uber's promotionType when present (BOGO, PERCENT, …)

    def to_dict(self) -> dict:
        return {"text": self.text, "type": self.type, "percent": self.percent, "amount": self.amount,
                "min_spend": self.min_spend, "select_items": self.select_items, "raw_type": self.raw_type}


# --- location ----------------------------------------------------------------

@dataclass(frozen=True)
class Location:
    line1: str
    line2: str
    reference: str
    reference_type: str             # "uber_places" | "google_places"
    latitude: float | None
    longitude: float | None
    formatted: str = ""             # eaterFormattedAddress

    @property
    def label(self) -> str:
        return f"{self.line1}, {self.line2}" if self.line2 else self.line1

    def cookie(self) -> dict:
        """The exact uev2.loc object (design 01 §3.2)."""
        return {
            "address": {"address1": self.line1, "address2": self.line2, "aptOrSuite": "",
                        "eaterFormattedAddress": self.formatted or self.line2, "subtitle": self.line2,
                        "title": self.line1, "uuid": ""},
            "latitude": self.latitude, "longitude": self.longitude,
            "reference": self.reference, "referenceType": self.reference_type,
            "type": self.reference_type, "source": "manual_auto_complete",
        }

    def to_dict(self) -> dict:
        return {"line1": self.line1, "line2": self.line2, "reference": self.reference,
                "reference_type": self.reference_type, "latitude": self.latitude,
                "longitude": self.longitude}


@dataclass(frozen=True)
class PlaceCandidate:
    id: str
    provider: str
    line1: str
    line2: str

    def to_dict(self) -> dict:
        return {"id": self.id, "provider": self.provider, "line1": self.line1, "line2": self.line2}


# --- the feed ----------------------------------------------------------------

@dataclass(frozen=True)
class StoreRow:
    uuid: str
    name: str
    rating: float | None
    rating_count_text: str | None
    eta_min: int | None
    eta_max: int | None
    distance_km: float | None       # haversine from the address; None without one
    deals: tuple[Deal, ...]
    exclusive: bool
    delivery_fee_text: str | None
    url_id: str | None              # base64url id from actionUrl
    latitude: float | None
    longitude: float | None

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "name": self.name, "rating": self.rating,
                "rating_count_text": self.rating_count_text, "eta_min": self.eta_min,
                "eta_max": self.eta_max, "distance_km": self.distance_km,
                "deals": [d.to_dict() for d in self.deals], "exclusive": self.exclusive,
                "delivery_fee_text": self.delivery_fee_text, "url_id": self.url_id}


@dataclass(frozen=True)
class FeedMeta:
    currency: str | None
    in_service_area: bool
    stores_returned: int            # distinct storeUuids after dedupe
    offset: int | None
    has_more: bool | None

    def to_dict(self) -> dict:
        return {"currency": self.currency, "in_service_area": self.in_service_area,
                "stores_returned": self.stores_returned, "offset": self.offset, "has_more": self.has_more}


# --- a store and its menu ----------------------------------------------------

@dataclass(frozen=True)
class Dish:
    uuid: str
    title: str
    description: str | None
    section: str | None             # first non-"Featured items" section it appears in
    price: Money                    # the price shown — ALREADY the sale price when discounted
    was: Money | None               # original price from the line-through span, else None
    deal: Deal | None               # "25% off", "Buy 1, get 1 free"
    sold_out: bool
    has_options: bool
    section_uuid: str | None
    subsection_uuid: str | None
    note: str | None = None         # "Earn $7 Uber Cash for photo" — a reward, not a discount
    price_unclear: bool = False     # two amounts in the markup but no line-through

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "title": self.title, "description": self.description,
                "section": self.section, "price": self.price.to_dict(),
                "was": self.was.to_dict() if self.was else None,
                "deal": self.deal.to_dict() if self.deal else None, "sold_out": self.sold_out,
                "has_options": self.has_options, "note": self.note, "price_unclear": self.price_unclear}


@dataclass(frozen=True)
class HoursSpan:
    start_minutes: int              # minutes since midnight (660 = 11:00)
    end_minutes: int

    def to_dict(self) -> dict:
        return {"start": f"{self.start_minutes // 60:02d}:{self.start_minutes % 60:02d}",
                "end": f"{self.end_minutes // 60:02d}:{self.end_minutes % 60:02d}"}


@dataclass(frozen=True)
class Store:
    uuid: str
    title: str                      # includes the branch: "Albert's … (St. Claire Ave W)"
    slug: str | None
    address: str | None
    is_open: bool
    is_orderable: bool
    closed_message: str | None
    hours: dict[str, tuple[HoursSpan, ...]]   # dayRange text -> spans
    rating: float | None
    rating_count_text: str | None   # "6000+"
    eta_text: str | None            # "40–60 Min" (None without an address)
    pickup_eta_text: str | None
    distance_km: float | None       # from distanceBadge; None without an address
    within_range: bool | None
    cuisines: tuple[str, ...]
    phone: str | None
    currency: str
    latitude: float | None
    longitude: float | None
    has_store_promotion: bool
    deals: tuple[Deal, ...]         # distinct deals carried by dishes
    dishes: tuple[Dish, ...]        # deduped by uuid
    entries_total: int              # catalog entries before dedupe (109)
    duplicates_removed: int         # 109 - 98 = 11

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "title": self.title, "slug": self.slug, "address": self.address,
                "is_open": self.is_open, "is_orderable": self.is_orderable,
                "closed_message": self.closed_message,
                "hours": {d: [s.to_dict() for s in spans] for d, spans in self.hours.items()},
                "rating": self.rating, "rating_count_text": self.rating_count_text,
                "eta": self.eta_text, "pickup_eta": self.pickup_eta_text,
                "distance_km": self.distance_km, "within_range": self.within_range,
                "cuisines": list(self.cuisines), "phone": self.phone, "currency": self.currency,
                "has_store_promotion": self.has_store_promotion,
                "deals": [d.to_dict() for d in self.deals],
                "dishes_total": len(self.dishes),
                "dishes_sold_out": sum(1 for d in self.dishes if d.sold_out),
                "duplicates_removed": self.duplicates_removed}


# --- one dish's options ------------------------------------------------------

@dataclass(frozen=True)
class Option:
    uuid: str
    title: str
    price: Money                    # add-on price; 0 cents when free
    sold_out: bool
    min_qty: int
    max_qty: int
    default_qty: int
    groups: tuple["OptionGroup", ...] = ()   # nested childCustomizationList

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "title": self.title, "price": self.price.to_dict(),
                "sold_out": self.sold_out, "min": self.min_qty, "max": self.max_qty,
                "default": self.default_qty, "groups": [g.to_dict() for g in self.groups]}


@dataclass(frozen=True)
class OptionGroup:
    uuid: str
    title: str
    min_permitted: int
    max_permitted: int
    options: tuple[Option, ...]

    @property
    def required(self) -> bool:
        return self.min_permitted > 0

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "title": self.title, "required": self.required,
                "min": self.min_permitted, "max": self.max_permitted,
                "options": [o.to_dict() for o in self.options]}


@dataclass(frozen=True)
class ItemDetail:
    uuid: str
    title: str
    price: Money
    was: Money | None
    deal: Deal | None
    sold_out: bool
    groups: tuple[OptionGroup, ...]
    from_price: Money               # price + cheapest required picks (sold-out excluded)
    required_groups: int

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "title": self.title, "price": self.price.to_dict(),
                "was": self.was.to_dict() if self.was else None,
                "deal": self.deal.to_dict() if self.deal else None, "sold_out": self.sold_out,
                "groups": [g.to_dict() for g in self.groups], "from_price": self.from_price.to_dict(),
                "required_groups": self.required_groups}


# --- name matching -----------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    row: StoreRow
    match: Literal["exact", "all_tokens", "prefix"]

    def to_dict(self) -> dict:
        return {**self.row.to_dict(), "match": self.match}


# --- helpers shared by lanes -------------------------------------------------

def from_price(base: Money, groups: tuple[OptionGroup, ...]) -> Money:
    """Base + for each required group the cheapest min_permitted in-stock options
    (recursing into the chosen option's own required groups)."""
    extra = 0.0
    for g in groups:
        if g.min_permitted <= 0:
            continue
        avail = sorted((o for o in g.options if not o.sold_out), key=lambda o: o.price.cents)
        picks = avail[: g.min_permitted]
        for o in picks:
            extra += o.price.cents + from_price(Money(0, base.currency), o.groups).cents
    return Money(base.cents + extra, base.currency)


def fold(text: str) -> str:
    """Case/accent/punctuation-insensitive form for name and dish matching."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("&", " and ")
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def envelope(command: str, ok: bool, **fields: Any) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": ok, "command": command, **fields}
