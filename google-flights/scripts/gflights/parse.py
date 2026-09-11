"""Turn a Google Flights search page into typed records.

Google server-renders results into the HTML as::

    AF_initDataCallback({key: 'ds:1', hash: '...', data: [...], sideChannel: {}});

so there is no JSON endpoint to call — the JSON *is* the page. Block ``ds:1``
holds the results; ``ds:0`` echoes the query; ``ds:2``-``ds:4`` are static
country/currency/language lists we never need.

**Why this module is defensive.** The payload is a positional array with no
field names, no version, and no schema. If Google inserts one element, every
index after it shifts and the tool starts reporting a *plausible wrong answer*
— a duration where a price should be, an integer either way. Nothing raises.
That is the exact failure this repo's self-check convention exists to catch, so
every read here goes through a guard that checks the shape it expects and
raises ``PayloadError`` when it does not hold. A loud failure is always better
than a confident wrong fare.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .model import (
    Airport,
    Itinerary,
    Layover,
    Leg,
    PriceContext,
    RouteFilters,
    SearchResult,
)

#: The results block. Non-greedy up to the closing `);</script>` so a page with
#: several AF_initDataCallback blocks still splits correctly.
_BLOCK = re.compile(
    r"AF_initDataCallback\((\{key:\s*'(ds:\d+)'.*?)\);</script>", re.DOTALL
)
_DATA = re.compile(r"data:(.*?), sideChannel", re.DOTALL)

#: The rendered verdict ("Prices are currently typical"). Read from the DOM
#: rather than the array because the array stores it as a bare enum whose
#: meaning is not self-evident, and this string is what Google actually shows.
_VERDICT = re.compile(
    r"Prices are currently\s*<span[^>]*>([^<]{1,40})</span>", re.IGNORECASE
)
#: Spans nested <span> tags ("usually earlier, <span>about <span>1-4
#: months</span> before takeoff</span>"), so capture the block and strip tags
#: rather than stopping at the first "<".
_BOOK_ADVICE = re.compile(
    r"The cheapest time to book is usually (.{1,200}?)</div>", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")

#: Emissions, cross-checked against the itinerary array. Present once per page.
_CO2_TYPICAL = re.compile(r'data-co2typical="(\d+)"')

#: Google omits trailing zeros in its [hour, minute] arrays and writes a null
#: hour for midnight, so [13] means 13:00 and [null, 49] means 00:49.
_MIDNIGHT = 0


class UnresolvableQuery(ValueError):
    """Google could not resolve the query at all — it returned an empty results
    block rather than an error. Seen when a place cannot be resolved, or the
    endpoints are the same. A usage problem (exit 2), not a network one."""


class PayloadError(RuntimeError):
    """The page parsed, but its shape was not what we expect.

    Almost always means Google changed the payload layout. Exit code 3: the
    query may have been perfectly valid, so this must never be reported as
    "no flights found".
    """


def _text(html_fragment: str) -> str:
    """Visible text of a small HTML fragment, whitespace collapsed."""
    return " ".join(_TAGS.sub(" ", html_fragment).split()).strip().rstrip(",")


def _guard(condition: bool, what: str) -> None:
    if not condition:
        raise PayloadError(
            f"unexpected payload shape: {what}. Google's response layout has "
            f"probably changed — rerun the self-check (test_flights.py) to "
            f"confirm, and do not trust any result from this run."
        )


def blocks(html: str) -> dict[str, Any]:
    """Every AF_initDataCallback block on the page, keyed by 'ds:N'."""
    found: dict[str, Any] = {}
    for match in _BLOCK.finditer(html):
        body, key = match.group(1), match.group(2)
        data = _DATA.search(body)
        if not data:
            continue
        try:
            found[key] = json.loads(data.group(1))
        except json.JSONDecodeError:
            continue  # a block we do not need; ds:1 failing is caught below
    return found


def _at(seq: Any, *path: int) -> Any:
    """Index a nested list, returning None instead of raising on any miss.

    The payload is full of legitimately absent branches (no layovers, no
    emissions estimate), so absence is normal and must not be an error. Shape
    violations that *do* matter are caught by explicit _guard calls.
    """
    node = seq
    for index in path:
        if not isinstance(node, list) or len(node) <= index:
            return None
        node = node[index]
    return node


def _str_at(seq: Any, *path: int) -> str:
    """Like ``_at``, but never returns anything but a string.

    Every name and code in this payload is read out of a positional slot, so a
    shifted index puts an int or a nested list where a carrier code or an
    airport name belongs. Callers slice and measure these values, and a
    TypeError raised three modules away names nothing useful. Returning "" for
    a non-string keeps the failure where it can be described: the guards below
    see an empty code and report a layout change.
    """
    value = _at(seq, *path)
    return value if isinstance(value, str) else ""


def _int_at(seq: Any, *path: int) -> int | None:
    """Like ``_at``, but never returns anything but an int.

    Same reasoning as ``_str_at``, for the slots that get compared, summed or
    formatted. ``bool`` is excluded deliberately: it is an ``int`` in Python,
    and a stray ``True`` formatted as a duration of 1 minute is exactly the
    plausible wrong answer this module exists to prevent.
    """
    value = _at(seq, *path)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_iata(code: Any) -> bool:
    """Three letters, checked as a *string*.

    ``len(code) == 3`` is not this test, and the difference is a real bug:
    it raises TypeError on the int a shifted index puts there, and it happily
    passes a three-element list. Both shapes were observed while shifting the
    payload by hand.
    """
    return isinstance(code, str) and len(code) == 3 and code.isalpha()


def _clock(value: Any) -> tuple[int, int]:
    """Google's [hour, minute] with trailing zeros and midnight elided."""
    if not isinstance(value, list) or not value:
        return (_MIDNIGHT, 0)
    hour = value[0] if isinstance(value[0], int) else _MIDNIGHT
    minute = value[1] if len(value) > 1 and isinstance(value[1], int) else 0
    return (hour, minute)


