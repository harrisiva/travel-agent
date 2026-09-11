"""Typed records for the parsed Google Flights payload.

Everything Google returns is a positional, untyped JSON array. That is the
single biggest hazard in this skill: a shifted index yields a *plausible wrong
answer* rather than a crash — a price read from the wrong slot is still an
integer. So the payload is converted into these records exactly once, at the
edge, by ``parse.py``; nothing downstream indexes raw arrays.

Every field here was confirmed against a live payload, and the index it came
from is named in ``parse.py`` next to the read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Leg:
    """One flight segment — a single takeoff and landing."""

    carrier: str                  # "F8"
    carrier_name: str             # "Flair Airlines"
    flight_number: str            # "654"
    origin: str                   # "YYZ"
    origin_name: str              # "Toronto Pearson International Airport"
    destination: str              # "YHZ"
    destination_name: str
    #: Naive local time at the airport concerned, no UTC offset and no zone:
    #: "2026-09-25T13:00:00" means 13:00 as the departure board shows it.
    #: Google ships [y, m, d] and [h, m] with no zone at all, so there is
    #: nothing to attach one from.
    depart: str
    #: Naive local time at the *arrival* airport — so on a flight that crosses
    #: a timezone, arrive minus depart is not the flight's length and can even
    #: be negative. Elapsed time is duration_minutes; never subtract the two.
    arrive: str
    duration_minutes: int
    aircraft: str | None          # "Boeing 737MAX 8 Passenger"
    legroom: str | None           # "29 in"
    co2_grams: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Layover:
    """One connection between two legs.

    Google ships the arrival and departure airport of a connection as two
    separate codes, which is not redundant: on a change-of-airport connection
    (land at LHR, leave from LGW) they differ, and a traveller told only
    "connects at LHR" would miss a transfer across London. ``minutes`` is
    Google's own connection time, which is why it is read rather than derived
    from the two adjacent legs -- the arithmetic on those is naive local times
    across a timezone change and can come out negative.
    """

    code: str                 # airport arrived at
    depart_code: str          # airport left from; differs on an airport change
    name: str | None
    city: str | None
    minutes: int | None

    @property
    def airport_change(self) -> bool:
        return bool(self.depart_code) and self.depart_code != self.code

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["airport_change"] = self.airport_change
        return data


@dataclass(frozen=True)
class Itinerary:
    """One priced option: the legs of a single direction, plus its fare.

    ``price`` is the *total for the whole trip* in the requested currency, even
    though ``legs`` covers only the outbound direction of a round trip. That
    asymmetry is Google's, not ours, and it is the most misreadable thing in
    the payload — hence ``price_covers``.
    """

    price: int | None
    currency: str
    price_covers: str             # "round trip" | "one way"
    #: Google's *headline* carrier for the option. Usually an IATA code
    #: ("F8"), but on an itinerary flown by more than one airline it is the
    #: literal string "multi" -- not a code, and not one of the airlines. Do
    #: not filter or display on this alone; `carriers` is the list actually
    #: flown. Kept verbatim rather than substituted, because quietly putting
    #: one leg's code here would read as a single-carrier trip.
    carrier: str
    #: The first name Google lists. On a "multi" itinerary that is one airline
    #: out of several -- see `carrier_names` for all of them.
    carrier_name: str
    origin: str
    destination: str
    #: Naive local time, no offset — local at the origin for depart, local at
    #: the destination for arrive. Use duration_minutes for elapsed time: the
    #: difference between these two stamps is wrong across a timezone change.
    depart: str
    arrive: str
    duration_minutes: int
    stops: int
    legs: list[Leg]
    layovers: list[str] = field(default_factory=list)   # IATA codes, in order
    #: Every airline named on the option, in Google's order. A single-carrier
    #: itinerary has exactly one; a "multi" one has all of them, which is what
    #: a human needs to be told.
    carrier_names: list[str] = field(default_factory=list)
    #: The carrier codes actually flown, deduplicated, in leg order. This is
    #: the honest answer to "who flies this" when `carrier` says "multi".
    carriers: list[str] = field(default_factory=list)
    #: The connections, in order, parallel to `layovers` but carrying the
    #: connection time and the change-of-airport case. `layovers` stays a bare
    #: code list because that is what the airport filters match on.
    layover_details: list[Layover] = field(default_factory=list)
    co2_grams: int | None = None
    co2_typical_grams: int | None = None
    co2_percent_vs_typical: int | None = None
    bucket: str = "other"         # "best" | "other" — Google's own ranking
    token: str | None = None      # opaque; identifies this option to Google

    @property
    def nonstop(self) -> bool:
        return self.stops == 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["nonstop"] = self.nonstop
        # asdict() flattens the dataclasses but not their properties, and
        # airport_change is the one thing a JSON consumer cannot re-derive
        # without knowing the convention. Re-render the list rather than
        # patching it in place.
        data["layover_details"] = [lo.to_dict() for lo in self.layover_details]
        return data


@dataclass(frozen=True)
class PriceContext:
    """Google's own verdict on whether the current fare is a good one.

    This is the part no listing scraper can reproduce, and the reason the
    ``price-check`` command exists. ``verdict`` is Google's wording ("low",
    "typical", "high"); ``history`` is a daily series of the cheapest fare
    seen for this route, which is what the UI plots.
    """

    verdict: str | None                  # "low" | "typical" | "high" | None
    current: int | None
    typical: int | None
    low: int | None
    high: int | None
    currency: str
    history: list[tuple[str, int]] = field(default_factory=list)  # (ISO date, price)
    advice: str | None = None            # e.g. "cheapest to book 1-4 months ahead"
    #: Google's own verdict enum from the payload array, independent of the
    #: rendered banner `verdict` is scraped from. Its value-to-word mapping is
    #: NOT established — only that 3 accompanied "typical" in one capture — so
    #: it is deliberately not translated. It exists to tell two cases apart: a
    #: response that genuinely published no verdict (no code either), and one
    #: where the banner wording changed and the scrape silently stopped working
    #: (code present, word missing).
    verdict_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["history"] = [{"date": d, "price": p} for d, p in self.history]
        return data


@dataclass(frozen=True)
class RouteFilters:
    """What is actually filterable on this route, straight from the payload.

    Lets the agent offer real choices ("Porter, Flair, Air Canada and WestJet
    fly this") instead of guessing, and lets the CLI reject a filter that
    cannot match anything before spending a request on it.
    """

    price_min: int | None
    price_max: int | None
    currency: str
    airlines: list[tuple[str, str]] = field(default_factory=list)   # (code, name)
    alliances: list[tuple[str, str]] = field(default_factory=list)
    connection_airports: list[tuple[str, str]] = field(default_factory=list)
    duration_min_minutes: int | None = None
    duration_max_minutes: int | None = None
    #: Google's own enum for its "stops" filter, NOT a count of stops: the
    #: values run 1, 2, 3 on a route whose flights are nonstop. Passed through
    #: verbatim rather than translated, because guessing the mapping is exactly
    #: how this payload produces a plausible wrong answer.
    stop_filter_options: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_min": self.price_min,
            "price_max": self.price_max,
            "currency": self.currency,
            "airlines": [{"code": c, "name": n} for c, n in self.airlines],
            "alliances": [{"code": c, "name": n} for c, n in self.alliances],
            "connection_airports": [
                {"code": c, "name": n} for c, n in self.connection_airports
            ],
            "duration_min_minutes": self.duration_min_minutes,
            "duration_max_minutes": self.duration_max_minutes,
            "stop_filter_options": self.stop_filter_options,
        }


@dataclass(frozen=True)
class Airport:
    """An airport named anywhere in the payload — used to resolve codes."""

    code: str
    name: str
    city: str | None
    country: str | None
    latitude: float | None
    longitude: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    """Everything one search page yields."""

    itineraries: list[Itinerary]
    price_context: PriceContext | None
    filters: RouteFilters | None
    airports: list[Airport] = field(default_factory=list)
    #: Options Google returned that did not match the expected shape and were
    #: dropped. Surfaced rather than swallowed: a non-zero count means the
    #: answer is incomplete, which the user deserves to know.
    unparsed: int = 0

    #: The currency the PAGE actually priced in, read off its rendered markup,
    #: or None when it could not be told confidently. `Itinerary.currency`
    #: records what was *asked for*; when these disagree, the fares are not in
    #: the currency they are labelled with. `Client` refuses that outright, but
    #: the discrepancy is carried here too so any caller of `search_result`
    #: — a lower layer that cannot refuse anything — can still see it.
    detected_currency: str | None = None
