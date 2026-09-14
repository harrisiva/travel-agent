"""Typed records for google-hotels — the contract every lane codes against.

Raw payload arrays are converted to these exactly once, in `parse.py`.
`client.py` and `cli.py` see only these types. Every dataclass is frozen and
has `to_dict()`, and the field names here are the JSON field names in
04-cli-design.md §6 — the self-check walks SKILL.md's key table against them.

Two rules live here because every command depends on them:

* `comparable(price, basis)` is THE ranking path (04 §6.2). `cheapest`,
  `best`, `--max-price`, `--max-total`, `--under`, `--under-total` and
  `--sort` call it and nothing else. Nothing reads `incl_tax` directly to rank.
* `TIE_MARGIN` — a single-figure row (inferred basis) is "cheapest" only if it
  undercuts the best two-basis row by at least this much in the page's
  currency; otherwise it is "≈ same price, basis not stated".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

Basis = Literal["both", "single"]
CompareBasis = Literal["incl", "ex"]
Qualifier = Literal["free", "extra_charge", "24h"]

#: Google's qualifier enum on amenity/highlight items (01 §H5).
QUALIFIERS: dict[int, Qualifier] = {1: "free", 2: "extra_charge", 3: "24h"}

#: A single-figure seller must undercut the best two-basis seller by this much
#: (in the page's currency) to be called cheapest (04 §6.2 tie rule).
TIE_MARGIN = 1.00

#: Token kind field: 1 hotel, 2 vacation rental.
KIND_HOTEL, KIND_RENTAL = 1, 2


@dataclass(frozen=True)
class Price:
    """One rate as Google rendered it. Never derived from another figure.

    basis "both": ex_tax and incl_tax are floats from the two slots.
    basis "single": the row carried one figure with no stated basis; it is in
    `amount`, ex_tax/incl_tax are None, and the evidence says it is all-in
    (01 §H5b). Human output labels it "single figure, treated as all-in".
    """

    ex_tax: float | None
    incl_tax: float | None
    currency: str
    basis: Basis = "both"
    amount: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ex_tax": self.ex_tax,
            "incl_tax": self.incl_tax,
            "currency": self.currency,
            "basis": self.basis,
        }
        if self.basis == "single":
            d["amount"] = self.amount
        return d


def comparable(price: Price | None, basis: CompareBasis) -> float | None:
    """The one figure a price is ranked or thresholded on (04 §6.2)."""
    if price is None:
        return None
    if price.basis == "both":
        return price.incl_tax if basis == "incl" else price.ex_tax
    # single-figure row: all-in per the evidence, so comparable on incl only
    return price.amount if basis == "incl" else None


@dataclass(frozen=True)
class FreeCancellation:
    """`o[12][12][1]`: [1, "Nov 1", "4:00 PM", "11/1"], or [0] / null.

    `shown` False means no deadline was shown — which is NOT "non-refundable".
    `deadline_text` is the normalised rendering (U+202F → space); `raw` is kept.
    """

    shown: bool
    deadline_text: str | None
    raw: list[Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {"shown": self.shown, "deadline_text": self.deadline_text, "raw": self.raw}


@dataclass(frozen=True)
class SellerRow:
    """One seller from the union of p[2] ∪ p[12] ∪ p[21] ∪ p[22] (04 §3.2)."""

    seller: str
    partner_id: int | None
    own_site: bool
    nightly: Price
    stay: Price
    free_cancellation: FreeCancellation
    rooms: tuple[str, ...] = ()

    @property
    def basis(self) -> Basis:
        return self.nightly.basis

    def to_dict(self) -> dict[str, Any]:
        return {
            "seller": self.seller,
            "partner_id": self.partner_id,
            "own_site": self.own_site,
            "basis": self.basis,
            "nightly": self.nightly.to_dict(),
            "stay": self.stay.to_dict(),
            "free_cancellation": self.free_cancellation.to_dict(),
            "rooms": list(self.rooms),
        }


@dataclass(frozen=True)
class Headline:
    """Google's lead price (p[1]) and the seller row it float-matches, if any."""

    seller: str | None
    partner_id: int | None
    nightly: Price
    stay: Price | None
    matched_row: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "seller": self.seller,
            "partner_id": self.partner_id,
            "nightly": self.nightly.to_dict(),
            "stay": self.stay.to_dict() if self.stay else None,
            "matched_row": self.matched_row,
        }


@dataclass(frozen=True)
class Breakdown:
    """p[44]: Google's own [base, taxes, fees, total] for the stay. Never computed."""

    base: float
    taxes: float
    fees: float
    total: float

    @property
    def fees_share(self) -> float | None:
        return (self.fees / self.total) if self.total else None

    def to_dict(self) -> dict[str, Any]:
        return {"base": self.base, "taxes": self.taxes, "fees": self.fees, "total": self.total}


