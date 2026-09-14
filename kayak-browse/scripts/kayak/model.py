"""Data model for the KAYAK affiliate APIs — and the one place the join happens.

The car search response is normalised: `results[]` holds little more than an id
and a list of booking options, and each option refers to `agencies`,
`providers` and `carLocations` maps hanging off the response root by code.
Rendering one row means joining across three maps.

That join is done **once**, here, in `Offer.parse`. Filters, sorting, the table
and the JSON projection all read a flat `Offer`. Doing it anywhere else means
doing it three times, differently, and the third one is always the one that
looks up `agencies` with a provider code.

Two rules the RAML forces on this file:

* **Enum values are stored as raw strings.** The spec says outright: "API enum
  values may change in the future. Applications must be designed to handle both
  new and removed enum values without breaking application logic." So nothing
  here is an `Enum` — a new fuel type or a new badge must flow through to
  output, not raise. `displayName` is kept alongside every code so a value we
  have never seen still prints as something a human can read. This is the same
  class of bug as the int/str site-name comparator described in CLAUDE.md: it
  passes every test until real data contains something new.
* **Every field is `.get()`-guarded.** `price`, `car` and `policy` are all
  optional in the spec. `price` in particular is `float | None`, and an offer
  with no price must never sort as the cheapest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Car groups from the RAML's CarTypeGroup enum, kept only as the vocabulary
#: `--type` suggests in help text. Filtering matches whatever the API returns,
#: including groups added after this was written.
KNOWN_CAR_GROUPS: tuple[str, ...] = (
    "small", "medium", "large", "suv", "van", "pickupTruck", "luxury",
    "convertible", "commercial",
)

#: Agency types whose cheap prices come with a catch worth surfacing.
#: `opaque` hides the agency name until booking is confirmed; `p2p` may only
#: reveal the exact pickup location after booking. Reporting either as simply
#: "the cheapest" is misleading, which is why they get their own flags.
CAVEATED_AGENCY_TYPES = {"opaque", "p2p"}

#: Groups that can plausibly be slept in on a road trip. Paired with a
#: passenger floor by the `--sleepable` sugar, since a 2-seat SUV cannot.
SLEEPABLE_GROUPS = frozenset({"suv", "van"})

#: `doors` is an enum of RANGES, not a count, and decoding it by arithmetic is
#: the worst trap in this API. `int("doors23".removeprefix("doors"))` does not
#: raise — it returns twenty-three, so a `--min-doors 4` filter keeps a
#: two-door car and reports it as a match. That is strictly worse than the
#: int/str comparator bug in CLAUDE.md: that one crashed and took the search
#: down, where this one answers confidently and wrongly.
#:
#: So: an explicit table, comparison on the minimum, and `None` for anything
#: not listed. A `doors9` added by KAYAK next year must decode to unknown, not
#: to nine and not to an exception.
DOORS_RANGES: dict[str, tuple[int, int]] = {
    "doors1": (1, 1),
    "doors2": (2, 2),
    "doors23": (2, 3),
    "doors24": (2, 4),
    "doors3": (3, 3),
    "doors4": (4, 4),
    "doors45": (4, 5),
    "doors5": (5, 5),
    "doors6": (6, 6),
}


def _dict(raw: Any, key: str) -> dict:
    """`raw[key]` when it is a dict, else {}. Guards a null in a JSON body."""
    value = raw.get(key) if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def _list(raw: Any, key: str) -> list:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value if isinstance(value, list) else []


def _str(raw: Any, key: str, default: str = "") -> str:
    value = raw.get(key) if isinstance(raw, dict) else None
    return str(value) if isinstance(value, (str, int, float)) else default


def _num(value: Any) -> float | None:
    """A float, or None for null/missing/unparsable. Never 0.0 as a stand-in."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _price(raw: Any, key: str) -> tuple[float | None, str]:
    """A `Price` object as (amount, formatted). Both halves may be absent."""
    price = _dict(raw, key)
    return _num(price.get("price")), _str(price, "displayPrice")