def _iso(day: Any, clock: Any) -> str | None:
    """Combine Google's [y, m, d] and [h, m] into a naive ISO 8601 stamp."""
    if not isinstance(day, list) or len(day) < 3:
        return None
    hour, minute = _clock(clock)
    try:
        return datetime(day[0], day[1], day[2], hour, minute).isoformat()
    except (TypeError, ValueError):
        return None


#: Half a day in milliseconds — the snap radius in ``_iso_date``.
_HALF_DAY_MS = 12 * 60 * 60 * 1000
_DAY_MS = 24 * 60 * 60 * 1000
_EPOCH = date(1970, 1, 1)


def _iso_date(epoch_ms: Any) -> str | None:
    """The calendar day one price-history point stands for.

    Google stamps each point at *local midnight at the origin airport*. The
    same 61-day series comes back at 04:00Z from JFK (UTC-4), 07:00Z from YVR
    (-7), 22:00Z from FRA (+2), 18:30Z from DEL (+5:30) and 15:00Z from NRT
    (+9) — all measured on live pages. So the day it means is the one whose
    midnight the stamp is, in a timezone the payload never names and that we
    cannot look up without a tz database.

    ``date.fromtimestamp`` resolved it in the *runner's* timezone, which is
    wherever Claude happens to be running. That made the answer depend on the
    container: the NRT series read on a UTC host comes out a day early, the
    YVR series read from Berlin comes out a day late, and nothing looks wrong
    — a history point silently attributed to the wrong day is exactly the
    plausible-wrong-value failure this module exists to prevent.

    Snapping to the nearest UTC midnight instead recovers the origin's own
    calendar date for any UTC offset inside ±12h, and gives the same answer on
    every machine. Origins at UTC+13/+14 (Auckland on daylight time, Apia,
    Kiritimati) are the known exception and come back one day late; that is a
    handful of airports, and it is at least deterministic.
    """
    if not isinstance(epoch_ms, (int, float)) or isinstance(epoch_ms, bool):
        return None
    try:
        return (_EPOCH + timedelta(
            days=(int(epoch_ms) + _HALF_DAY_MS) // _DAY_MS
        )).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Itineraries
#
# Index map, confirmed against a live payload (see NOTES.md). Named constants
# rather than bare integers, because a bare `it[0][9]` three functions deep is
# unreviewable and this is precisely where a silent index shift would hide.
# ---------------------------------------------------------------------------

_GROUP = 0          # itinerary -> the flight group (carrier, legs, endpoints)
_FARE = 1           # itinerary -> [[null, price], token]

_G_CARRIER = 0
_G_CARRIER_NAMES = 1
_G_LEGS = 2
_G_ORIGIN = 3
_G_DEPART_DATE = 4
_G_DEPART_TIME = 5
_G_DESTINATION = 6
_G_ARRIVE_DATE = 7
_G_ARRIVE_TIME = 8
_G_DURATION = 9
#: The connection list, one record per gap between consecutive legs. Confirmed
#: on every itinerary of twelve live routes: len == len(legs) - 1, and record k
#: names the airport leg k lands at.
_G_LAYOVERS = 13
_G_EMISSIONS = 22