@dataclass(frozen=True)
class RatingSource:
    name: str
    score: float | None
    scale: float | None
    count: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Rating:
    score: float | None
    reviews: int | None
    histogram: tuple[tuple[int, int, int], ...] = ()  # (stars, percent, count)
    sources: tuple[RatingSource, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "reviews": self.reviews,
            "histogram": [list(h) for h in self.histogram],
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass(frozen=True)
class Highlight:
    """One of the four chips e[10][6][1] = [has, id, qualifier?] (04 §3.2.1)."""

    id: int
    name: str | None
    qualifier: Qualifier | None
    qualifier_raw: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Amenity:
    """One grouped item [has, id, qualifier?]; has=False means Google lists it as absent."""

    id: int | None
    name: str | None
    has: bool
    qualifier: Qualifier | None = None
    qualifier_raw: int | None = None
    negated_label: str | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Rental:
    sleeps: int | None
    bedrooms: int | None
    bathrooms: int | None
    beds: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Stay:
    """The stay as REQUESTED (validated up front, 04 §5)."""

    checkin: date
    checkout: date
    adults: int
    child_ages: tuple[int, ...]
    currency: str

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkin": self.checkin.isoformat(),
            "checkout": self.checkout.isoformat(),
            "nights": self.nights,
            "adults": self.adults,
            "child_ages": list(self.child_ages),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class Echo:
    """The stay as GOOGLE PRICED IT — ds:1[0][6][1][4], [13], [3] (04 §5 step 4).

    A mismatch against `Stay` is exit 3, never a price and never "no rates".
    """

    checkin: date | None
    checkout: date | None
    nights: int | None
    adults: int | None
    child_ages: tuple[int, ...] | None  # None = Google echoed no occupancy (a mismatch when ts was sent)
    currency: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkin": self.checkin.isoformat() if self.checkin else None,
            "checkout": self.checkout.isoformat() if self.checkout else None,
            "nights": self.nights,
            "adults": self.adults,
            "child_ages": list(self.child_ages) if self.child_ages is not None else None,
            "currency": self.currency,
        }

    def matches(self, stay: Stay) -> bool:
        return (
            self.checkin == stay.checkin
            and self.checkout == stay.checkout
            and self.adults == stay.adults
            and self.child_ages is not None
            and tuple(self.child_ages) == tuple(stay.child_ages)
        )


@dataclass(frozen=True)
class HotelIds:
    """Every id form for one property; all interconvertible offline (04 §10)."""

    ftid: str | None
    place_id: str | None
    cid: int | None
    token: str
    kind: int  # KIND_HOTEL | KIND_RENTAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Candidate:
    """One entry read from a `gmaps.py search --full --json` file (04 §3.1/§3.3)."""

    name: str | None
    ids: HotelIds
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    reviews: int | None = None
    address: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"name": self.name, **self.ids.to_dict()}
        d.update(lat=self.lat, lng=self.lng, rating=self.rating, reviews=self.reviews, address=self.address)
        return d


@dataclass(frozen=True)
class Center:
    """The `from` centre in a gmaps file — the anchor for shortlist distance_km."""

    lat: float
    lng: float
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityRecord:
    """One hotel's page, parsed (01 §record, 04 §6.3). All prices Google's own."""

    name: str
    kind: int
    ids: HotelIds
    star_class: tuple[str, int] | None
    address: str | None
    phone: str | None
    website: str | None
    checkin_time: str | None
    checkout_time: str | None
    lat: float | None
    lng: float | None
    echo: Echo
    currency: str | None            # p[15]; may be None when there is no headline
    headline: Headline | None       # None when p[1] is null (still exit 0 if sellers exist)
    sellers: tuple[SellerRow, ...]  # the deduped union of four slots
    breakdown: Breakdown | None
    rating: Rating
    highlights: tuple[Highlight, ...] | None  # None for rentals
    amenities: tuple[Amenity, ...]
    amenities_unnamed: int
    rental: Rental | None
    unparsed_rows: int

    def to_dict(self) -> dict[str, Any]:
        # ids first, then the string `kind` — HotelIds.kind is the int code and
        # must not overwrite the documented "hotel"|"rental" (04 §6.3).
        hotel: dict[str, Any] = {**self.ids.to_dict()}
        hotel.update(
            name=self.name,
            kind="rental" if self.kind == KIND_RENTAL else "hotel",
            kind_code=self.kind,
            star_class={"label": self.star_class[0], "stars": self.star_class[1]} if self.star_class else None,
            address=self.address,
            phone=self.phone,
            website=self.website,
            checkin_time=self.checkin_time,
            checkout_time=self.checkout_time,
            lat=self.lat,
            lng=self.lng,
        )
        return {
            "hotel": hotel,
            "currency": self.currency,
            "headline": self.headline.to_dict() if self.headline else None,
            "sellers": [s.to_dict() for s in self.sellers],
            "breakdown": self.breakdown.to_dict() if self.breakdown else None,
            "fees_share": self.breakdown.fees_share if self.breakdown else None,
            "rating": self.rating.to_dict(),
            "highlights": [h.to_dict() for h in self.highlights] if self.highlights is not None else None,
            "amenities": [a.to_dict() for a in self.amenities],
            "amenities_unnamed": self.amenities_unnamed,
            "rental": self.rental.to_dict() if self.rental else None,
            "unparsed_rows": self.unparsed_rows,
        }


def cheapest_seller(sellers: tuple[SellerRow, ...], basis: CompareBasis) -> tuple[SellerRow | None, bool]:
    """The cheapest seller under `comparable()` plus the tie rule (04 §6.2).

    Returns (row, tie_flagged). `tie_flagged` is True when a single-figure row
    was the raw minimum but did not undercut the best two-basis row by
    TIE_MARGIN, so the two-basis row is returned and the single row should be
    reported as "≈ same price, basis not stated".
    """
    both = [(comparable(s.nightly, basis), s) for s in sellers if s.basis == "both"]
    both = [(v, s) for v, s in both if v is not None]
    single = [(comparable(s.nightly, basis), s) for s in sellers if s.basis == "single"]
    single = [(v, s) for v, s in single if v is not None]
    best_both = min(both, key=lambda t: t[0]) if both else None
    best_single = min(single, key=lambda t: t[0]) if single else None
    if best_both is None:
        return (best_single[1] if best_single else None), False
    if best_single is None:
        return best_both[1], False
    if best_single[0] <= best_both[0] - TIE_MARGIN:
        return best_single[1], False
    return best_both[1], best_single[0] < best_both[0]