def _results(raw: Any) -> list[dict]:
    """The `results` array every list-shaped endpoint wraps its rows in.

    Tolerates a bare list, since a future endpoint returning one unwrapped
    should degrade to "no rows" at worst rather than raising.
    """
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return [r for r in _list(raw, "results") if isinstance(r, dict)]


@dataclass(frozen=True)
class Location:
    """One entry of the `carLocations` map."""

    id: str = ""
    type: str = ""             # inTerminal | shuttle | nonAirport | ... (raw)
    address: str = ""
    city: str = ""
    country: str = ""
    distance: str = ""         # already localised, e.g. "1.1 mi"
    airport_code: str = ""
    airport_name: str = ""
    terminal: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @classmethod
    def parse(cls, raw: dict) -> "Location":
        airport = _dict(raw, "airport")
        coords = _dict(raw, "coordinates")
        return cls(
            id=_str(raw, "locationId"),
            type=_str(raw, "locationType"),
            address=_str(raw, "address"),
            city=_str(raw, "cityName"),
            country=_str(raw, "countryCode"),
            distance=_str(raw, "displayDistance"),
            airport_code=_str(airport, "code"),
            airport_name=_str(airport, "displayName"),
            terminal=_str(airport, "terminalName"),
            latitude=_num(coords.get("latitude")),
            longitude=_num(coords.get("longitude")),
        )

    @property
    def label(self) -> str:
        """Shortest useful description: airport code, else city, else address."""
        if self.airport_code:
            return f"{self.airport_code} {self.type}".strip()
        return self.city or self.address or self.id


@dataclass(frozen=True)
class Car:
    """`CarDetail`. Every enum-valued field is the raw string from the API."""

    brand: str = ""
    type_code: str = ""
    type_name: str = ""
    groups: tuple[str, ...] = ()
    passengers: int | None = None
    bags: int | None = None
    doors: str = ""            # an enum of RANGES ("doors45"), not an integer
    transmission: str = ""
    fuel: str = ""
    sipp: str = ""
    features: tuple[tuple[str, str], ...] = ()   # (code, displayName)
    image: str = ""

    @classmethod
    def parse(cls, raw: dict) -> "Car":
        type_info = _dict(raw, "type")
        return cls(
            brand=_str(raw, "brand"),
            type_code=_str(type_info, "code"),
            type_name=_str(type_info, "displayName"),
            groups=tuple(str(g) for g in _list(type_info, "groups") if g),
            passengers=_int(raw.get("passengers")),
            bags=_int(raw.get("bags")),
            doors=_str(raw, "doors"),
            transmission=_str(raw, "transmission"),
            fuel=_str(raw, "fuel"),
            sipp=_str(raw, "sipp"),
            features=tuple(
                (_str(f, "code"), _str(f, "displayName"))
                for f in _list(raw, "features")
                if isinstance(f, dict)
            ),
            image=_str(raw, "image"),
        )

    @property
    def name(self) -> str:
        """"Nissan Versa (Compact)" — brand and class, whichever exist."""
        label = self.type_name or self.type_code
        if self.brand and label:
            return f"{self.brand} ({label})"
        return self.brand or label or "car"

    @property
    def doors_range(self) -> tuple[int, int] | None:
        """(min, max) doors, or None for a code we do not recognise.

        Table lookup, never arithmetic — see DOORS_RANGES for why.
        """
        return DOORS_RANGES.get(self.doors)

    @property
    def doors_min(self) -> int | None:
        """The floor a `--min-doors`-style filter must compare against.

        Comparing on the minimum is the conservative reading: `doors23` means
        the agency may hand over a two-door car, so it must not satisfy a
        request for four.
        """
        span = self.doors_range
        return span[0] if span else None

    @property
    def doors_max(self) -> int | None:
        span = self.doors_range
        return span[1] if span else None

    @property
    def doors_display(self) -> str:
        """"doors45" -> "4-5", "doors4" -> "4", unknown codes pass through."""
        span = self.doors_range
        if not span:
            return self.doors
        low, high = span
        return str(low) if low == high else f"{low}-{high}"