_LO_MINUTES = 0
_LO_ARRIVE_CODE = 1
_LO_DEPART_CODE = 2      # differs from the arrival code on an airport change
_LO_ARRIVE_NAME = 4
_LO_ARRIVE_CITY = 5

_E_PERCENT_VS_TYPICAL = 3
_E_GRAMS = 7
_E_TYPICAL_GRAMS = 8

_L_ORIGIN = 3
_L_ORIGIN_NAME = 4
_L_DESTINATION_NAME = 5
_L_DESTINATION = 6
_L_DEPART_TIME = 8
_L_ARRIVE_TIME = 10
_L_DURATION = 11
_L_LEGROOM = 14
_L_AIRCRAFT = 17
_L_DEPART_DATE = 20
_L_ARRIVE_DATE = 21
_L_FLIGHT = 22       # [carrier, number, null, carrier name]
_L_CO2 = 31


def _names_at(seq: Any, *path: int) -> list[str]:
    """A list of non-empty strings from a slot, or [] if it is anything else."""
    value = _at(seq, *path)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _layovers(group: Any, legs: list[Leg]) -> list[Layover]:
    """The connections of one itinerary, cross-checked against its legs.

    Two things are read here that cannot be derived from the legs. The
    connection time is Google's own: subtracting the adjacent legs' stamps
    would be arithmetic on naive local times across a timezone change, which
    comes out wrong and sometimes negative. And the departure airport of a
    connection is a separate field, because it is not always the arrival one —
    a change-of-airport connection is a transfer the traveller has to make.

    Every record is checked against the leg it belongs to. That check is the
    point: if Google moves this list, the codes stop lining up and the row is
    rejected, instead of the tool reporting a 90-minute connection that is
    really a duration, an emissions figure, or somebody else's flight.
    """
    if len(legs) < 2:
        return []
    records = _at(group, _G_LAYOVERS)
    if records is None:
        # Google omits the block on some payloads; the airport codes are still
        # recoverable from the legs, so this degrades rather than failing.
        return []
    _guard(
        isinstance(records, list) and len(records) == len(legs) - 1,
        f"itinerary[{_G_LAYOVERS}] holds {len(records) if isinstance(records, list) else type(records).__name__} "
        f"connections for {len(legs)} legs",
    )
    out: list[Layover] = []
    for index, record in enumerate(records):
        code = _str_at(record, _LO_ARRIVE_CODE)
        depart_code = _str_at(record, _LO_DEPART_CODE)
        _guard(
            code == legs[index].destination
            and depart_code == legs[index + 1].origin,
            f"connection {index} is {code!r}->{depart_code!r} but the legs "
            f"land at {legs[index].destination!r} and leave from "
            f"{legs[index + 1].origin!r}",
        )
        out.append(
            Layover(
                code=code,
                depart_code=depart_code,
                name=_str_at(record, _LO_ARRIVE_NAME) or None,
                city=_str_at(record, _LO_ARRIVE_CITY) or None,
                minutes=_int_at(record, _LO_MINUTES),
            )
        )
    return out


def _leg(raw: Any) -> Leg:
    flight = _at(raw, _L_FLIGHT) or []
    number = _at(flight, 1)
    return Leg(
        carrier=_str_at(flight, 0),
        carrier_name=_str_at(flight, 3),
        # The flight number is an int in the payload and a string in the record
        # (it can carry a letter), so this one slot is converted rather than
        # type-filtered — but only from a scalar, never from a shifted list.
        flight_number=str(number) if isinstance(number, (str, int)) else "",
        origin=_str_at(raw, _L_ORIGIN),
        origin_name=_str_at(raw, _L_ORIGIN_NAME),
        destination=_str_at(raw, _L_DESTINATION),
        destination_name=_str_at(raw, _L_DESTINATION_NAME),
        depart=_iso(_at(raw, _L_DEPART_DATE), _at(raw, _L_DEPART_TIME)) or "",
        arrive=_iso(_at(raw, _L_ARRIVE_DATE), _at(raw, _L_ARRIVE_TIME)) or "",
        duration_minutes=_int_at(raw, _L_DURATION) or 0,
        aircraft=_str_at(raw, _L_AIRCRAFT) or None,
        legroom=_str_at(raw, _L_LEGROOM) or None,
        co2_grams=_int_at(raw, _L_CO2),
    )


