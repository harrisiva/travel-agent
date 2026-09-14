"""Entity HTML -> EntityRecord (01 §record, §price block; 04 §5, §6).

The only module that indexes the raw payload. Index positions are named
constants; every read is guarded; per-row failures are counted, not raised.
Lane B owns this file.

Exceptions:
  PayloadError   — the page is not a hotel page we can read (exit 3): ds:2
                   missing (blocked), ds:1 wholly missing, shifted layout,
                   >25% of seller rows unparseable, or an echo mismatch is
                   raised from client.py using Echo.matches().
  UnknownEntity  — ds:2 present AND ds:1 is the error block
                   `data:[<int>], errorHasStatus: true` (exit 2). Never
                   defined as "ds:1 absent" (04 §5.3).

Why this module is defensive (from gflights/parse.py:11-18): the payload is a
positional array with no field names and no version. If Google inserts one
element, every index after it shifts and the tool reports a *plausible wrong
answer* — an ex-tax figure where the incl-tax one belongs. So every read goes
through a guard, and a loud failure is always preferred to a confident wrong
rate.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .model import (
    QUALIFIERS,
    Amenity,
    Breakdown,
    Echo,
    EntityRecord,
    FreeCancellation,
    Headline,
    Highlight,
    HotelIds,
    Price,
    Rating,
    RatingSource,
    Rental,
    SellerRow,
)

#: The healthy-page marker: present on every real hotels page (01 §H4).
RESULTS_MARKER = "AF_initDataCallback({key: 'ds:2'"

#: The record block on an entity page (01 "Blocks per page"): `[record, null, [[widget ids]]]`.
RECORD_KEY = "ds:1"

#: Same regexes as gflights/parse.py:40-43 (`_BLOCK`, `_DATA`): non-greedy up
#: to the closing `);</script>` so several blocks split correctly.
_BLOCK = re.compile(r"AF_initDataCallback\((\{key:\s*'(ds:\d+)'.*?)\);</script>", re.DOTALL)
_DATA = re.compile(r"data:(.*?), sideChannel", re.DOTALL)
#: The unknown-entity block has no `sideChannel` and no `hash` — just
#: `data:[5],errorHasStatus: true` (01 §H4). gflights skips such a block; we
#: must keep it, because "ds:1 absent" is NOT the unknown-entity signature.
_ERROR_DATA = re.compile(r"data:\s*(\[.*?\])\s*,\s*errorHasStatus:\s*true", re.DOTALL)

#: U+202F NARROW NO-BREAK SPACE sits before AM/PM in every time string (01 e[2][17]).
_NNBSP = " "

#: Float equality for the headline ↔ seller-row match (04 §6.2). The two floats
#: are the same JSON literal on every capture; the tolerance only guards a
#: reformatted literal (1452.375 vs 1452.3750).
_MATCH_EPS = 1e-6

#: More than this share of seller rows failing to parse means the row layout
#: moved, and the rows that did parse are not to be trusted either (04 §5.6).
MAX_UNPARSED_SHARE = 0.25

# --- Index map: the hotel record `e = ds:1[0]` (01 "The hotel record") -------
E_NAME = 1              # str
E_PLACE = 2             # [ [lat, lng], address tree, phone, …, [17] times, …, [29] website ]
E_STAR = 3              # ["5-star hotel", 5] or null (rentals)
E_PRICES = 6            # [null, echo, p]
E_REVIEWS = 7           # [[score, count], [[histogram]], …, [4] per-source summaries]
E_FTID = 9              # "0x…:0x…" or null (rentals)
E_AMENITIES = 10        # hotels: [6] grouped ids + chips; rentals: [1] named rows
E_KIND = 14             # 1 hotel, 2 vacation rental
E_RENTAL = 19           # rentals only: [1] = [null, sleeps, bedrooms, bathrooms, beds]
E_TOKEN = 20            # entity token
E_META = 26             # [kind, …, [4] place_id]
E_MIN_LEN = 27          # both pages use the same 27/46-slot record (01)

PLACE_LATLNG = 0        # [lat, lng]
PLACE_ADDRESS = 1       # [[[street address]]]  (entity page only)
PLACE_PHONE = 2         # [display, "tel:…"]
PLACE_TIMES = 17        # [check-in, check-out], U+202F before AM/PM
PLACE_WEBSITE = 29      # [null, null, url]
WEBSITE_URL = 2

REVIEWS_SUMMARY = 0     # [score, count]
REVIEWS_HISTOGRAM = 1   # [[[stars, percent, count], …]]
REVIEWS_SOURCES = 4     # [[name, logo, [score, scale], count, …], …]
SOURCE_NAME, SOURCE_SCORE, SOURCE_COUNT = 0, 2, 3

META_PLACE_ID = 4
RENTAL_FACTS = 1        # [null, sleeps, bedrooms, bathrooms, beds]

# --- e[6]: the price block ---------------------------------------------------
PRICES_ECHO = 1         # the query as Google priced it
PRICES_TABLE = 2        # p
ECHO_CURRENCY = 3       # "CAD"
ECHO_DATES = 4          # [[y,m,d], [y,m,d], nights, null, 0]
ECHO_OCCUPANCY = 13     # [adults, [[age], …] | null, 0]  — null when no ts was sent

P_LEAD = 1              # [ex str, incl str, ex float, null, ex int]; p[1][3] is ALWAYS null
P_LEAD_FLOAT = 2
P_OFFERS = 2            # offers with room lists
P_STAY_STRINGS = 9      # [ex str, incl str] — strings only, never read for a figure
P_FEATURED = 12
P_CURRENCY = 15         # currency actually priced in; null on a page with no headline
P_ALL_OTAS = 21         # all OTA rows
P_HEADLINE_ROWS = 22    # 1–3 headline rows
P_BREAKDOWN = 44        # [base, taxes, fees, total]; ABSENT (len(p) == 43) on the +270d page
P_MIN_LEN = 23          # the four seller slots must be addressable
#: Sellers are the union of these four slots (04 §3.2); any may be null.
SELLER_SLOTS = (P_ALL_OTAS, P_OFFERS, P_FEATURED, P_HEADLINE_ROWS)

# --- one seller row `o` (01 "An OTA row") ------------------------------------
O_SELLER = 0            # [name, partner id, click-out url, [logo]]
SELLER_NAME, SELLER_PARTNER_ID = 0, 1
O_ROOMS = 7             # [[room name, …], …] on p[2] rows; null elsewhere
O_RATES = 12            # [ …, [4] nightly, [5] stay, …, [12] flags ]
RATES_NIGHTLY = 4       # [ex str, incl str, ex float, incl float, ex int, incl int]
RATES_STAY = 5          # same layout
RATES_FLAGS = 12        # [[2, null, 1], free-cancellation, …]
FLAGS_FREE_CANCEL = 1   # [1, "Nov 1", "4:00 PM", "11/1"], or [0] / null
#: Slots inside a rate array. A row with [1] and [3] null and [2] populated is a
#: SINGLE-figure row and [2] is all-in (01 §b) — never read [2] as ex-tax blindly.
RATE_EX_STR, RATE_INCL_STR, RATE_EX, RATE_INCL = 0, 1, 2, 3

# --- e[10]: amenities --------------------------------------------------------
AMEN_RENTAL_ROWS = 1    # rentals: ["Amenities", [[name, has, icon, …], …]]
AMEN_GROUPED = 6        # hotels: [ [0] groups, [1] chips, [2] health & safety, [3] sustainability ]
AMEN_CHIPS = 1          # the four highlight chips [[has, id, qualifier?], …]
ITEM_HAS, ITEM_ID, ITEM_QUALIFIER = 0, 1, 2
RENTAL_ROW_NAME, RENTAL_ROW_HAS = 0, 1

#: Four highlight chips, e[10][6][1]: base id -> name (01 §H5). Extend only from a fixture.
HIGHLIGHT_NAMES: dict[int, str] = {
    28: "Wi-Fi", 54: "Breakfast", 15: "Parking", 19: "Pool",
    26: "Spa", 10: "Hot tub", 23: "Restaurant",
}

# Generated from evidence/amenity-entity-table.json by scratch/lane-b/gen_constants.py
# (Lane B). PROVISIONAL — derived from two distinct properties (Fairmont Banff
# Springs, Samesun Banff); 04 §3.2.1. The build-time check asserts these equal
# the JSON; do not edit by hand. Ids the JSON does not name stay unnamed.
AMENITY_NAMES: dict[int, str] = {
    4: 'Bar',
    8: 'Fitness center',
    9: 'Golf',
    10: 'Hot tub',
    11: 'Kid-friendly',
    14: 'Kitchen in some rooms',
    15: 'Parking',
    18: 'Pet-friendly',
    19: 'Pool',
    20: 'Indoor pool',
    22: 'Outdoor pool',
    23: 'Restaurant',
    24: 'Room service',
    25: 'Smoke-free property',
    26: 'Spa',
    27: 'accessible',
    28: 'Wi-Fi',
    31: 'Full-service laundry',
    33: 'Front desk',
    34: 'Sauna',
    35: 'Massage',
    37: 'Credit cards',
    38: 'Concierge',
    39: 'Car rental onsite',
    40: 'Convenience store',
    41: 'Bicycle rental',
    44: 'Tennis',
    47: 'Horseback riding',
    51: 'Baggage storage',
    54: 'Breakfast',
    56: 'Breakfast buffet',
    89: 'Bathtub in some rooms',
    90: 'Boutique shopping',
    93: 'Cash',
    94: 'Cats allowed',
    96: 'Coffee maker',
    98: 'Debit cards',
    100: 'Dogs allowed',
    102: 'EV charger',
    103: 'Elevator',
    104: 'Elliptical machine',
    105: 'English',
    107: 'Free weights',
    109: 'German',
    110: 'Gift shop',
    112: 'Housekeeping',
    116: 'Activities for kids',
    117: "Kids' club",
    122: 'Local shuttle',
    129: 'NFC mobile payments',
    130: 'Accessible elevator',
    131: 'Accessible parking',
    135: 'Private bathroom',
    136: 'Private bathroom in some rooms',
    137: 'Private car service',
    142: 'Hair salon',
    143: 'Self parking',
    144: 'Shower',
    145: 'Shower in some rooms',
    146: 'Social hour',
    148: 'Table service',
    151: 'Treadmill',
    153: 'Valet parking',
    154: 'Vending machines',
    156: 'Wading pool',
    157: 'Wake up calls',
    161: 'Weight machines',
    162: 'Wi-Fi in public areas',
    163: 'air conditioning',
    164: 'Air conditioning in some rooms',
    190: 'Donates excess food',
    192: 'Food waste reduction program',
    193: 'single-use plastic straws',
    203: 'Safely disposes of electronics, batteries, and lightbulbs',
    207: 'Safely handles hazardous substances',
    210: 'Locally sourced food and beverages',
    213: 'Vegetarian meals',
    214: 'Organic cage-free eggs',
    215: 'Organic food and beverages',
    254: 'Green Key Eco Rating',
}

#: Group id -> heading, same source and status as AMENITY_NAMES. Groups the JSON
#: does not name (4, 15–19, 22, 23) yield `group: None`.
AMENITY_GROUPS: dict[int, str] = {
    2: 'Accessibility',
    3: 'Activities',
    5: 'Children',
    6: 'Food & drink',
    7: 'Internet',
    8: 'Parking & transportation',
    9: 'Pets',
    10: 'Policies & payments',
    11: 'Pools',
    12: 'Rooms',
    13: 'Services',
    14: 'Wellness',
    20: 'Bathrooms',
    21: 'Languages spoken',
    24: 'Waste reduction',
    25: 'Sustainable sourcing',
    26: 'Eco certifications',
}

#: Google's own negated label for a `has = 0` id ("No pools"), when the JSON
#: records one (04 §3.2.1). The current JSON records none; the renderer falls
#: back to "listed as not having: <name>".
AMENITY_NEGATED: dict[int, str] = {
}

#: The 72 currency codes in the page's ds:2 catalogue (04 §2). Generated by Lane B from p8_fairmont_ts.ds2.json.
CURRENCIES: frozenset[str] = frozenset({
    'AED', 'ALL', 'AMD', 'ARS', 'AUD', 'AWG', 'AZN', 'BAM', 'BGN',
    'BHD', 'BMD', 'BRL', 'BSD', 'BYN', 'CAD', 'CHF', 'CLP', 'CNY',
    'COP', 'CRC', 'CUP', 'CZK', 'DKK', 'DOP', 'DZD', 'EGP', 'EUR',
    'GBP', 'GEL', 'HKD', 'HRK', 'HUF', 'IDR', 'ILS', 'INR', 'IRR',
    'ISK', 'JMD', 'JOD', 'JPY', 'KRW', 'KWD', 'KZT', 'LBP', 'MAD',
    'MDL', 'MKD', 'MXN', 'MYR', 'NOK', 'NZD', 'OMR', 'PAB', 'PEN',
    'PHP', 'PKR', 'PLN', 'QAR', 'RON', 'RSD', 'RUB', 'SAR', 'SEK',
    'SGD', 'THB', 'TRY', 'TWD', 'UAH', 'USD', 'VND', 'XPF', 'ZAR',
})


class PayloadError(Exception):
    """Layout the parser cannot trust. Exit 3. Message tells the user to rerun test_hotels.py."""


class UnknownEntity(Exception):
    """No such hotel id (the errorHasStatus ds:1 block). Exit 2."""


class _RowError(Exception):
    """One seller row is not the shape we read. Counted in `unparsed_rows`, never raised out."""


# --- guarded reads (copied by hand from gflights/parse.py:87-149) ------------

def _guard(condition: bool, what: str) -> None:
    if not condition:
        raise PayloadError(
            f"unexpected payload shape: {what}. Google's response layout has "
            f"probably changed — rerun the self-check (test_hotels.py) to "
            f"confirm, and do not trust any result from this run."
        )


def _at(seq: Any, *path: int) -> Any:
    """Index a nested list, returning None instead of raising on any miss.

    Absent branches are normal in this payload (no breakdown, no histogram on a
    rental); shape violations that matter are caught by explicit _guard calls.
    """
    node = seq
    for index in path:
        if not isinstance(node, list) or len(node) <= index:
            return None
        node = node[index]
    return node


def _list_at(seq: Any, *path: int) -> list:
    """Like ``_at``, but always a list — [] for an absent branch AND for a
    non-list (e[7][4] = 7 must not be iterated; it is not a source list)."""
    value = _at(seq, *path)
    return value if isinstance(value, list) else []


def _is_int(value: Any) -> bool:
    """``bool`` is an ``int`` in Python; a stray ``True`` read as partner id 1 is
    exactly the plausible wrong answer this module exists to prevent."""
    return isinstance(value, int) and not isinstance(value, bool)


def _int_at(seq: Any, *path: int) -> int | None:
    """Like ``_at``, but never returns anything but an int (bool excluded)."""
    value = _at(seq, *path)
    return value if _is_int(value) else None


def _float_at(seq: Any, *path: int) -> float | None:
    """A JSON number (int or float, never bool) as a float, or None."""
    value = _at(seq, *path)
    if _is_int(value) or isinstance(value, float):
        return float(value)
    return None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _normalise_space(text: str | None) -> str | None:
    return text.replace(_NNBSP, " ") if isinstance(text, str) else None


# --- blocks ------------------------------------------------------------------

def blocks(html: str) -> dict[str, object]:
    """All AF_initDataCallback blocks by key, json-decoded. Same regex as gflights/parse.py::blocks.

    Divergence from gflights: a block without `sideChannel` that carries
    `errorHasStatus: true` is kept, decoded to its raw `data` list (`[5]`), so
    the unknown-entity signature can be recognised by shape (01 §H4). Any other
    undecodable block is skipped; only the block a caller needs failing is fatal.
    """
    found: dict[str, object] = {}
    for match in _BLOCK.finditer(html):
        body, key = match.group(1), match.group(2)
        data = _DATA.search(body) or _ERROR_DATA.search(body)
        if not data:
            continue
        try:
            found[key] = json.loads(data.group(1))
        except json.JSONDecodeError:
            continue
    return found


def is_error_block(data: object) -> bool:
    """The unknown-entity ds:1 shape: a list whose only element is an int (`[5]`, `[3]`)."""
    return isinstance(data, list) and len(data) == 1 and _is_int(data[0])


# --- pieces ------------------------------------------------------------------

def _price(rate: Any, currency: str, what: str) -> Price:
    """One rate array -> Price. Two-basis when [2] and [3] are numbers; single
    when [1] and [3] are null and [2] is a number (01 §b). Anything else is a
    row we do not understand."""
    if not isinstance(rate, list) or len(rate) <= RATE_EX:
        raise _RowError(f"{what}: rate array missing")
    ex = _float_at(rate, RATE_EX)
    incl = _float_at(rate, RATE_INCL)
    if ex is not None and incl is not None:
        # A swapped [2]/[3] layout would otherwise parse every row with the
        # bases exchanged and nothing would fire; this trips the 25 % guard.
        if incl < ex:
            raise _RowError(f"{what}: incl-tax {incl} below ex-tax {ex} — slot layout changed")
        return Price(ex_tax=ex, incl_tax=incl, currency=currency, basis="both")
    if ex is not None and incl is None and _at(rate, RATE_INCL_STR) is None:
        return Price(ex_tax=None, incl_tax=None, currency=currency, basis="single", amount=ex)
    raise _RowError(f"{what}: rate slots {rate[:4]!r} are neither two-basis nor single-figure")


def _free_cancellation(raw: Any) -> FreeCancellation:
    """`o[12][12][1]`: [1, "Jun 14", "6:00 PM", "6/14"], or [0] / null (not shown ≠ non-refundable)."""
    if not isinstance(raw, list) or _int_at(raw, 0) != 1:
        return FreeCancellation(shown=False, deadline_text=None, raw=raw if isinstance(raw, list) else None)
    day = _opt_str(_at(raw, 1))
    time_ = _opt_str(_at(raw, 2))
    if day and time_:
        text: str | None = f"{day} {time_}"
    elif day:
        text = f"{day} (time not shown)"
    else:
        text = None
    return FreeCancellation(shown=True, deadline_text=_normalise_space(text), raw=raw)


def _seller_row(o: Any, currency: str, hotel_name: str) -> tuple[SellerRow, int]:
    """One row -> (SellerRow, richness). Richness = non-null top-level slots, used
    to keep the fullest copy of a partner that appears in several slots."""
    if not isinstance(o, list) or len(o) <= O_RATES:
        raise _RowError("row is not a list of the expected length")
    seller = _at(o, O_SELLER, SELLER_NAME)
    if not isinstance(seller, str) or not seller:
        raise _RowError("seller name missing")
    partner_id = _int_at(o, O_SELLER, SELLER_PARTNER_ID)
    rates = _at(o, O_RATES)
    nightly = _price(_at(rates, RATES_NIGHTLY), currency, f"{seller} nightly")
    stay = _price(_at(rates, RATES_STAY), currency, f"{seller} stay")
    rooms_raw = _at(o, O_ROOMS)
    rooms = tuple(
        name for name in (_at(room, 0) for room in (rooms_raw if isinstance(rooms_raw, list) else []))
        if isinstance(name, str)
    )
    row = SellerRow(
        seller=seller,
        partner_id=partner_id,
        own_site=(seller == hotel_name),
        nightly=nightly,
        stay=stay,
        free_cancellation=_free_cancellation(_at(rates, RATES_FLAGS, FLAGS_FREE_CANCEL)),
        rooms=rooms,
    )
    richness = sum(1 for slot in o if slot is not None)
    return row, richness


def _sellers(p: list, currency: str | None, hotel_name: str) -> tuple[tuple[SellerRow, ...], int]:
    """The deduped union of the four seller slots (04 §3.2) and the unparsed count.

    `unparsed` counts SELLERS (dedupe keys) that no slot yielded a readable row
    for, and the 25 % ratio is over sellers, not slot rows (04 §5.6) — a partner
    broken in p[21] but readable in p[2] is not unparsed.
    """
    slot_rows: list[Any] = []
    for slot in SELLER_SLOTS:
        rows = _at(p, slot)
        if rows is None:
            continue               # null slot: normal (plus270 has p[21], p[22] null)
        _guard(isinstance(rows, list), f"p[{slot}] is neither null nor a list")
        slot_rows.extend(rows)
    if slot_rows:
        _guard(currency is not None,
               f"{len(slot_rows)} seller rows but no currency to label them with (p[15] and the echo are both null)")
    kept: dict[Any, tuple[SellerRow, int]] = {}
    order: list[Any] = []
    failed_keys: set[Any] = set()
    for n, o in enumerate(slot_rows):
        try:
            row, richness = _seller_row(o, currency, hotel_name)
        except (_RowError, TypeError, AttributeError, IndexError, KeyError, ValueError):
            failed_keys.add(_row_key(_int_at(o, O_SELLER, SELLER_PARTNER_ID), _at(o, O_SELLER, SELLER_NAME), n))
            continue
        key = _row_key(row.partner_id, row.seller, n)
        if key not in kept:
            order.append(key)
            kept[key] = (row, richness)
        elif richness > kept[key][1]:
            kept[key] = (row, richness)
    unparsed = len(failed_keys - kept.keys())
    total = len(kept) + unparsed
    if total and unparsed / total > MAX_UNPARSED_SHARE:
        _guard(False, f"{unparsed} of {total} sellers have no readable row")
    return tuple(kept[k][0] for k in order), unparsed


def _row_key(partner_id: int | None, seller: Any, ordinal: int) -> Any:
    """Dedupe key: partner id, else seller name, else the row itself (never merged)."""
    if partner_id is not None:
        return partner_id
    if isinstance(seller, str) and seller:
        return ("name", seller)
    return ("row", ordinal)


def _headline(p: list, sellers: tuple[SellerRow, ...], currency: str | None) -> Headline | None:
    """Google's lead rate p[1], named by float-matching slot [2] against the
    seller union (04 §6.2). Never chosen by position."""
    lead = _at(p, P_LEAD)
    if lead is None:
        return None
    _guard(isinstance(lead, list) and len(lead) > P_LEAD_FLOAT, "p[1] lead rate is not a rate array")
    lead_float = _float_at(lead, P_LEAD_FLOAT)
    _guard(lead_float is not None, "p[1][2] lead float missing")
    _guard(currency is not None, "a lead price with no currency on the page")
    matches = [
        row for row in sellers
        if (figure := row.nightly.amount if row.nightly.basis == "single" else row.nightly.ex_tax) is not None
        and abs(figure - lead_float) < _MATCH_EPS
    ]
    if not matches:
        # p[1][3] is null on every capture and p[9] carries strings only: no incl figure, no stay.
        return Headline(seller=None, partner_id=None,
                        nightly=Price(ex_tax=lead_float, incl_tax=None, currency=currency),
                        stay=None, matched_row=False)
    # Several rows can carry the same float (child-5 fixture: Booking.com and
    # Priceline both at 7139, both in p[22]). Prefer a member of p[22], Google's
    # own headline rows, in p[22]'s order — not union order, which p[21] would
    # otherwise decide; otherwise the first match in union order. The float
    # match, not the position, is still what names the seller.
    by_partner = {m.partner_id: m for m in matches if m.partner_id is not None}
    headline_ids = [_int_at(o, O_SELLER, SELLER_PARTNER_ID) for o in _list_at(p, P_HEADLINE_ROWS)]
    row = next((by_partner[i] for i in headline_ids if i in by_partner), matches[0])
    if row.nightly.basis == "single":
        nightly = row.nightly            # the lead IS a single figure (Samesun/Bluepillow)
    else:
        nightly = Price(ex_tax=lead_float, incl_tax=row.nightly.incl_tax, currency=currency)
    return Headline(seller=row.seller, partner_id=row.partner_id, nightly=nightly,
                    stay=row.stay, matched_row=True)


def _breakdown(p: list) -> Breakdown | None:
    raw = _at(p, P_BREAKDOWN)
    if raw is None:
        return None            # absent (len(p) == 43) or null: "not provided", never computed
    parts = [_float_at(raw, i) for i in range(4)]
    _guard(isinstance(raw, list) and all(v is not None for v in parts), "p[44] breakdown is not four numbers")
    base, taxes, fees, total = parts  # type: ignore[misc]
    return Breakdown(base=base, taxes=taxes, fees=fees, total=total)


def _echo(q: Any) -> Echo:
    """ds:1[0][6][1] -> Echo. A malformed echo yields None fields, which
    Echo.matches() reports as a mismatch (exit 3) — never as a match."""
    dates = _at(q, ECHO_DATES)

    def _day(i: int) -> date | None:
        y, m, d = (_int_at(dates, i, k) for k in range(3))
        if y is None or m is None or d is None:
            return None
        try:
            return date(y, m, d)
        except ValueError:
            return None

    occupancy = _at(q, ECHO_OCCUPANCY)
    adults: int | None = None
    child_ages: tuple[int, ...] | None = None
    if isinstance(occupancy, list):
        adults = _int_at(occupancy, 0)
        ages_raw = _at(occupancy, 1)
        if ages_raw is None:
            child_ages = ()                      # [2, null, 0]: no children
        elif isinstance(ages_raw, list):
            ages = [_int_at(entry, 0) for entry in ages_raw]
            # any entry that is not [int] is a shape we do not read: None, never
            # "zero children" — Echo.matches() then reports a mismatch
            child_ages = tuple(ages) if all(a is not None for a in ages) else None  # type: ignore[arg-type]
        else:
            child_ages = None
    return Echo(
        checkin=_day(0), checkout=_day(1), nights=_int_at(dates, 2),
        adults=adults, child_ages=child_ages, currency=_opt_str(_at(q, ECHO_CURRENCY)),
    )


def _rating(r: Any) -> Rating:
    histogram: list[tuple[int, int, int]] = []
    for h in _list_at(r, REVIEWS_HISTOGRAM, 0):
        stars, pct, count = (_int_at(h, i) for i in range(3))
        if stars is not None and pct is not None and count is not None:
            histogram.append((stars, pct, count))
    sources: list[RatingSource] = []
    for s in _list_at(r, REVIEWS_SOURCES):
        name = _opt_str(_at(s, SOURCE_NAME))
        if name is None:
            continue
        sources.append(RatingSource(
            name=name, score=_float_at(s, SOURCE_SCORE, 0), scale=_float_at(s, SOURCE_SCORE, 1),
            count=_int_at(s, SOURCE_COUNT),
        ))
    return Rating(
        score=_float_at(r, REVIEWS_SUMMARY, 0), reviews=_int_at(r, REVIEWS_SUMMARY, 1),
        histogram=tuple(histogram), sources=tuple(sources),
    )


def _is_item(node: Any) -> bool:
    """An amenity item is `[has, id, qualifier?, …]`: [0] is 0/1 and [1] an int.
    A group is `[group id, [items]]` — its [1] is a list, so the two never collide."""
    return (isinstance(node, list) and len(node) > ITEM_ID
            and _is_int(node[ITEM_HAS]) and node[ITEM_HAS] in (0, 1) and _is_int(node[ITEM_ID]))


def _walk_items(node: Any, group: int | None, out: list[tuple[list, int | None]]) -> None:
    """Every item reachable recursively under `node`, tagged with the nearest
    enclosing `[group id, [...]]` (04 §3.2.1: [2]/[3] nest one level deeper than [0])."""
    if not isinstance(node, list):
        return
    if _is_item(node):
        out.append((node, group))
        return
    if len(node) >= 2 and _is_int(node[0]) and isinstance(node[1], list):
        group = node[0]
    for child in node:
        _walk_items(child, group, out)


def _qualifier(item: list) -> tuple[str | None, int | None]:
    raw = _int_at(item, ITEM_QUALIFIER)
    return (QUALIFIERS.get(raw) if raw is not None else None), raw


def _highlights(grouped: Any) -> tuple[Highlight, ...] | None:
    chips = _at(grouped, AMEN_CHIPS)
    if chips is None:
        return None            # rentals: chips null
    _guard(isinstance(chips, list), "e[10][6][1] chips is neither null nor a list")
    out: list[Highlight] = []
    for chip in chips:
        _guard(_is_item(chip), f"highlight chip {chip!r} is not [has, id, qualifier?]")
        qualifier, raw = _qualifier(chip)
        out.append(Highlight(id=chip[ITEM_ID], name=HIGHLIGHT_NAMES.get(chip[ITEM_ID]),
                             qualifier=qualifier, qualifier_raw=raw))
    return tuple(out)


def _hotel_amenities(grouped: Any) -> tuple[Amenity, ...]:
    """The walk of 04 §3.2.1: every item under e[10][6] except slot [1]; one entry per id."""
    if not isinstance(grouped, list):
        return ()
    found: list[tuple[list, int | None]] = []
    for i, slot in enumerate(grouped):
        if i != AMEN_CHIPS:
            _walk_items(slot, None, found)
    seen: set[int] = set()
    out: list[Amenity] = []
    for item, group in found:
        aid = item[ITEM_ID]
        if aid in seen:
            continue
        seen.add(aid)
        has = item[ITEM_HAS] == 1
        qualifier, raw = _qualifier(item)
        out.append(Amenity(
            id=aid, name=AMENITY_NAMES.get(aid), has=has, qualifier=qualifier, qualifier_raw=raw,
            negated_label=None if has else AMENITY_NEGATED.get(aid),
            group=AMENITY_GROUPS.get(group) if group is not None else None,
        ))
    return tuple(out)


def _rental_amenities(named: Any) -> tuple[Amenity, ...]:
    """Rentals: e[10][1] = ["Amenities", [[name, has, icon, …], …]] — names, no ids, no group."""
    out: list[Amenity] = []
    for row in _list_at(named, 1):
        name = _opt_str(_at(row, RENTAL_ROW_NAME))
        has = _int_at(row, RENTAL_ROW_HAS)
        if name is None or has is None:
            continue
        out.append(Amenity(id=None, name=name, has=(has == 1), group=None))
    return tuple(out)


def _rental(e: list) -> Rental | None:
    facts = _at(e, E_RENTAL, RENTAL_FACTS)
    if facts is None:
        return None
    return Rental(sleeps=_int_at(facts, 1), bedrooms=_int_at(facts, 2),
                  bathrooms=_int_at(facts, 3), beds=_int_at(facts, 4))


def _ids(e: list) -> HotelIds:
    ftid = _opt_str(_at(e, E_FTID))
    cid: int | None = None
    if ftid is not None:
        halves = ftid.split(":")
        _guard(len(halves) == 2 and halves[1].startswith("0x"), f"e[9] ftid {ftid!r} is not 0x…:0x…")
        cid = int(halves[1], 16)
    token = _at(e, E_TOKEN)
    _guard(isinstance(token, str) and bool(token), "e[20] entity token missing")
    kind = _int_at(e, E_KIND)
    _guard(kind in (1, 2), f"e[14] kind {kind!r} is not 1 (hotel) or 2 (rental)")
    return HotelIds(ftid=ftid, place_id=_opt_str(_at(e, E_META, META_PLACE_ID)), cid=cid, token=token, kind=kind)


# --- the record --------------------------------------------------------------

def entity_record(html: str) -> EntityRecord:
    """Parse one entity page. Raises UnknownEntity or PayloadError; never returns None.

    Any other exception escaping the parse is a layout drift the guards did not
    anticipate; it becomes a PayloadError so one drifted hotel fails as exit 3
    rather than aborting a whole shortlist with a traceback.
    """
    try:
        return _entity_record(html)
    except (PayloadError, UnknownEntity):
        raise
    except Exception as exc:  # noqa: BLE001 — deliberate: shape drift, not a bug to hide
        _guard(False, f"{type(exc).__name__} while reading the record ({exc})")
        raise AssertionError("unreachable")  # _guard always raises


def _entity_record(html: str) -> EntityRecord:
    if RESULTS_MARKER not in html:
        raise PayloadError(
            "not a Google Hotels page (the ds:2 block is absent) — www.google.com "
            "served something else: a block, a consent wall, or a redirect target."
        )
    found = blocks(html)
    _guard(RECORD_KEY in found, "the ds:1 record block is absent from a hotels page")
    ds1 = found[RECORD_KEY]
    if is_error_block(ds1):
        raise UnknownEntity(
            f"no such hotel id — Google returned error status {ds1[0]} for this entity token"
        )
    e = _at(ds1, 0)
    _guard(isinstance(e, list) and len(e) >= E_MIN_LEN, "ds:1[0] is not the hotel record")
    name = _at(e, E_NAME)
    _guard(isinstance(name, str) and bool(name), "e[1] hotel name is not a string")
    ids = _ids(e)
    prices = _at(e, E_PRICES)
    _guard(isinstance(prices, list) and len(prices) > PRICES_TABLE, "e[6] price block missing")
    q, p = _at(prices, PRICES_ECHO), _at(prices, PRICES_TABLE)
    _guard(isinstance(q, list), "e[6][1] query echo missing")
    _guard(isinstance(p, list) and len(p) >= P_MIN_LEN, "e[6][2] price table missing or short")
    echo = _echo(q)
    page_currency = _opt_str(_at(p, P_CURRENCY))
    price_currency = page_currency or echo.currency

    sellers, unparsed = _sellers(p, price_currency, name)
    headline = _headline(p, sellers, price_currency)

    star_raw = _at(e, E_STAR)
    star_class: tuple[str, int] | None = None
    if star_raw is not None:
        label, stars = _opt_str(_at(star_raw, 0)), _int_at(star_raw, 1)
        _guard(label is not None and stars is not None, "e[3] star class is not [label, int]")
        star_class = (label, stars)

    grouped = _at(e, E_AMENITIES, AMEN_GROUPED)
    if ids.kind == 2:
        highlights: tuple[Highlight, ...] | None = None
        amenities = _rental_amenities(_at(e, E_AMENITIES, AMEN_RENTAL_ROWS))
    else:
        highlights = _highlights(grouped)
        amenities = _hotel_amenities(grouped)

    place = _at(e, E_PLACE)
    return EntityRecord(
        name=name,
        kind=ids.kind,
        ids=ids,
        star_class=star_class,
        address=_opt_str(_at(place, PLACE_ADDRESS, 0, 0, 0)),
        phone=_opt_str(_at(place, PLACE_PHONE, 0)),
        website=_opt_str(_at(place, PLACE_WEBSITE, WEBSITE_URL)),
        checkin_time=_normalise_space(_opt_str(_at(place, PLACE_TIMES, 0))),
        checkout_time=_normalise_space(_opt_str(_at(place, PLACE_TIMES, 1))),
        lat=_float_at(place, PLACE_LATLNG, 0),
        lng=_float_at(place, PLACE_LATLNG, 1),
        echo=echo,
        currency=page_currency,
        headline=headline,
        sellers=sellers,
        breakdown=_breakdown(p),
        rating=_rating(_at(e, E_REVIEWS)),
        highlights=highlights,
        amenities=amenities,
        amenities_unnamed=sum(1 for a in amenities if a.name is None),
        rental=_rental(e),
        unparsed_rows=unparsed,
    )