@dataclass(frozen=True)
class Policy:
    """`CarBookingOptionPolicy` — where the real comparison between offers is."""

    mileage_code: str = ""     # limited | unlimited (raw)
    mileage_limit: float | None = None
    mileage_name: str = ""
    cancel_unlimited: bool | None = None
    cancel_limit_hours: float | None = None
    cancel_fee: float | None = None
    fuel_code: str = ""
    fuel_name: str = ""
    insurance: tuple[str, ...] = ()   # insurance codes, raw

    @classmethod
    def parse(cls, raw: dict) -> "Policy":
        mileage = _dict(raw, "mileage")
        cancel = _dict(raw, "cancellation")
        fuel = _dict(raw, "fuel")
        fee, _ = _price(cancel, "nonCancellationFee")
        unlimited = cancel.get("isUnlimited")
        return cls(
            mileage_code=_str(mileage, "code"),
            mileage_limit=_num(mileage.get("limit")),
            mileage_name=_str(mileage, "displayName"),
            cancel_unlimited=unlimited if isinstance(unlimited, bool) else None,
            cancel_limit_hours=_num(cancel.get("limitHours")),
            cancel_fee=fee,
            fuel_code=_str(fuel, "code"),
            fuel_name=_str(fuel, "displayName"),
            insurance=tuple(
                _str(i, "code") for i in _list(raw, "insurance")
                if isinstance(i, dict) and _str(i, "code")
            ),
        )

    @property
    def unlimited_mileage(self) -> bool:
        return self.mileage_code == "unlimited"

    @property
    def mileage_display(self) -> str:
        if self.mileage_name:
            return self.mileage_name
        if self.unlimited_mileage:
            return "unlimited"
        if self.mileage_limit is not None:
            return f"{self.mileage_limit:g} mi"
        return "?"

    @property
    def cancel_hours(self) -> float | None:
        """Hours before pickup that free cancellation is still allowed.

        `inf` when the policy is unlimited, None when the API said nothing —
        and None must not be treated as zero, because "unknown" and "not
        cancellable" are different answers.
        """
        if self.cancel_unlimited:
            return math.inf
        return self.cancel_limit_hours

    @property
    def cancel_display(self) -> str:
        hours = self.cancel_hours
        if hours is None:
            return "?"
        if hours == math.inf:
            return "free"
        return f"{hours:g}h"


@dataclass(frozen=True)
class Fee:
    code: str = ""
    amount: float | None = None
    display: str = ""
    included: bool | None = None


@dataclass(frozen=True)
class Money:
    """An amount that cannot be separated from the unit it is denominated in.

    The trap this closes: KAYAK prices in `perDayTotal` by default, so a bare
    float labelled "total" understates a ten-day rental by a factor of ten.
    Carrying `mode` *on* the amount rather than beside it means a formatter
    physically cannot print the number without going through `label()`, and
    `label()` cannot omit the "/day".

    `mode` is always the mode the response came back in, never the one we
    asked for — if the API ignores our request the label follows reality.
    """

    amount: float | None = None
    currency: str = ""
    mode: str = ""              # total | perDayTotal (raw, from the response)
    days: int | None = None
    display: str = ""           # the API's own localised string, if it sent one

    @property
    def per_day(self) -> bool:
        return self.mode == "perDayTotal"

    def label(self) -> str:
        """The amount with its unit. The only way to render this number."""
        if self.amount is None:
            return "no price"
        shown = self.display or f"{self.amount:,.0f} {self.currency}".strip()
        return f"{shown}/day" if self.per_day else str(shown)

    def total(self) -> float | None:
        """Trip total whatever mode the API priced in; None if unknowable."""
        if self.amount is None:
            return None
        if self.per_day:
            return None if not self.days else self.amount * self.days
        return self.amount

    def sort_key(self) -> tuple[int, float]:
        """Ascending, with unpriced offers pinned last.

        The leading flag is what keeps a missing price out of "cheapest": it
        sorts after every real number regardless of the second element. A bare
        `key=lambda o: o.price.amount` raises TypeError comparing None to
        float the first time a provider omits a price — the same shape as the
        int/str comparator bug recorded in CLAUDE.md.
        """
        return (1, 0.0) if self.amount is None else (0, self.amount)

    def as_dict(self) -> dict:
        """Serialised form. The mode never travels separately from the number."""
        return {
            "amount": self.amount,
            "currency": self.currency,
            "mode": self.mode,
            "days": self.days,
            "display": self.label(),
            "total": self.total(),
        }