def _itinerary(raw: Any, bucket: str, currency: str, price_covers: str) -> Itinerary:
    group = _at(raw, _GROUP)
    _guard(isinstance(group, list), f"itinerary[{_GROUP}] is not a flight group")

    raw_legs = _at(group, _G_LEGS)
    _guard(
        isinstance(raw_legs, list) and bool(raw_legs),
        "itinerary has no legs — every priced option must have at least one",
    )
    legs = [_leg(leg) for leg in raw_legs]
    # Legs were built with no checks at all, so an index shift inside one of
    # them produced a flight with a blank code, a blank clock or a zero
    # duration and the itinerary still rendered — "YYZ 00:00, 0h 0m" reads as
    # data, not as a fault. Every leg of every itinerary across twelve live
    # routes satisfies all three, so a failure here is a layout change, and a
    # single odd row is dropped and counted rather than shown.
    for leg in legs:
        _guard(
            _is_iata(leg.origin) and _is_iata(leg.destination),
            f"leg endpoints {leg.origin!r}->{leg.destination!r} are not IATA codes",
        )
        _guard(
            bool(leg.depart) and bool(leg.arrive),
            f"leg {leg.origin}->{leg.destination} has no readable clock times",
        )
        _guard(
            leg.duration_minutes > 0,
            f"leg {leg.origin}->{leg.destination} has duration "
            f"{leg.duration_minutes!r}, which is not a flight",
        )

    price = _at(raw, _FARE, 0, 1)
    _guard(
        price is None or isinstance(price, (int, float)),
        f"price is {type(price).__name__}, expected a number",
    )

    depart = _iso(_at(group, _G_DEPART_DATE), _at(group, _G_DEPART_TIME))
    arrive = _iso(_at(group, _G_ARRIVE_DATE), _at(group, _G_ARRIVE_TIME))
    _guard(bool(depart), "itinerary has no readable departure time")
    # Guarded as hard as the departure. An unreadable arrival used to fall
    # through as "", which the time filters read as "no arrival stated" and
    # skip — so an itinerary whose arrival slot had moved would quietly pass
    # an --arrive-before it might well violate.
    _guard(bool(arrive), "itinerary has no readable arrival time")

    duration = _at(group, _G_DURATION)
    _guard(
        isinstance(duration, int) and 0 < duration < 60 * 24 * 5,
        f"duration {duration!r} is not a plausible minute count",
    )

    emissions = _at(group, _G_EMISSIONS)
    # An endpoint Google simply omitted is recoverable from the legs. One that
    # is *present but not a code* is not the same thing: it is a shifted index,
    # and quietly substituting the leg's airport would hide the shift behind an
    # answer that looks right. So only a missing slot falls back; anything else
    # goes to the guard, which rejects a non-string and a three-element list
    # alike — `len(x) == 3` does neither.
    origin = _at(group, _G_ORIGIN)
    destination = _at(group, _G_DESTINATION)
    if origin is None and legs:
        origin = legs[0].origin
    if destination is None and legs:
        destination = legs[-1].destination
    _guard(
        _is_iata(origin) and _is_iata(destination),
        f"endpoints {origin!r}->{destination!r} are not IATA codes",
    )

    # Google prices in whole currency units on every market checked (including
    # the zero-decimal ones — JPY 30370, KRW 210500), so this is defensive.
    # It rounds rather than truncating: int(560.6) would report 560 and
    # understate a fare, and understating is the direction that costs a user
    # money when they go to book.
    return Itinerary(
        price=round(price) if isinstance(price, (int, float)) else None,
        currency=currency,
        price_covers=price_covers,
        carrier=_str_at(group, _G_CARRIER),
        carrier_name=_str_at(group, _G_CARRIER_NAMES, 0),
        origin=origin,
        destination=destination,
        depart=depart or "",
        arrive=arrive or "",
        duration_minutes=duration,
        # The number of legs, not group[10]: that slot is None on most rows
        # and 1, 2 or 3 on others with no relation to the leg count (it is 2
        # on a nonstop YVR-SYD). The connection list is len(legs) - 1 long on
        # every itinerary of every route checked, which is the corroboration.
        stops=len(legs) - 1,
        legs=legs,
        layovers=[leg.destination for leg in legs[:-1]],
        # isinstance before iterating, not `or []`: a shifted index puts a
        # bare string here, and iterating a string yields its characters —
        # a carrier list of ["A", "i", "r"] that looks like data.
        carrier_names=_names_at(group, _G_CARRIER_NAMES),
        # Deduplicated in leg order — dict.fromkeys, not set(), because the
        # order is the route and a set would scramble it.
        carriers=list(dict.fromkeys(leg.carrier for leg in legs if leg.carrier)),
        layover_details=_layovers(group, legs),
        co2_grams=_int_at(emissions, _E_GRAMS),
        co2_typical_grams=_int_at(emissions, _E_TYPICAL_GRAMS),
        co2_percent_vs_typical=_int_at(emissions, _E_PERCENT_VS_TYPICAL),
        bucket=bucket,
        token=_str_at(raw, _FARE, 1) or None,
    )


