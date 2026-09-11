"""Domain model, and the only place raw API JSON is touched.

Hard rule for this package: no other module subscripts an API dict. Everything
outside `model.py` works with the dataclasses below. That is what keeps the
API's sharp edges - a `DRIVE` facet that is sometimes absent, a literal
`"null"` string in the filter catalogue, a `PAYLATER` key that is easy to
mistype as `PAY_LATER` - contained in one reviewed file.

The most important type here is `Price`. See its docstring.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable

from .errors import AgeRefused, RouteRefused, UsageError

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


@dataclass(frozen=True, order=False)
class Money:
    amount: Decimal
    currency: str

    def __str__(self) -> str:
        return f"{self.amount:,.2f} {self.currency}"


@dataclass(frozen=True)
class Price:
    """What the card is charged, plus an optional display-only conversion.

    `view_currency_code` on the request performs a *display* conversion; it
    does not change what is billed. Quoting Halifax in USD returns
    `total_price_view` 315.10 USD alongside `total_price_payment` 436.00 CAD.
    Reporting the first as "the price" is the worst bug this tool could ship.

    So the split is structural, not a convention:

    * `charged` is the real price. Every sort, comparison and total reads it.
    * `converted` is reachable only through `format_price`, whose output always
      carries the word "est." next to the number.

    `Price` deliberately has no `.amount`, no `__float__` and no `__str__` that
    can emit `converted` on its own, so misreporting requires writing
    `.converted` by hand - which is greppable in review and asserted against in
    the self-check.
    """

    charged: Money
    converted: Money | None = None

    @property
    def sort_key(self) -> Decimal:
        return self.charged.amount


def format_price(price: Price | None) -> str:
    """The one renderer allowed to show the converted figure."""
    if price is None:
        return "-"
    if price.converted is None:
        return str(price.charged)
    return f"{price.charged} (~{price.converted} est.)"


def _decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _money(node: Any) -> Money | None:
    """Build Money from an API price node, or None if it is not a real price."""
    if not isinstance(node, dict):
        return None
    amount = _decimal(node.get("amount"))
    currency = node.get("code")
    if amount is None or not currency:
        return None
    return Money(amount, str(currency))


def _price(charge: Any, payment_key: str, view_key: str) -> Price | None:
    """Pair a *_payment node with its *_view twin.

    `charged` always comes from `_payment`. When the two currencies match the
    conversion is redundant and dropped, so `converted` is non-None only when
    it actually says something.
    """
    if not isinstance(charge, dict):
        return None
    charged = _money(charge.get(payment_key))
    if charged is None:
        return None
    converted = _money(charge.get(view_key))
    if converted is not None and converted.currency == charged.currency:
        converted = None
    return Price(charged, converted)


# --------------------------------------------------------------------------
# Vehicles
# --------------------------------------------------------------------------


class VehicleStatus(str, Enum):
    AVAILABLE = "AVAILABLE_AT_RETAIL_RATE"
    SOLD_OUT = "SOLD_OUT"
    RESTRICTED = "RESTRICTED_AT_RETAIL_RATE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, raw: Any) -> "VehicleStatus":
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNKNOWN


#: The facet catalogue contains a literal {"code": "null", "description":
#: "null"} entry - the string, not JSON null. Anything matching this is not a
#: real value and must never reach a filter or a table.
_NULLISH = frozenset({"", "null", "none", "n/a"})


def _clean(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text.lower() in _NULLISH else text


#: Facet code -> description, from the API's own `car_classes_filters`
#: catalogue. Several classes carry a `filter_code` with NO
#: `filter_description`, and a bare "112" is both meaningless in a table and
#: silently wrong in a predicate: `"4 wheel" in "59"` is False, so an AWD class
#: would fail an AWD filter. Resolving the code first closes that hole.
_FACET_CODES: dict[str, dict[str, str]] = {
    "DRIVE": {"59": "4 Wheel Drive or All Wheel Drive", "112": "2 Wheel Drive"},
    "FUEL": {
        "102": "Diesel Vehicle", "168": "Hybrid Vehicle",
        "169": "Gasoline Vehicle", "170": "Electric Vehicle",
    },
    "TRANSMISSION": {"25": "Automatic", "26": "Manual"},
}


def _facet_code(filters: Any, name: str) -> str | None:
    """The facet's raw code, which is the same in every locale.

    Descriptions are translated - `2 Wheel Drive` becomes
    `Zweiradantrieb`, `Cars` becomes `Mietwagen` - so any filter matching on
    the description silently stops working outside English. Codes do not move,
    so every predicate matches on these and only the display uses the text.
    """
    if not isinstance(filters, dict):
        return None
    node = filters.get(name)
    if not isinstance(node, dict):
        return None
    return _clean(node.get("filter_code"))


def _facet(filters: Any, name: str) -> str | None:
    """Read one entry from a car class's pre-computed `filters` block.

    Returns None when the API omits the facet entirely, which it does for
    `DRIVE` on several premium classes. Callers must treat None as unknown
    rather than as "2WD".
    """
    if not isinstance(filters, dict):
        return None
    node = filters.get(name)
    if not isinstance(node, dict):
        return None
    described = _clean(node.get("filter_description"))
    if described:
        return described
    code = _clean(node.get("filter_code"))
    if code is None:
        return None
    # Fall back to the catalogue; an unmapped code is reported as unknown
    # rather than shown raw, since a bare number tells a reader nothing.
    return _FACET_CODES.get(name, {}).get(code)


#: Locale-independent facet codes, from the API's own catalogue.
#:
#: Canada only ever returns 100/200/300/500, which made "vans = 500" look like
#: the whole story. Frankfurt returns FOUR categories: `Kleinbusse`
#: (people-carriers) is **400**, and 500 is `Transporter` - cargo vans. So a
#: market can use a code the reference market never shows, and a person asking
#: for "a van" means either depending on where they are standing.
CLASS_CARS, CLASS_TRUCKS, CLASS_SUVS = "100", "200", "300"
CLASS_MINIBUS, CLASS_VANS = "400", "500"
DRIVE_AWD, DRIVE_2WD = "59", "112"
FUEL_DIESEL, FUEL_HYBRID, FUEL_PETROL, FUEL_ELECTRIC = "102", "168", "169", "170"


@dataclass(frozen=True)
class Vehicle:
    code: str
    model: str
    category: str | None
    category_code: str | None
    sub_category: str | None
    seats: int
    bags: int
    small_bags: int
    large_bags: int
    drive: str | None
    drive_code: str | None
    fuel: str | None
    fuel_code: str | None
    transmission: str | None
    transmission_code: str | None
    unlimited_mileage: bool
    guaranteed: bool
    status: VehicleStatus
    total: Price | None
    daily: Price | None
    #: Points are a PER-DAY figure. `charges.REDEMPTION.total_price_payment`
    #: equals `rates[0].unit_amount_payment` in every response checked (130 of
    #: 130) - a daily rate wearing a total's name - and the response never
    #: states how many days a redemption covers. So a trip-level points cost is
    #: NOT derivable, and dividing it into a cash total is meaningless.
    points_per_day: int | None
    fuel_consumption: str | None
    #: What the API's own rate line actually means - DAILY, WEEKLY, MONTHLY.
    #: Carried because the raw `unit_amount` is meaningless without it: on a
    #: six-day Toronto rental the API returns a WEEKLY figure, and labelling
    #: that "per day" quotes a Kia Rio at $372 a day.
    rate_period: str | None = None
    rate_quantity: float | None = None
    #: How many lines the API used to price this. A 30-night rental comes back
    #: as WEEKLY x4 + EXTRA_DAILY x2, so `rates[0]` is neither the daily rate
    #: nor the whole price. Never reconstruct a total from the rate lines: in
    #: Canada tax sits outside them and the sum is short.
    rate_lines: int = 0

    @property
    def bookable(self) -> bool:
        """Priced and actually rentable.

        `SOLD_OUT` classes stay in the response with no charges, so counting
        `len(vehicles)` reports 59 available when 20 are bookable.
        """
        return self.status is VehicleStatus.AVAILABLE and self.total is not None

    @property
    def is_awd(self) -> bool:
        """Four- or all-wheel drive, by code so it holds in every locale."""
        return self.drive_code == DRIVE_AWD

    @property
    def is_big_enough_to_sleep_in(self) -> bool:
        """SUV, people-carrier or van - anything you can fold flat and lie in."""
        return self.category_code in (CLASS_SUVS, CLASS_MINIBUS, CLASS_VANS)

    def per_day(self, days: int) -> Money | None:
        """Trip total divided by rental days - the only honest daily figure.

        Deliberately derived rather than read from the API: `rates[0]` may be a
        daily base excluding tax, or a weekly rate, depending on branch and
        duration. This is always comparable and always what the renter pays per
        day of the rental.
        """
        if self.total is None or days < 1:
            return None
        return Money(
            (self.total.charged.amount / Decimal(days)).quantize(Decimal("0.01")),
            self.total.charged.currency,
        )

    @property
    def rate_note(self) -> str | None:
        """How the API expressed its own rate, when it is not a plain daily one.

        The switch from DAILY to WEEKLY happens between 4 and 5 nights and
        flips for every class in the response at once.
        """
        period = (self.rate_period or "").lower()
        if self.rate_lines > 1:
            # Say the period AND the structure: a 30-night rental is
            # WEEKLY x4 + EXTRA_DAILY x2, which is the case a reader most needs
            # explaining, not the one to be vaguest about.
            detail = f"{period} plus extras" if period else "several rates"
            return f"{detail}, across {self.rate_lines} rate lines"
        if not period or period == "daily":
            return None
        return period

    @classmethod
    def parse(cls, raw: dict) -> "Vehicle":
        charges = raw.get("charges") or {}
        # The one and only reader of this key, which kills the PAY_LATER typo.
        paylater = charges.get("PAYLATER")
        redemption = charges.get("REDEMPTION") or {}
        points = _decimal(
            (redemption.get("total_price_payment") or {}).get("amount")
        )
        mileage = raw.get("mileage_info") or {}
        filters = raw.get("filters") or {}
        rates = (paylater or {}).get("rates") or []
        # rates[0] is read three times below; guard it once. A non-dict entry
        # would otherwise crash on .get, and the API has surprised us before.
        first_rate = rates[0] if rates and isinstance(rates[0], dict) else {}
        return cls(
            code=str(raw.get("code") or "?"),
            model=_clean(raw.get("make_model_or_similar_text")) or "",
            category=_clean((raw.get("category") or {}).get("name")),
            category_code=_clean((raw.get("category") or {}).get("code")),
            sub_category=_clean((raw.get("sub_category") or {}).get("name")),
            seats=int(raw.get("people_capacity") or 0),
            bags=int(raw.get("luggage_capacity") or 0),
            small_bags=int(raw.get("small_luggage_capacity") or 0),
            large_bags=int(raw.get("large_luggage_capacity") or 0),
            drive=_facet(filters, "DRIVE"),
            drive_code=_facet_code(filters, "DRIVE"),
            fuel=_facet(filters, "FUEL"),
            fuel_code=_facet_code(filters, "FUEL"),
            transmission=_facet(filters, "TRANSMISSION"),
            transmission_code=_facet_code(filters, "TRANSMISSION"),
            unlimited_mileage=bool(mileage.get("unlimited_mileage")),
            guaranteed=bool(raw.get("guaranteed_vehicle")),
            status=VehicleStatus.parse(raw.get("status")),
            total=_price(paylater, "total_price_payment", "total_price_view"),
            daily=_price(first_rate, "unit_amount_payment", "unit_amount_view"),
            points_per_day=int(points) if points is not None else None,
            fuel_consumption=_clean(raw.get("fuel_consumption")),
            rate_period=_clean(first_rate.get("unit_rate_type")),
            rate_quantity=first_rate.get("unit_rate_type_quantity"),
            rate_lines=len([r for r in rates if isinstance(r, dict)]),
        )


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


#: Buckets that name a real branch you can actually collect a car from.
#: `city` is a search result, not a counter.
BOOKABLE_KINDS = frozenset({"airport", "branch", "rail", "port", "exotic"})


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    kind: str                 # bucket it came from: airport/branch/city/exotic
    location_type: str        # the API's own location_type, for the POST body
    airport_code: str | None
    city: str | None
    country: str | None
    currency: str | None
    latitude: float | None
    longitude: float | None
    phone: str | None = None

    @property
    def label(self) -> str:
        code = f" ({self.airport_code})" if self.airport_code else ""
        return f"{self.name}{code}"

    @property
    def is_exotic(self) -> bool:
        """Exotic branches carry a different fleet at very different prices."""
        return self.kind == "exotic" or "exotic" in self.name.lower()

    @property
    def bookable(self) -> bool:
        return self.kind in BOOKABLE_KINDS

    def as_request(self) -> dict:
        """The location object the quote endpoint expects inline."""
        body: dict[str, Any] = {
            "id": self.id,
            "type": "BRANCH",
            "location_type": self.location_type or "BRANCH",
            "name": self.name,
            "country_code": self.country,
            "my_location": False,
        }
        if self.airport_code:
            body["airport_code"] = self.airport_code
        if self.latitude is not None and self.longitude is not None:
            body["gps"] = {"latitude": self.latitude, "longitude": self.longitude}
        return body

    @classmethod
    def parse(cls, raw: dict, kind: str) -> "Location":
        address = raw.get("address") or {}
        gps = raw.get("gps") or {}
        phones = raw.get("phones") or []
        return cls(
            id=str(raw.get("id") or ""),
            name=_clean(raw.get("name")) or "(unnamed)",
            kind=kind,
            location_type=str(raw.get("location_type") or "BRANCH"),
            airport_code=_clean(raw.get("airport_code")),
            city=_clean(address.get("city")),
            country=_clean(address.get("country_code")),
            currency=_clean(raw.get("currency_code")),
            latitude=gps.get("latitude"),
            longitude=gps.get("longitude"),
            phone=_clean((phones[0] if phones else {}).get("phone_number")),
        )


# --------------------------------------------------------------------------
# Requests and quotes
# --------------------------------------------------------------------------


_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def parse_amount(text: str) -> Decimal:
    """Parse a money argument, raising something argparse understands.

    `Decimal(...)` raises `InvalidOperation`, which inherits from
    `ArithmeticError` and NOT `ValueError`, so argparse does not treat it as a
    bad argument: it escapes as a traceback and exits 1 - the one code that
    means "keep waiting". A watch with a typo'd threshold would poll forever on
    a query that never ran.
    """
    cleaned = text.strip().replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(
            f"{text!r} is not an amount - use a plain number like 400 or 400.50"
        ) from None
    if not value.is_finite():
        # Decimal("nan") is a perfectly valid Decimal, so this survives parsing
        # and then raises InvalidOperation at match time - surfacing as a
        # network error rather than a bad argument.
        raise ValueError(f"{text!r} is not a usable amount")
    if value < 0:
        raise ValueError("amount must not be negative")
    return value


def check_dates(pickup: str, ret: str, *, now: datetime | None = None) -> None:
    """Reject impossible date pairs locally.

    The API catches all of these, but each rejection costs a ~700 KB pricing
    call against a host that rate-limits without warning. It also reports only
    the first fault it finds, so a reversed range in the past sends the user to
    fix the wrong field.
    """
    start = datetime.strptime(pickup, "%Y-%m-%dT%H:%M")
    end = datetime.strptime(ret, "%Y-%m-%dT%H:%M")
    now = now or datetime.now()

    faults: list[str] = []
    if end <= start:
        faults.append(f"return {ret} is not after pickup {pickup}")
    if start < now:
        faults.append(f"pickup {pickup} is in the past")
    if (end - now).days > 395:
        faults.append(
            f"return {ret} is more than 395 days ahead, beyond Enterprise's "
            f"booking horizon"
        )
    if faults:
        # Report every fault at once; the API names only the first.
        raise UsageError("; ".join(faults))


def parse_when(text: str) -> str:
    """Validate a pickup/return timestamp.

    The API wants exactly `YYYY-MM-DDTHH:MM` - no seconds, no timezone. A bare
    date is accepted and defaulted to 10:00 because that is what people type.
    """
    text = text.strip()
    if _TIME_RE.match(text):
        return text
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%dT10:00")
    except ValueError:
        pass
    raise UsageError(
        f"bad date/time {text!r} - use YYYY-MM-DD or YYYY-MM-DDTHH:MM"
    )


@dataclass(frozen=True)
class QuoteRequest:
    pickup: Location
    dropoff: Location
    pickup_time: str
    return_time: str
    age: int = 25
    residency: str = "CA"
    currency: str | None = None

    @property
    def is_one_way(self) -> bool:
        return self.pickup.id != self.dropoff.id

    @property
    def rental_days(self) -> int:
        """Billable days, rounded up - a 25-hour rental is two days."""
        try:
            start = datetime.strptime(self.pickup_time, "%Y-%m-%dT%H:%M")
            end = datetime.strptime(self.return_time, "%Y-%m-%dT%H:%M")
        except ValueError:
            return 1
        hours = (end - start).total_seconds() / 3600
        # ceil on the float: int() first discarded the fraction, so a 24h30m
        # rental counted as one day and its whole two-day total was reported as
        # a single day's price. Enterprise's grace period is far under 30 min.
        return max(1, math.ceil(hours / 24))

    @property
    def countries_known(self) -> bool:
        """Whether both branch countries were actually resolved.

        A numeric id whose detail lookup failed has `country=None`. Guessing
        one would silently disable cross-border detection, so unknown is
        carried honestly and handled as a separate case.
        """
        return bool(self.pickup.country and self.dropoff.country)

    @property
    def is_cross_border(self) -> bool:
        return self.countries_known and self.pickup.country != self.dropoff.country

    @property
    def maybe_cross_border(self) -> bool:
        """A one-way that is cross-border, or might be."""
        return self.is_one_way and (self.is_cross_border or not self.countries_known)

    def as_body(self) -> dict:
        currency = self.currency or self.pickup.currency or "CAD"
        return {
            "pickup_time": self.pickup_time,
            "return_time": self.return_time,
            "pickup_location": self.pickup.as_request(),
            "pickup_location_id": self.pickup.id,
            "return_location": self.dropoff.as_request(),
            "return_location_id": self.dropoff.id,
            "renter_age": self.age,
            "country_of_residence_code": self.residency,
            "view_currency_code": currency,
            "enable_north_american_prepay_rates": False,
            # Proven inert server-side; sent empty to match the site exactly.
            "applied_vehicle_class_filters": [],
            "check_if_no_vehicles_available": True,
            "check_if_oneway_allowed": True,
        }


@dataclass(frozen=True)
class Policy:
    code: str | None
    description: str | None


@dataclass(frozen=True)
class Quote:
    request: QuoteRequest
    vehicles: tuple[Vehicle, ...] = ()
    policies: tuple[Policy, ...] = ()
    #: Branch name as the pricing API itself reports it. Populated even when
    #: the caller passed a bare numeric id, so output never has to say
    #: "Location 1022509".
    branch_name: str | None = None

    @property
    def pickup_label(self) -> str:
        if self.branch_name and self.request.pickup.name.startswith("Location "):
            code = self.request.pickup.airport_code
            return f"{self.branch_name}{f' ({code})' if code else ''}"
        return self.request.pickup.label

    @property
    def bookable(self) -> list[Vehicle]:
        return sorted(
            (v for v in self.vehicles if v.bookable),
            key=lambda v: v.total.sort_key,  # type: ignore[union-attr]
        )

    def cheapest(self) -> Vehicle | None:
        found = self.bookable
        return found[0] if found else None

    @property
    def restricted(self) -> list[Vehicle]:
        """Classes the branch will not rent online at a retail rate."""
        return [v for v in self.vehicles if v.status is VehicleStatus.RESTRICTED]

    @property
    def sold_out_categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.vehicles:
            if v.status is VehicleStatus.SOLD_OUT:
                counts[v.category or "Other"] = counts.get(v.category or "Other", 0) + 1
        return counts


#: Message codes the pricing API uses to refuse rather than to answer.
AGE_REFUSED_CODES = frozenset({"PRICING_4463"})
NO_VEHICLES_CODES = frozenset({"PRICING_16007"})
#: Dates outside the bookable horizon (max 395 days ahead). A refusal, not an
#: empty result - retrying the same dates can never succeed.
HORIZON_CODES = frozenset({"PRICING_220"})


def parse_quote(raw: dict, request: QuoteRequest) -> Quote:
    """Turn a raw initiate response into a Quote, or raise a refusal.

    The distinction that matters: a response with `car_classes` *absent* is a
    refusal, while `car_classes` present-but-nothing-bookable is a real empty
    result. Collapsing the two makes a watch loop poll a rental that can never
    be booked.
    """
    reservation = (
        (raw.get("session") or {}).get("gbo") or {}
    ).get("reservation") or {}
    classes = reservation.get("car_classes")

    if classes is None:
        # No vehicle list. Either the API refused the request outright (age,
        # route, booking horizon) - which must not look like an empty result -
        # or it is telling us this branch is genuinely sold out on these dates.
        if _messages_mean_sold_out(raw, request):
            return Quote(request=request)
        _raise_for_messages(raw, request)
        raise UsageError(
            "the pricing API returned no vehicle list and gave no reason - "
            "the request may name a branch or brand it cannot price"
        )

    policies = tuple(
        Policy(_clean(p.get("code")), _clean(p.get("description")))
        for p in (reservation.get("policies") or [])
        if isinstance(p, dict)
    )
    detail = (
        ((raw.get("session") or {}).get("gma") or {}).get("reservation") or {}
    ).get("pickup_location_with_detail") or {}
    return Quote(
        request=request,
        vehicles=tuple(Vehicle.parse(c) for c in classes if isinstance(c, dict)),
        policies=policies,
        branch_name=_clean(detail.get("name")),
    )


def _messages_mean_sold_out(raw: dict, request: QuoteRequest) -> bool:
    """True when the response is a genuine sell-out rather than a refusal.

    Same-country: PRICING_16007 means what it says. Cross-border one-way: the
    identical code almost certainly means the route is not permitted, so it is
    handled as a refusal instead - see `_raise_for_messages`.
    """
    codes = {
        str(m.get("code") or "")
        for m in (raw.get("messages") or [])
        if isinstance(m, dict)
    }
    if not codes or not (codes & NO_VEHICLES_CODES):
        return False
    # A one-way that is - or might be - cross-border is handled as a refusal
    # instead, because the API uses this same code for a route it will not
    # permit. Only a trip we are confident is domestic is taken at face value.
    return not request.maybe_cross_border


def _raise_for_messages(raw: dict, request: QuoteRequest) -> None:
    """Translate an API refusal message into the right exception."""
    for message in raw.get("messages") or []:
        if not isinstance(message, dict):
            continue
        code = str(message.get("code") or "")
        text = _clean(message.get("message")) or code

        if code in HORIZON_CODES:
            raise UsageError(f"{text} [{code}]")

        if code in AGE_REFUSED_CODES:
            raise AgeRefused(
                f"{text}\n"
                f"    Renter age {request.age} is below the minimum for "
                f"{request.pickup.label}."
            )

        if code in NO_VEHICLES_CODES and request.maybe_cross_border:
            # Same message the API uses for a genuine sell-out, but a
            # same-country one-way works on these dates, so a route
            # restriction is overwhelmingly likelier. Say so rather than
            # sending someone hunting for dates that will never work.
            if request.is_cross_border:
                route = (
                    f"a cross-border one-way ({request.pickup.country} -> "
                    f"{request.dropoff.country})"
                )
            else:
                route = (
                    "a one-way whose branch countries could not both be "
                    "confirmed, so it may be cross-border"
                )
            raise RouteRefused(
                f"{text}\n"
                f"    {request.pickup.label} -> {request.dropoff.label} is "
                f"{route}.\n"
                f"    Enterprise reports a route it will not permit with the "
                f"same message it uses for a sold-out branch, so this is more "
                f"likely a route restriction than a date problem. Confirm with "
                f"the branch before trying other dates."
            )

        if code in NO_VEHICLES_CODES:
            # Same-country: take the message at face value - a real sell-out.
            return

        if str(message.get("priority") or "").upper() == "ERROR":
            raise UsageError(f"{text} [{code}]")


def sort_by_total(vehicles: Iterable[Vehicle]) -> list[Vehicle]:
    """Sort by what is actually charged. Never by the converted figure."""
    priced = [v for v in vehicles if v.total is not None]
    return sorted(priced, key=lambda v: v.total.sort_key)  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# Branch hours
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DayHours:
    """One date's two independent windows.

    `counter` is when staff are there to hand a car over; `drop` is when you
    may leave one, and is often 24h where the counter is not. A late flight can
    usually return a car when it cannot collect one, which is what decides
    whether a cheap downtown branch is actually usable.
    """

    date: str
    counter: str
    drop: str


def _window(node: Any) -> str:
    if not isinstance(node, dict):
        return "-"
    if node.get("open24Hours"):
        return "24 hours"
    if node.get("closed"):
        return "closed"
    spans = [
        f"{h.get('open')}-{h.get('close')}"
        for h in node.get("hours") or []
        if isinstance(h, dict)
    ]
    return ", ".join(spans) if spans else "closed"


def parse_hours(raw: Any) -> list[DayHours]:
    """Flatten ``{"data": {"YYYY-MM-DD": {"STANDARD": .., "DROP": ..}}}``."""
    data = (raw or {}).get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return []
    return [
        DayHours(
            date=date,
            counter=_window((data[date] or {}).get("STANDARD")),
            drop=_window((data[date] or {}).get("DROP")),
        )
        for date in sorted(data)
    ]


def parse_age_policy(raw: Any) -> str:
    """Minimum renter age, as a sentence. Falls back to the universal rule."""
    if isinstance(raw, dict):
        minimum = raw.get("minimum_age") or raw.get("min_age")
        if minimum:
            return (
                f"minimum renter age {minimum}; under-25 rates and available "
                f"classes both differ"
            )
    # Nothing usable came back. Say that, rather than asserting a policy under
    # the branch heading as though the API had confirmed it.
    return (
        "minimum age not reported by the API for this branch - under-25 "
        "renters generally pay a surcharge and cannot rent every class, but "
        "confirm with the branch"
    )