@dataclass(frozen=True)
class Maps:
    """The lookup maps hanging off a car search response root.

    Also carries the response-level facts an offer needs to describe its own
    price honestly: `price_mode`, `currency`, and the rental length in days.
    """

    agencies: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    locations: dict = field(default_factory=dict)
    price_mode: str = ""
    currency: str = ""
    days: int | None = None

    @classmethod
    def parse(cls, raw: dict) -> "Maps":
        return cls(
            agencies=_dict(raw, "agencies"),
            providers=_dict(raw, "providers"),
            locations=_dict(raw, "carLocations"),
            price_mode=_str(raw, "priceMode"),
            currency=_str(raw, "currency"),
            days=_int(raw.get("days")),
        )


@dataclass(frozen=True)
class Offer:
    """One bookable option: a car, from an agency, through a provider, at a price.

    Flat by design. Nothing downstream should have to look anything up.
    """

    result_id: str
    provider_code: str = ""
    provider_name: str = ""
    agency_code: str = ""
    agency_name: str = ""
    agency_type: str = ""      # regular | p2p | opaque | delivery | ... (raw)
    booking_url: str = ""
    price: Money = field(default_factory=Money)
    payment_type: str = ""
    rate_type: str = ""
    credit_card_required: bool | None = None
    badges: tuple[tuple[str, str], ...] = ()
    fees: tuple[Fee, ...] = ()
    car: Car = field(default_factory=Car)
    policy: Policy = field(default_factory=Policy)
    pickup: Location | None = None
    dropoff: Location | None = None

    @classmethod
    def parse(cls, raw: dict, result_id: str, maps: Maps) -> "Offer":
        """Flatten one `CarSearchResultBookingOption` against the response maps.

        An agency or provider code with no entry in its map is not an error —
        it yields an empty display name and the code still shows in output.
        """
        agency_code = _str(raw, "agencyCode")
        provider_code = _str(raw, "providerCode")
        agency = _dict(maps.agencies, agency_code)
        provider = _dict(maps.providers, provider_code)
        pickup_id = _str(raw, "pickupLocationId")
        dropoff_id = _str(raw, "dropoffLocationId")
        amount, display = _price(raw, "price")
        credit_card = raw.get("isCreditCardRequired")
        return cls(
            result_id=result_id,
            provider_code=provider_code,
            provider_name=_str(provider, "displayName"),
            agency_code=agency_code,
            agency_name=_str(agency, "displayName"),
            agency_type=_str(agency, "type"),
            booking_url=_str(raw, "bookingUrl"),
            price=Money(
                amount=amount,
                currency=maps.currency,
                mode=maps.price_mode,
                days=maps.days,
                display=display,
            ),
            payment_type=_str(raw, "paymentType"),
            rate_type=_str(raw, "rateType"),
            credit_card_required=(
                credit_card if isinstance(credit_card, bool) else None
            ),
            badges=tuple(
                (_str(b, "code"), _str(b, "displayName"))
                for b in _list(raw, "badges")
                if isinstance(b, dict)
            ),
            fees=tuple(
                Fee(
                    code=_str(f, "code"),
                    amount=_price(f, "rate")[0],
                    display=_price(f, "rate")[1],
                    included=(
                        f.get("isIncludedInTotal")
                        if isinstance(f.get("isIncludedInTotal"), bool)
                        else None
                    ),
                )
                for f in _list(raw, "fees")
                if isinstance(f, dict)
            ),
            car=Car.parse(_dict(raw, "car")),
            policy=Policy.parse(_dict(raw, "policy")),
            pickup=(
                Location.parse(_dict(maps.locations, pickup_id))
                if pickup_id and pickup_id in maps.locations else None
            ),
            dropoff=(
                Location.parse(_dict(maps.locations, dropoff_id))
                if dropoff_id and dropoff_id in maps.locations else None
            ),
        )

    # ---------------- derived facts ----------------

    @property
    def agency_label(self) -> str:
        """The agency as a human should see it, caveat included.

        An opaque agency has no name to show until booking, so saying so is
        more honest than printing the code.
        """
        name = self.agency_name or self.agency_code or "?"
        if self.agency_type in CAVEATED_AGENCY_TYPES:
            return f"{name} [{self.agency_type}]"
        return name

    @property
    def opaque(self) -> bool:
        return self.agency_type == "opaque"

    @property
    def peer_to_peer(self) -> bool:
        return self.agency_type == "p2p"

    @property
    def badge_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.badges if code)

    @property
    def free_cancellation(self) -> bool:
        """Cancellable at no charge at any point before pickup.

        A `limitHours` policy is *not* free cancellation — it is cancellation
        up to a deadline, which `--cancel-window` filters on instead.
        """
        return bool(self.policy.cancel_unlimited) or "freeCancellation" in self.badge_codes

    @property
    def sleepable(self) -> bool:
        """An SUV or van big enough to sleep in — the car-camping question."""
        return bool(SLEEPABLE_GROUPS.intersection(self.car.groups)) and (
            self.car.passengers is not None and self.car.passengers >= 4
        )

    @property
    def price_label(self) -> str:
        """The price with the unit it is actually in. Delegates to Money."""
        return self.price.label()

    @property
    def total_price(self) -> float | None:
        """Trip total, whatever mode the API priced in. None if unknowable."""
        return self.price.total()

    def sort_key(self) -> tuple[int, float]:
        """Ascending price, with unpriced offers pinned last."""
        return self.price.sort_key()

    def summary(self) -> dict:
        """The trimmed projection emitted by `--json`.

        Deliberately not the raw booking option: a 500-result response is
        megabytes of logo URLs and tracking tokens, and every byte of it lands
        in an agent's context window. `--full` opts into the raw response.
        """
        return {
            "result_id": self.result_id,
            "car": self.car.name,
            "car_type": self.car.type_code,
            "groups": list(self.car.groups),
            "passengers": self.car.passengers,
            "bags": self.car.bags,
            "doors": self.car.doors_display,
            "doors_code": self.car.doors,
            "doors_min": self.car.doors_min,
            "doors_max": self.car.doors_max,
            "transmission": self.car.transmission,
            "fuel": self.car.fuel,
            "sipp": self.car.sipp,
            "features": [code for code, _ in self.car.features],
            "agency": self.agency_name or self.agency_code,
            "agency_type": self.agency_type,
            "provider": self.provider_name or self.provider_code,
            "price": self.price.as_dict(),
            "payment_type": self.payment_type,
            "credit_card_required": self.credit_card_required,
            "unlimited_mileage": self.policy.unlimited_mileage,
            "mileage": self.policy.mileage_display,
            "free_cancellation": self.free_cancellation,
            "cancel_hours_before_pickup": (
                None if self.policy.cancel_hours in (None, math.inf)
                else self.policy.cancel_hours
            ),
            "fuel_policy": self.policy.fuel_code,
            "insurance": list(self.policy.insurance),
            "fees": [
                {"code": f.code, "amount": f.amount, "included": f.included}
                for f in self.fees
            ],
            "badges": [code for code, _ in self.badges],
            "sleepable": self.sleepable,
            "pickup": self.pickup.label if self.pickup else None,
            "pickup_address": self.pickup.address if self.pickup else None,
            "dropoff": self.dropoff.label if self.dropoff else None,
            "booking_url": self.booking_url,
        }