# ---------------------------------------------------------------------------
# Price context, filters, airports
# ---------------------------------------------------------------------------

_PC_VERDICT_CODE = 0
_PC_CURRENT = 1
_PC_TYPICAL = 2
_PC_LOW = 4
_PC_HIGH = 5
#: The history is wrapped one level deeper than its sibling scalars:
#: node[10] is a list *containing* the list of [epoch_ms, price] points.
_PC_HISTORY = 10

_F_PRICE_BOUNDS = 0
_F_GROUPS = 1          # [alliances, airlines]
_F_CONNECTIONS = 2     # [[(code, city), ...], min, max]
_F_DURATION = 3        # [min, max] in minutes
_F_STOPS = 4


def _price_context(node: Any, currency: str, html: str) -> PriceContext | None:
    if not isinstance(node, list):
        return None
    verdict = _VERDICT.search(html)
    advice = _BOOK_ADVICE.search(html)
    # A point is kept only when both halves are the type the chart implies. A
    # shifted series otherwise plots strings as fares, which renders without
    # complaint and reads as real price history.
    history = [
        (_iso_date(point[0]), point[1])
        for point in (_at(node, _PC_HISTORY, 0) or [])
        if isinstance(point, list) and len(point) >= 2 and _iso_date(point[0])
        and isinstance(point[1], (int, float)) and not isinstance(point[1], bool)
    ]
    return PriceContext(
        verdict=verdict.group(1).strip().lower() if verdict else None,
        verdict_code=_int_at(node, _PC_VERDICT_CODE),
        current=_int_at(node, _PC_CURRENT, 1),
        typical=_int_at(node, _PC_TYPICAL, 1),
        low=_int_at(node, _PC_LOW, 1),
        high=_int_at(node, _PC_HIGH, 1),
        currency=currency,
        history=history,
        advice=_text(advice.group(1)) if advice else None,
    )


def _pairs(node: Any) -> list[tuple[str, str]]:
    return [
        (item[0], item[1])
        for item in (node or [])
        if isinstance(item, list) and len(item) >= 2
        and isinstance(item[0], str) and isinstance(item[1], str)
    ]


def _filters(node: Any, currency: str) -> RouteFilters | None:
    if not isinstance(node, list):
        return None
    stops = _at(node, _F_STOPS, 0)
    return RouteFilters(
        price_min=_int_at(node, _F_PRICE_BOUNDS, 0, 1),
        price_max=_int_at(node, _F_PRICE_BOUNDS, 1, 1),
        currency=currency,
        alliances=_pairs(_at(node, _F_GROUPS, 0)),
        airlines=_pairs(_at(node, _F_GROUPS, 1)),
        connection_airports=_pairs(_at(node, _F_CONNECTIONS, 0)),
        duration_min_minutes=_int_at(node, _F_DURATION, 0),
        duration_max_minutes=_int_at(node, _F_DURATION, 1),
        stop_filter_options=[s for s in (stops or []) if isinstance(s, int)],
    )


_A_CODE = 0
_A_NAME = 1
_A_PLACE = 2          # ["/m/0h7h6", "Toronto", [image urls]]
_A_COORDS = 3         # [latitude, longitude]
_A_COUNTRY = 4        # "CA"
_A_COUNTRY_NAME = 6   # "Canada"


def _airports(nodes: Any) -> list[Airport]:
    """Airports described anywhere in the payload, deduplicated by code."""
    found: dict[str, Airport] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, list):
            return
        # [["YYZ", 0], "Toronto Pearson ...", ["/m/...", "Toronto", [images]],
        #  [43.67, -79.63], "CA", 0, "Canada"]
        code, name, coords = _at(node, _A_CODE, 0), _at(node, _A_NAME), _at(node, _A_COORDS)
        if _is_iata(code) and isinstance(name, str):
            found.setdefault(
                code,
                Airport(
                    code=code,
                    name=name,
                    city=_at(node, _A_PLACE, 1),
                    country=_at(node, _A_COUNTRY_NAME) or _at(node, _A_COUNTRY),
                    latitude=_at(coords, 0) if isinstance(coords, list) else None,
                    longitude=_at(coords, 1) if isinstance(coords, list) else None,
                ),
            )
        for child in node:
            walk(child)

    walk(nodes)
    return sorted(found.values(), key=lambda a: a.code)


# ---------------------------------------------------------------------------
# Currency verification
#
# `--currency` is sent to Google and then stamped onto every itinerary, so if
# Google ignores it the tool labels CAD fares "XYZ" and nothing complains — a
# mislabelled price is a silent wrong answer of the worst kind, because the
# number itself looks right. The rendered page carries the truth: prices are
# announced to screen readers by currency *name* ("98 Canadian dollars") and
# printed with a symbol or code prefix ("CA$98", "PLN 400"). Reading it back
# out costs nothing and turns that failure into a warning the user can see.
# ---------------------------------------------------------------------------

#: Both spellings Google renders, because the plurals are irregular often
#: enough ("kroner", "reais", "yen") that stripping an "s" would mis-key them.
#: Only fully-qualified names are listed: a bare "dollars" or "pesos" names a
#: dozen different currencies, and guessing one is the failure this exists to
#: catch.
_CURRENCY_NAMES = {
    "canadian dollar": "CAD", "canadian dollars": "CAD",
    "us dollar": "USD", "us dollars": "USD",
    "australian dollar": "AUD", "australian dollars": "AUD",
    "new zealand dollar": "NZD", "new zealand dollars": "NZD",
    "hong kong dollar": "HKD", "hong kong dollars": "HKD",
    "singapore dollar": "SGD", "singapore dollars": "SGD",
    "new taiwan dollar": "TWD", "new taiwan dollars": "TWD",
    "euro": "EUR", "euros": "EUR",
    "british pound": "GBP", "british pounds": "GBP",
    "swiss franc": "CHF", "swiss francs": "CHF",
    "japanese yen": "JPY",
    "chinese yuan": "CNY",
    "south korean won": "KRW",
    "indian rupee": "INR", "indian rupees": "INR",
    "mexican peso": "MXN", "mexican pesos": "MXN",
    "brazilian real": "BRL", "brazilian reais": "BRL",
    "swedish krona": "SEK", "swedish kronor": "SEK",
    "norwegian krone": "NOK", "norwegian kroner": "NOK",
    "danish krone": "DKK", "danish kroner": "DKK",
    "polish zloty": "PLN", "polish zlotys": "PLN",
    "czech koruna": "CZK", "czech korunas": "CZK",
    "hungarian forint": "HUF", "hungarian forints": "HUF",
    "turkish lira": "TRY",
    "israeli new shekel": "ILS", "israeli new shekels": "ILS",
    "south african rand": "ZAR",
    "thai baht": "THB",
    "philippine peso": "PHP", "philippine pesos": "PHP",
    "malaysian ringgit": "MYR", "malaysian ringgits": "MYR",
    "indonesian rupiah": "IDR", "indonesian rupiahs": "IDR",
    "vietnamese dong": "VND",
    "uae dirham": "AED", "uae dirhams": "AED",
    "saudi riyal": "SAR", "saudi riyals": "SAR",
}

#: Prefixes Google prints in front of the number. Deliberately excludes a bare
#: "$": it is USD, CAD, AUD, MXN and a dozen more, and the page is full of
#: minified JS where "$1" is a variable. Ambiguity here means returning None,
#: never a guess.
_CURRENCY_PREFIXES = {
    "CA$": "CAD", "US$": "USD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD",
    "S$": "SGD", "NT$": "TWD", "MX$": "MXN", "R$": "BRL", "CN\u00a5": "CNY",
    "\u20ac": "EUR", "\u00a3": "GBP", "\u00a5": "JPY", "\u20b9": "INR",
    "\u20aa": "ILS", "\u20a9": "KRW", "\u20ab": "VND", "\u20b1": "PHP",
    "\u0e3f": "THB",
}