@dataclass(frozen=True)
class CarSearch:
    """A car search response, flattened. `offers` is the join already done."""

    search_id: str
    cluster: str
    status: str                # first-phase | second-phase | complete (raw)
    offers: tuple[Offer, ...]
    total_count: int | None
    price_mode: str
    currency: str
    days: int | None
    polls: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, raw: dict, polls: int = 0) -> "CarSearch":
        maps = Maps.parse(raw)
        offers: list[Offer] = []
        for result in _list(raw, "results"):
            if not isinstance(result, dict):
                continue
            result_id = _str(result, "id")
            for option in _list(result, "bookingOptions"):
                if isinstance(option, dict):
                    offers.append(Offer.parse(option, result_id, maps))
        return cls(
            search_id=_str(raw, "searchId"),
            cluster=_str(raw, "cluster"),
            status=_str(raw, "status"),
            offers=tuple(offers),
            total_count=_int(raw.get("totalCount")),
            price_mode=maps.price_mode,
            currency=maps.currency,
            days=maps.days,
            polls=polls,
            raw=raw,
        )

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    def cheapest(self) -> "Offer | None":
        """The lowest-priced offer, or None when nothing here has a price."""
        priced = [o for o in self.offers if o.price.amount is not None]
        return min(priced, key=Offer.sort_key) if priced else None

    def cheapest_key(self) -> tuple[int, float]:
        """Sort key ranking whole searches — used to rank days in a sweep.

        A search with no priced offer sorts last, for the same reason a
        priceless offer does: it cannot be the cheapest anything.
        """
        best = self.cheapest()
        return best.sort_key() if best else (1, 0.0)