#: Currencies with no symbol render as the ISO code plus a non-breaking space
#: ("PLN 400"). Only codes we already know are accepted, so an arbitrary
#: three-letter word followed by a number cannot be mistaken for a price.
_CURRENCY_CODES = frozenset(_CURRENCY_NAMES.values()) | frozenset(
    _CURRENCY_PREFIXES.values()
)

#: A screen-reader price label. Anything with a digit in it; the name is looked
#: up inside. Labels without a digit are currency *pickers*, not prices.
_ARIA_LABEL = re.compile(r'aria-label="([^"]{1,160})"')
_NAME_IN_LABEL = re.compile(
    r"\b(" + "|".join(sorted(_CURRENCY_NAMES, key=len, reverse=True)) + r")\b"
)
#: The prefixes contain "$", so every alternative is escaped — unescaped,
#: "CA$" is the regex for "CA at end of string" and matches no price at all.
_PREFIXED_PRICE = re.compile(
    r"(?<![\w$])("
    + "|".join(
        re.escape(prefix)
        for prefix in sorted(_CURRENCY_PREFIXES, key=len, reverse=True)
    )
    + r")\s?\d[\d,.\u00a0 ]*"
)
_CODED_PRICE = re.compile(r"(?<![\w'\"])([A-Z]{3})[\u00a0 ]\d[\d,.]*")

#: One stray match is noise; a priced results page shows the fare many times.
_MIN_PREFIX_HITS = 2


def detected_currency(html: str) -> str | None:
    """The ISO 4217 code the page actually priced in, or None if unsure.

    Google accepts any ``curr=`` value and silently falls back to the currency
    of the point of sale when it does not recognise one, so ``--currency XYZ``
    returns CAD fares that the tool would otherwise label "XYZ". Nothing about
    the number looks wrong, which makes it the most dangerous shape of error
    this skill can produce. The caller cross-checks this against what it asked
    for and warns on a mismatch.

    Precision over recall throughout: None means "no cross-check available"
    and costs the caller nothing, while a wrong answer here would raise a false
    alarm on a correct price — so a page whose evidence is mixed or absent
    returns None rather than a best guess. Evidence is taken in order of
    reliability: the currency names Google writes into its price aria-labels,
    then the symbol or ISO-code prefix on the rendered fares.
    """
    named: set[str] = set()
    for label in _ARIA_LABEL.findall(html):
        if not any(character.isdigit() for character in label):
            continue  # a currency picker, not a price
        match = _NAME_IN_LABEL.search(label.lower())
        if match:
            named.add(_CURRENCY_NAMES[match.group(1)])
    if len(named) == 1:
        return named.pop()
    if named:
        return None  # two currencies on one page: not something to guess at

    prefixed: dict[str, int] = {}
    for symbol in _PREFIXED_PRICE.findall(html):
        code = _CURRENCY_PREFIXES[symbol]
        prefixed[code] = prefixed.get(code, 0) + 1
    for code in _CODED_PRICE.findall(html):
        if code in _CURRENCY_CODES:
            prefixed[code] = prefixed.get(code, 0) + 1
    if len(prefixed) == 1:
        code, hits = next(iter(prefixed.items()))
        return code if hits >= _MIN_PREFIX_HITS else None
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_RESULTS = "ds:1"
_BEST = 2
_OTHER = 3
_PRICE_CONTEXT = 5
_FILTERS = 7
_ENDPOINTS = 1
_NEARBY = 17

#: Above this share of unreadable itineraries, treat the payload as changed
#: rather than the rows as odd.
_MAX_UNPARSED_FRACTION = 0.25


_QUERY_ECHO = "ds:0"
_Q_BODY = 1
_Q_PARTY = 6        # [adults, children, infants_in_seat, infants_on_lap]
_Q_SLICES = 13
_Q_SLICE_DATE = 6


def parsed_query(html: str) -> dict[str, Any] | None:
    """What Google understood the query to be, from its own ds:0 echo.

    This is a free oracle. Google restates the party size and the dates it
    actually searched, so the tool can check that what came back answers the
    question that was asked. That matters more here than anywhere else in this
    codebase: an undocumented positional encoding fails by returning a real
    fare for a *different* query — a wrong party size or a silently substituted
    date — and no amount of validating our own inputs would catch it.

    Returns None when the echo is absent, which is normal on some pages; the
    caller treats that as "no cross-check available", never as a mismatch.
    """
    payload = blocks(html).get(_QUERY_ECHO)
    body = _at(payload, _Q_BODY, _Q_BODY)
    party = _at(body, _Q_PARTY)
    if not isinstance(party, list) or len(party) < 4:
        return None
    dates = [
        _at(slice_, _Q_SLICE_DATE)
        for slice_ in (_at(body, _Q_SLICES) or [])
        if isinstance(slice_, list)
    ]
    return {
        "adults": party[0],
        "children": party[1],
        "infants_in_seat": party[2],
        "infants_on_lap": party[3],
        "dates": [d for d in dates if isinstance(d, str)],
    }


def airports_in(html: str) -> list[Airport]:
    """Every airport named anywhere in a results page.

    Used by the `airports` command: Google resolves a free-text place itself
    and names the airports it would search, including the nearby alternates a
    traveller usually wants to hear about.
    """
    payload = blocks(html).get(_RESULTS)
    if payload is None:
        raise PayloadError(
            "no results block (ds:1) in the response — Google served something "
            "other than a search page, most likely a consent or captcha page."
        )
    return _airports([_at(payload, _ENDPOINTS), _at(payload, _NEARBY)])


def search_result(
    html: str, currency: str, price_covers: str = "round trip"
) -> SearchResult:
    """Parse one search page.

    Raises ``PayloadError`` if the results block is missing or misshapen. An
    *empty* result set is not an error — a route with no flights is a real
    answer, and the caller distinguishes the two by exit code.
    """
    payload = blocks(html).get(_RESULTS)
    if payload is None:
        raise PayloadError(
            "no results block (ds:1) in the response. Google served something "
            "other than a search page — most likely a departure date past the "
            "330-day booking horizon (Google drops the block entirely rather "
            "than saying so), a consent or captcha interstitial, or a layout "
            "change."
        )
    _guard(isinstance(payload, list), "ds:1 is not a list")
    if not payload:
        raise UnresolvableQuery(
            "Google could not resolve this query — it returned an empty "
            "results block. Usually an airport or place it does not recognise, "
            "or an origin and destination that are the same place."
        )

    # One malformed option must not cost the whole search — Google occasionally
    # ships an oddity, and losing 17 good flights to it helps nobody. But
    # silence is the worse failure here, so the drops are counted and a broad
    # failure still raises: past a quarter of the results, the shape has
    # changed rather than one row being strange.
    itineraries: list[Itinerary] = []
    seen = unparsed = 0
    for index, bucket in ((_BEST, "best"), (_OTHER, "other")):
        raw_bucket = _at(payload, index, 0)
        if not isinstance(raw_bucket, list):
            continue  # a bucket may be absent; both being absent is fine too
        for raw in raw_bucket:
            seen += 1
            try:
                itineraries.append(_itinerary(raw, bucket, currency, price_covers))
            except (PayloadError, TypeError, AttributeError,
                    IndexError, KeyError, ValueError):
                # Not every shifted index reaches a guard: an int where a code
                # belongs raises TypeError inside the read itself, and one such
                # row used to abort the whole search. Any of these exceptions
                # means the same thing a PayloadError does — this row is not
                # the shape we expect — so it is counted the same way, and the
                # threshold below still turns a layout change (as opposed to
                # one odd row) into a loud failure.
                unparsed += 1

    if seen and unparsed > seen * _MAX_UNPARSED_FRACTION:
        raise PayloadError(
            f"{unparsed} of {seen} itineraries did not match the expected "
            f"shape. That is a layout change, not a few odd rows — no result "
            f"from this run should be trusted."
        )

    filters = _filters(_at(payload, _FILTERS), currency)

    # A hollow payload — no flights AND no route metadata — is not "this route
    # has no service". It is what Google returns for a query it could not parse,
    # which in practice means the tfs encoding is wrong for this request. A real
    # route with no flights still carries its airline and airport metadata, so
    # this stays a network-class error rather than a silent empty answer.
    if not itineraries and (filters is None or not filters.airlines):
        raise PayloadError(
            "Google returned a results block with neither flights nor route "
            "metadata. That is what an unparseable query looks like, not an "
            "empty route — the tfs encoding is probably wrong for this request."
        )

    return SearchResult(
        itineraries=itineraries,
        unparsed=unparsed,
        detected_currency=detected_currency(html),
        price_context=_price_context(_at(payload, _PRICE_CONTEXT), currency, html),
        filters=filters,
        airports=_airports([_at(payload, _ENDPOINTS), _at(payload, _NEARBY)]),
    )