@dataclass(frozen=True)
class Place:
    """One autocomplete record, across all three verticals.

    The verticals return different id fields — cars wants a `cityId` or an IATA
    code, price insights wants a `placeId`, hotels wants an `entityKey`. All
    three are carried so `places` can tell the agent which to use where.
    """

    place_id: int | None
    name: str
    full_name: str
    place_type: str
    iata: str = ""
    city_id: int | None = None
    hotel_id: int | None = None
    entity_key: str = ""
    country: str = ""
    region: str = ""
    city: str = ""

    @classmethod
    def parse_many(cls, raw: Any) -> list["Place"]:
        """Every row of a `results` envelope, bad rows skipped not fatal."""
        return [cls.parse(row) for row in _results(raw)]

    @classmethod
    def parse(cls, raw: dict) -> "Place":
        display = _dict(raw, "displayPlaceType")
        return cls(
            place_id=_int(raw.get("placeId")),
            name=_str(raw, "name") or _str(raw, "hotelName") or _str(raw, "cityName"),
            full_name=_str(raw, "fullName"),
            place_type=_str(raw, "primaryPlaceType") or _str(display, "type"),
            iata=_str(raw, "iataCode"),
            city_id=_int(raw.get("cityId")),
            hotel_id=_int(raw.get("hotelId")),
            entity_key=_str(raw, "entityKey"),
            country=_str(raw, "countryName"),
            region=_str(raw, "regionName"),
            city=_str(raw, "cityName"),
        )

    @property
    def label(self) -> str:
        return self.full_name or self.name or str(self.place_id or "")

    def summary(self) -> dict:
        return {
            "place_id": self.place_id,
            "name": self.name,
            "full_name": self.full_name,
            "type": self.place_type,
            "iata": self.iata or None,
            "city_id": self.city_id,
            "hotel_id": self.hotel_id,
            "entity_key": self.entity_key or None,
            "country": self.country or None,
            "region": self.region or None,
        }


@dataclass(frozen=True)
class Hotel:
    """One `HotelResult`, trimmed to what a person choosing a hotel needs."""

    id: int | None
    key: str
    name: str
    address: str
    star_rating: float | None
    guest_rating: float | None
    reviews: int | None
    lowest_rate: float | None
    highest_rate: float | None
    distance: float | None
    providers: int | None
    url: str

    @classmethod
    def parse_many(cls, raw: Any) -> list["Hotel"]:
        """Every row of a `results` envelope, bad rows skipped not fatal."""
        return [cls.parse(row) for row in _results(raw)]

    @classmethod
    def parse(cls, raw: dict) -> "Hotel":
        return cls(
            id=_int(raw.get("id")),
            key=_str(raw, "key"),
            name=_str(raw, "name"),
            address=_str(raw, "address"),
            star_rating=_num(raw.get("starRating")),
            guest_rating=_num(raw.get("guestRating")),
            reviews=_int(raw.get("numberOfReviews")),
            lowest_rate=_num(raw.get("lowestRate")),
            highest_rate=_num(raw.get("highestRate")),
            distance=_num(raw.get("distance")),
            providers=_int(raw.get("numberOfProviders")),
            url=_str(raw, "href"),
        )

    def sort_key(self) -> tuple[int, float]:
        """Cheapest first, unpriced last — same rule as Offer.sort_key."""
        return (1, 0.0) if self.lowest_rate is None else (0, self.lowest_rate)

    def summary(self) -> dict:
        return {
            "hotel_id": self.id,
            "key": self.key,
            "name": self.name,
            "address": self.address,
            "star_rating": self.star_rating,
            "guest_rating": self.guest_rating,
            "reviews": self.reviews,
            "lowest_rate": self.lowest_rate,
            "highest_rate": self.highest_rate,
            "distance": self.distance,
            "providers": self.providers,
            "url": self.url,
        }


@dataclass(frozen=True)
class CalendarDay:
    """One day (or month) of the flights price-insights calendar.

    `predicted` is load-bearing for honesty: a predicted price is a model's
    guess, not a fare anyone has been quoted, and it must be labelled as such
    wherever it is shown.
    """

    key: str                  # "2026-12-20" for day aggregation, "2026-12" for month
    price: float | None
    currency: str
    predicted: bool
    origin: str
    destination: str
    depart: str
    return_date: str = ""
    non_stop: bool | None = None
    url: str = ""

    @classmethod
    def parse_many(cls, raw: Any) -> list["CalendarDay"]:
        """Every row of a `results` envelope, bad rows skipped not fatal."""
        return [cls.parse(row) for row in _results(raw)]

    @classmethod
    def parse(cls, raw: dict) -> "CalendarDay":
        outbound = _dict(raw, "outboundLeg")
        inbound_legs = [l for l in _list(raw, "inboundLegs") if isinstance(l, dict)]
        # For a round trip the price and the deeplink hang off the inbound leg,
        # because they cover both legs together; one-way carries them outbound.
        priced = outbound
        cheapest: dict = {}
        for leg in inbound_legs:
            amount = _num(_dict(leg, "price").get("price"))
            if amount is not None and (
                not cheapest or amount < _num(_dict(cheapest, "price").get("price"))
            ):
                cheapest = leg
        if cheapest:
            priced = cheapest
        price_obj = _dict(priced, "price")
        non_stop = priced.get("noStops")
        return cls(
            key=_str(raw, "aggregationKey"),
            price=_num(price_obj.get("price")),
            currency=_str(price_obj, "currency"),
            predicted=bool(raw.get("predicted")),
            origin=_str(outbound, "origin"),
            destination=_str(outbound, "destination"),
            depart=_str(outbound, "departureDate"),
            return_date=_str(cheapest, "departureDate") if cheapest else "",
            non_stop=non_stop if isinstance(non_stop, bool) else None,
            url=_str(priced, "deeplinkUrl"),
        )

    def sort_key(self) -> tuple[int, float]:
        return (1, 0.0) if self.price is None else (0, self.price)

    def summary(self) -> dict:
        return {
            "date": self.key,
            "price": self.price,
            "currency": self.currency,
            "predicted": self.predicted,
            "origin": self.origin,
            "destination": self.destination,
            "depart": self.depart,
            "return": self.return_date or None,
            "non_stop": self.non_stop,
            "url": self.url,
        }


@dataclass(frozen=True)
class HotelSearch:
    """A hotel search response, with the completion flag kept alongside.

    The whole point of this class is `complete`. `/hotels` answers with
    whatever providers have reported *so far* unless `onlyIfComplete=true`,
    and a partial hotel list presented as final is exactly the confident wrong
    answer this skill exists to avoid — the cheapest hotel in a half-finished
    search is not the cheapest hotel. Callers that must not mislead check this
    flag before using the word "cheapest".

    The spec disagrees with itself about the field's name, so both spellings
    are read, `isComplete` first:

    * `isComplete` is what the RAML **type definitions** declare —
      `MultipleHotelSearchResponse` (hotels RAML line 1401),
      `SingleHotelSearchResponse` (line 1242) and
      `BasicMultipleHotelSearchResponse` (line 1589) — and what **every
      example body** in the spec uses (lines 1340, 1521, 1620).
    * `isCompleted` appears exactly once, in the prose of the "Query
      completion" overview at line 24.

    `isComplete` is therefore the one to trust and the fallback is the
    hedge — do not delete either. A missing flag means False: never silently
    "yes, this is the whole picture".
    """

    hotels: tuple[Hotel, ...]
    complete: bool
    total_count: int | None
    currency: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, raw: Any, single: bool = False) -> "HotelSearch":
        """Parse `/hotels` (a `results` envelope) or `/hotel` (one hotel).

        `single=True` is the `/hotel` shape: the body *is* the hotel record,
        with the search-level fields hanging off the same object rather than
        wrapping a list.
        """
        body = raw if isinstance(raw, dict) else {}
        if single:
            hotels = [Hotel.parse(body)] if body else []
        else:
            hotels = Hotel.parse_many(raw)
        flag = body.get("isComplete")
        if not isinstance(flag, bool):
            flag = body.get("isCompleted")
        total = _int(body.get("totalResults"))
        if total is None:
            total = _int(body.get("totalAvailableResults"))
        return cls(
            hotels=tuple(hotels),
            complete=flag if isinstance(flag, bool) else False,
            total_count=total,
            currency=_str(body, "currencyCode"),
            raw=body,
        )

    def cheapest(self) -> "Hotel | None":
        """The lowest-rate hotel, or None when nothing here is priced."""
        priced = [h for h in self.hotels if h.lowest_rate is not None]
        return min(priced, key=Hotel.sort_key) if priced else None


@dataclass(frozen=True)
class CalendarSearch:
    """A `/priceInsights/flights/v1/calendar` response.

    The rows alone are not enough to report honestly: the response resolves
    the request's `PlaceRequest` back into a named `PlaceResponse`, and an
    IATA code the API mapped to a metro area is a different question than the
    one the user thought they asked. Carrying the resolved names lets the
    caller say which airports were actually priced.
    """

    days: tuple[CalendarDay, ...]
    origin_name: str
    destination_name: str
    currency: str

    @classmethod
    def parse(cls, raw: Any) -> "CalendarSearch":
        body = raw if isinstance(raw, dict) else {}
        days = tuple(CalendarDay.parse_many(raw))
        # The response has no search-level currency field, so it is taken from
        # the first priced row — every row is converted to the same currency.
        currency = next((d.currency for d in days if d.currency), "")
        return cls(
            days=days,
            origin_name=_str(_dict(body, "origin"), "name"),
            destination_name=_str(_dict(body, "destination"), "name"),
            currency=currency,
        )

    def cheapest(self) -> "CalendarDay | None":
        priced = [d for d in self.days if d.price is not None]
        return min(priced, key=CalendarDay.sort_key) if priced else None
