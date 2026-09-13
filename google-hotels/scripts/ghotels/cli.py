"""argparse, the six commands, human rendering, exit codes, JSON envelope (04 §3, §4, §6).

Lane D owns this file. Exit codes 0/1/2/3 identical under --json; main() has a
catch-all that returns 3, never 1, never a traceback.

Exit codes (04 §4):
    0  found what was asked for
    1  the query worked and there is nothing — the ONLY "keep waiting" code
    2  usage / lookup / budget: every future run fails the same way
    3  transport, block, shape, or a page that priced a different stay

Helpers `_date`, `_budget`, `_bounded`, `_currency`, `_country`, `_emit`,
`_fail`, `_Parser` and the shape of `main()` are copied by hand from
google-flights/scripts/gflights/cli.py (lines 58-130, 184-201, 802-893); the
display-width helpers from enterprise-rentals/scripts/enterprise/cli.py:69-160.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from . import ids as idmod
from . import client as _client_mod
from .client import (
    MAX_SWEEP_DAYS, Client, EchoMismatch, QueryError, cheapest_row, validate_stay,
)


def _today() -> date:
    """The package clock (client.today), resolved at call time so a test's
    monkeypatch of `ghotels.client.today` reaches every date in the CLI."""
    return _client_mod.today()
from .http import (
    THROTTLE_SECONDS, HotelsHTTPError, RequestBudgetError, Transport, looks_blocked,
)
from .ids import IdError
from .model import (
    KIND_HOTEL, KIND_RENTAL, Candidate, EntityRecord, HotelIds, Price,
    SellerRow, Stay, cheapest_seller, comparable,
)
from .parse import AMENITY_NAMES, HIGHLIGHT_NAMES, PayloadError, UnknownEntity, entity_record

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3
SCHEMA_VERSION = 1

#: Spare requests added to a sweep's auto-budget so one transient failure does
#: not end the run (flights cli.py:36).
RETRY_HEADROOM = 3
DEFAULT_MAX_REQUESTS = {"resolve": 0, "quote": 2, "watch": 2, "shortlist": 11, "doctor": 1}
MAX_MAX_REQUESTS = 40

DEFAULT_LIMIT, MAX_LIMIT = 6, 10
DEFAULT_DAYS, MAX_DAYS = 14, MAX_SWEEP_DAYS
MAX_NIGHTS_FLAG = 30

#: Above this share of the stay total, the fee amount goes in the first line (05).
FEES_SHARE_ALERT = 0.10

#: `doctor` prices one fixed, known hotel: the Samesun Banff hostel from the
#: probe (evidence/requests.log #29/#37, token CgoIlM69nM_7pJ9XEAE).
DOCTOR_CID = 6286624707044140820
DOCTOR_TOKEN = "CgoIlM69nM_7pJ9XEAE"
DOCTOR_DAYS_AHEAD = 45

#: A bare-name refusal (04 §3.1, D4) always quotes `ids.gmaps_command(name)` —
#: the one copy of the google-maps syntax, so the two cannot drift.

SINGLE_LABEL = "single figure, treated as all-in"
TIE_LABEL = "≈ same price, basis not stated"


def _fix_console() -> None:
    """Windows consoles default to cp1252 and raise on accented hotel names."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


#: Bound on a relative offset (flights cli.py:55). The real horizon (330 days)
#: is refused by validate_stay with a proper message; this only stops an absurd
#: value raising OverflowError out of timedelta before main() can catch it.
MAX_OFFSET_DAYS = 10_000


def _date(value: str) -> date:
    """A date, or a relative offset like `+14` meaning fourteen days from now."""
    if value.startswith("+") and value[1:].isdigit():
        offset = int(value[1:])
        if offset > MAX_OFFSET_DAYS:
            raise argparse.ArgumentTypeError(
                f"+{offset} is not a plausible number of days from today "
                f"(maximum +{MAX_OFFSET_DAYS})"
            )
        return _today() + timedelta(days=offset)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date (expected YYYY-MM-DD, or +N days from today)"
        )


def _budget(value: str) -> int:
    """A request ceiling, capped. Google blocks rather than throttles, and on a
    shared egress IP an over-eager sweep degrades the address for everyone."""
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not 1 <= count <= MAX_MAX_REQUESTS:
        raise argparse.ArgumentTypeError(
            f"--max-requests must be between 1 and {MAX_MAX_REQUESTS}"
        )
    return count


def _bounded(name: str, low: int, high: int | None = None):
    """An int flag with real bounds. Zero is a usage error, never "no filter"."""
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} expects a number, got {value!r}")
        if number < low or (high is not None and number > high):
            limit = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"{name} must be {limit} (got {number})")
        return number
    return parse


def _positive(name: str, high: float | None = None):
    """A price or rating: a number strictly above zero."""
    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} expects a number, got {value!r}")
        if not math.isfinite(number) or number <= 0 or (high is not None and number > high):
            top = f" and at most {high:g}" if high is not None else ""
            raise argparse.ArgumentTypeError(f"{name} must be above 0{top} (got {value})")
        return number
    return parse


def _currency(value: str) -> str:
    if not (len(value) == 3 and value.isalpha()):
        raise argparse.ArgumentTypeError(
            f"--currency expects a 3-letter ISO code like CAD or USD, got {value!r}"
        )
    return value.upper()


def _country(value: str) -> str:
    if not (len(value) == 2 and value.isalpha()):
        raise argparse.ArgumentTypeError(
            f"--country expects a 2-letter country code like CA or US, got {value!r}"
        )
    return value.upper()


def _amenity(value: str) -> tuple[str, bool]:
    """`NAME` or `NAME:free` -> (name, must_be_free)."""
    name, sep, term = value.partition(":")
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("--amenity needs a name, e.g. wifi or parking:free")
    if sep and term.strip().lower() != "free":
        raise argparse.ArgumentTypeError(
            f"--amenity {value!r}: the only qualifier is ':free'"
        )
    return name, bool(sep)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _width(text: str) -> int:
    """Display width, counting CJK and other wide glyphs as two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return fill + text if right else text + fill


def _clip(text: str, width: int) -> str:
    """Shorten to `width` display columns at a word boundary, ending in an ellipsis."""
    if _width(text) <= width:
        return text
    cut = text
    while cut and _width(cut) + 1 > width:
        cut = cut[:-1]
    if cut and len(cut) < len(text) and text[len(cut)] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,-") + "…"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()) -> str:
    if not rows:
        return ""
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(str(cell)))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            _pad(str(cell), widths[i], right=i in right) for i, cell in enumerate(cells)
        ).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _price_text(price: Price | None, basis: str = "incl") -> str:
    """"1,452.38 incl. tax (1,282.75 before tax)" / "1,452.81 (single figure, treated as all-in)".

    The basis is always on the same line as the number, and a single-figure
    row is never called "incl. tax" (04 §6.2).
    """
    if price is None:
        return "—"
    if price.basis == "single":
        return f"{_money(price.amount)} ({SINGLE_LABEL})"
    if price.incl_tax is None:
        # a headline whose row was not float-matched carries only p[1][2] (lead ruling a)
        return f"{_money(price.ex_tax)} before tax (incl-tax figure not shown)"
    if basis == "ex":
        return f"{_money(price.ex_tax)} before tax ({_money(price.incl_tax)} incl. tax)"
    return f"{_money(price.incl_tax)} incl. tax ({_money(price.ex_tax)} before tax)"


def _price_cell(price: Price | None) -> str:
    """The compact table form. Never clipped, so the basis suffix survives."""
    if price is None:
        return "—"
    if price.basis == "single":
        return f"{_money(price.amount)} single figure"
    if price.incl_tax is None:
        return f"{_money(price.ex_tax)} before tax"
    return f"{_money(price.incl_tax)} incl. tax ({_money(price.ex_tax)} ex)"


def _party_text(adults: int, child_ages: Sequence[int]) -> str:
    text = f"{adults} adult" + ("" if adults == 1 else "s")
    if child_ages:
        ages = [str(a) for a in child_ages]
        if len(ages) == 1:
            text += f" + child aged {ages[0]}"
        else:
            text += f" + children aged {', '.join(ages[:-1])} and {ages[-1]}"
    return text


def _cancel_text(row: SellerRow) -> str:
    fc = row.free_cancellation
    if fc.shown:
        return f"free until {fc.deadline_text}" if fc.deadline_text else "free cancellation shown"
    return "no deadline shown"


def _seller_label(row: SellerRow) -> str:
    return f"{row.seller} (the hotel's own site)" if row.own_site else row.seller


def _header(record: EntityRecord | None, name: str | None, stay: Stay, currency: str,
            priced: bool | None = None) -> None:
    """Hotel, stay AS ECHOED, party, currency — on every priced command (04 §2).
    `priced` says whether any page confirmed the stay (shortlist has no single
    record but its rows are all echo-checked); default: a record was given."""
    echo = record.echo if record else None
    if priced is None:
        priced = record is not None
    checkin = (echo.checkin if echo and echo.checkin else stay.checkin).isoformat()
    checkout = (echo.checkout if echo and echo.checkout else stay.checkout).isoformat()
    adults = echo.adults if echo and echo.adults is not None else stay.adults
    ages = echo.child_ages if echo and echo.child_ages is not None else stay.child_ages
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    label = (record.name if record else name) or "(hotel)"
    kind = ""
    if record:
        kind = "vacation rental" if record.kind == KIND_RENTAL else "hotel"
        if record.star_class:
            kind = record.star_class[0]  # "5-star hotel", Google's own label
        kind = f" ({kind})"
    print(
        f"{label}{kind} — {checkin} → {checkout} ({nights} night{'s' if nights != 1 else ''}, "
        f"{'as Google priced it' if priced else 'as requested'}) — for {_party_text(adults, ages)} — prices in {currency}"
    )


def _footer(requests_used: int, single_rows: bool = False) -> None:
    print()
    if single_rows:
        print(f"  single figure = one figure shown, basis not stated — treated as all-in")
    print(
        f"requests used: {requests_used} · prices are what sellers listed at fetch "
        f"time, not availability · this tool cannot book"
    )


# ---------------------------------------------------------------------------
# Output envelope
# ---------------------------------------------------------------------------


def _emit(args, payload: dict, code: int) -> int:
    if args.json:
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": True, **payload},
            indent=2, ensure_ascii=False,
        ))
    return code


def _fail(args, code: int, message: str, extra: dict | None = None) -> int:
    message = " ".join(str(message).split())  # always one line (04 §2)
    if getattr(args, "json", False):
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": False, "error": message, **(extra or {})},
            indent=2, ensure_ascii=False,
        ))
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# Inputs: ids, stays, candidates
# ---------------------------------------------------------------------------


def _input_kind(text: str) -> str:
    text = text.strip()          # parse_hotel_id strips too; classify what it sees
    if re.fullmatch(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", text):
        return "ftid"
    if text.startswith("ChIJ"):
        return "place_id"
    if text.isdigit():
        return "cid"
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", text):
        return "token"
    return "name"


def _parse_id(text: str) -> HotelIds:
    """An id in any form; a bare name is refused with the gmaps command (04 §3.1)."""
    try:
        return idmod.parse_hotel_id(text)
    except IdError as e:
        message = str(e)
        if "gmaps.py" not in message:  # ids.py normally carries the command itself
            message = (
                f"{message} — this skill prices hotels by id; find it with google-maps "
                f"first: {idmod.gmaps_command(text)}, then pass --ids-from hotel.json"
            )
        raise QueryError(message) from None


def _ids_from_token(token: str) -> HotelIds:
    try:
        cid, kind = idmod.decode_token(token)
    except (IdError, ValueError) as e:
        raise QueryError(f"--token {token!r} is not an entity token ({e})") from None
    return HotelIds(ftid=None, place_id=None, cid=cid, token=token, kind=kind)


def _read_file(path: str) -> tuple[list[Candidate], list[str], object]:
    try:
        return idmod.read_gmaps_file(path)
    except IdError as e:
        raise QueryError(str(e)) from None
    except OSError as e:
        raise QueryError(f"cannot read {path}: {e.strerror or e}") from None


def _hotel_from_args(args) -> tuple[HotelIds, str | None, dict]:
    """The one hotel a single-hotel command prices: HOTEL, --ids-from or --token."""
    given = [
        flag for flag, value in (
            ("HOTEL", getattr(args, "hotel", None)),
            ("--ids-from", getattr(args, "ids_from", None)),
            ("--token", getattr(args, "token", None)),
        ) if value
    ]
    if len(given) != 1:
        raise QueryError(
            "give exactly one hotel: an id (ftid, place_id or CID), --ids-from FILE, "
            "or --token TOK"
            + (f" (got {', '.join(given)})" if given else "")
        )
    if getattr(args, "token", None):
        return _ids_from_token(args.token), None, {"input": args.token, "input_kind": "token"}
    if getattr(args, "ids_from", None):
        cands, skipped, _ = _read_file(args.ids_from)
        if not cands:
            raise QueryError(
                f"{args.ids_from} carries no hotel ids ({len(skipped)} entries without "
                f"ftid/place_id) — re-run gmaps.py search with --full"
            )
        if len(cands) > 1:
            # never guess which building (04 §3.1): a single-hotel command needs a single id
            listed = "; ".join(f"{c.name or '(unnamed)'} = {c.ids.ftid or c.ids.cid}" for c in cands[:10])
            more = f"; … {len(cands) - 10} more" if len(cands) > 10 else ""
            raise QueryError(
                f"{args.ids_from} lists {len(cands)} hotels — pass the ftid of the one you "
                f"mean, or re-run gmaps with --limit 1 (shortlist --ids-from prices them "
                f"all): {listed}{more}"
            )
        note = {"input": args.ids_from, "input_kind": "ids-from", "file_candidates": 1}
        return cands[0].ids, cands[0].name, note
    return _parse_id(args.hotel), None, {"input": args.hotel, "input_kind": _input_kind(args.hotel)}


def _stay(args) -> Stay:
    return validate_stay(args.checkin, args.checkout, args.adults, args.child_age or [],
                         args.currency)


def _client(args, command: str) -> Client:
    budget = args.max_requests if args.max_requests is not None else DEFAULT_MAX_REQUESTS[command]
    return Client(Transport(max_requests=budget, throttle=THROTTLE_SECONDS), gl=args.country)


def _hotel_dict(ids: HotelIds, name: str | None) -> dict:
    return {"name": name, **ids.to_dict()}


def _query(ids: HotelIds, name: str | None, stay: Stay, record: EntityRecord | None,
           note: dict | None = None) -> dict:
    q = {
        "hotel": _hotel_dict(record.ids if record else ids, record.name if record else name),
        "requested": stay.to_dict(),
        "echoed": record.echo.to_dict() if record else None,
        "echo_matched": bool(record and record.echo.matches(stay)),
    }
    if note:
        q.update({k: v for k, v in note.items() if k != "input"})
        q["input"] = note.get("input")
    return q


def _stay_dict(stay: Stay) -> dict:
    return {
        "checkin": stay.checkin.isoformat(),
        "checkout": stay.checkout.isoformat(),
        "nights": stay.nights,
        "days_ahead": (stay.checkin - _today()).days,
    }


def _occupancy_dict(stay: Stay) -> dict:
    return {"adults": stay.adults, "child_ages": list(stay.child_ages), "rooms": 1}


def _cheapest_dict(row: SellerRow | None) -> dict | None:
    if row is None:
        return None
    return {
        "seller": row.seller,
        "partner_id": row.partner_id,
        "own_site": row.own_site,
        "basis": row.basis,
        "nightly": row.nightly.to_dict(),
        "stay": row.stay.to_dict(),
        "free_cancellation": row.free_cancellation.to_dict(),
    }


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, EchoMismatch):
        return "priced a different stay"
    if isinstance(exc, UnknownEntity):
        return f"no such hotel id ({' '.join(str(exc).split())})"
    if isinstance(exc, (HotelsHTTPError, PayloadError)):
        return " ".join(str(exc).split())
    # anything else is a bug surfacing on one item: named, counted, never hidden
    return f"unexpected {type(exc).__name__}: {' '.join(str(exc).split())}"


# ---------------------------------------------------------------------------
# Amenities (04 §3.2.1)
# ---------------------------------------------------------------------------


def _norm(name: str) -> str:
    """Matching rule for --amenity: case-insensitive, with spaces, hyphens and
    punctuation folded away, so `wifi`, `wi-fi` and `Wi Fi` all name "Wi-Fi"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


#: Every name --amenity can ask for: the provisional table plus the chip names.
KNOWN_AMENITIES: dict[str, str] = {
    _norm(n): n for n in (*AMENITY_NAMES.values(), *HIGHLIGHT_NAMES.values())
}


def _check_amenity_names(wanted: list[tuple[str, bool]]) -> None:
    """An amenity the table cannot name would silently count every hotel as
    not_listed, so it is a usage error naming the known names instead."""
    unknown = [n for n, _ in wanted if _norm(n) not in KNOWN_AMENITIES]
    if unknown:
        known = ", ".join(sorted(set(KNOWN_AMENITIES.values()), key=str.lower))
        raise QueryError(
            f"--amenity {', '.join(repr(n) for n in unknown)}: not a name Google's "
            f"amenity table knows. Known names: {known}"
        )


def _amenity_state(record: EntityRecord, name: str, want_free: bool) -> str:
    """has / has_other_terms / lacks / not_listed — and not_listed is never "no"."""
    wanted = _norm(name)
    matches = [a for a in record.amenities if a.name and _norm(a.name) == wanted]
    if any(a.has and (not want_free or a.qualifier == "free") for a in matches):
        return "has"
    if any(a.has for a in matches):
        return "has_other_terms"
    if any(not a.has for a in matches):
        return "lacks"
    # the four chips are `has` by construction; consulted only when the grouped lists are silent
    chips = [h for h in (record.highlights or ()) if h.name and _norm(h.name) == wanted]
    if any(not want_free or h.qualifier == "free" for h in chips):
        return "has"
    if chips:
        return "has_other_terms"
    return "not_listed"


def _amenity_label(name: str, want_free: bool) -> str:
    return f"free {name}" if want_free else name


def _amenity_counts_line(label: str, checked: int, counts: dict) -> str:
    """"6 checked: 3 have free Wi-Fi, 1 has it on other terms, 1 is listed as not having it, 1 does not list it." """
    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"
    return (
        f"{checked} checked: {n(counts['has'], 'has', 'have')} {label}, "
        f"{n(counts['has_other_terms'], 'has', 'have')} it on other terms, "
        f"{n(counts['lacks'], 'is', 'are')} listed as not having it, "
        f"{n(counts['not_listed'], 'does', 'do')} not list it."
    )


def _print_amenities(record: EntityRecord) -> None:
    if record.highlights is not None:
        chips = []
        for h in record.highlights:
            label = h.name or f"amenity #{h.id}"
            if h.qualifier:
                label += f" ({h.qualifier.replace('_', ' ')})"
            elif h.qualifier_raw is not None:
                label += f" (qualifier {h.qualifier_raw})"
            chips.append(label)
        if chips:
            print(f"Highlights: {', '.join(chips)}")
    if not record.amenities:
        return
    groups: dict[str, list] = {}
    for a in record.amenities:
        groups.setdefault(a.group or ("Amenities" if record.kind == KIND_RENTAL else "Other"), []).append(a)
    print("Amenities (as Google lists them; an amenity not listed is unknown, not absent):")
    for group, items in groups.items():
        named_has, lacks, unnamed = [], [], 0
        for a in items:
            if a.name is None:
                unnamed += 1
            elif a.has:
                label = a.name
                if a.qualifier:
                    label += f" ({a.qualifier.replace('_', ' ')})"
                elif a.qualifier_raw is not None:
                    label += f" (qualifier {a.qualifier_raw})"
                named_has.append(label)
            else:
                lacks.append(a.negated_label or f"listed as not having: {a.name}")
        parts = []
        if named_has:
            parts.append(", ".join(named_has))
        parts.extend(lacks)
        if unnamed:
            parts.append(f"+{unnamed} unnamed")
        print(f"  {group}: {'; '.join(parts)}")


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------


def _quote_payload(record: EntityRecord, stay: Stay) -> dict:
    best, tie = cheapest_seller(record.sellers, "incl")
    near_tie = None
    if tie:
        single, _ = cheapest_seller(tuple(s for s in record.sellers if s.basis == "single"), "incl")
        near_tie = _cheapest_dict(single)
    base = record.to_dict()
    return {
        **base,
        "stay": _stay_dict(stay),
        "occupancy": _occupancy_dict(stay),
        "cheapest": _cheapest_dict(best),
        "near_tie": near_tie,
    }


def _print_quote(record: EntityRecord, stay: Stay, requests_used: int) -> None:
    _header(record, None, stay, stay.currency)
    print()
    best, tie = cheapest_seller(record.sellers, "incl")
    rental = record.kind == KIND_RENTAL
    if best is None:
        print("No rates listed for these nights — no seller shows a price on Google's page.")
        print("(That is not availability: the page says nothing about it, and a rate may appear later.)")
    elif rental:
        nightly = comparable(best.nightly, "incl")
        print(
            f"Cheapest: {_seller_label(best)} — stay {_price_text(best.stay)} for "
            f"{stay.nights} night{'s' if stay.nights != 1 else ''}; the nightly figure "
            f"({_money(nightly)}) is not the price of a rental — fees are in the stay total"
        )
    else:
        print(
            f"Cheapest: {_seller_label(best)} — {_price_text(best.nightly)} per night; "
            f"stay {_price_text(best.stay)}"
        )
    if tie:
        single, _ = cheapest_seller(tuple(s for s in record.sellers if s.basis == "single"), "incl")
        if single is not None:
            print(f"  {TIE_LABEL}: {single.seller} at {_money(single.nightly.amount)}/night (single figure)")
    head = record.headline
    if head is not None:
        via = f"via {head.seller}" if head.seller else "seller not identified"
        same = " (the cheapest row)" if best is not None and head.seller == best.seller and head.seller else ""
        print(f"Google's headline: {_price_text(head.nightly)} per night {via}{same}")
    elif best is not None:
        print("Google's headline: none shown on the page (sellers still list the stay)")
    bd = record.breakdown
    if bd is not None:
        print(
            f"Stay total (Google's own breakdown): base {_money(bd.base)} + taxes "
            f"{_money(bd.taxes)} + fees {_money(bd.fees)} = {_money(bd.total)} {stay.currency}"
        )
        share = bd.fees_share
        if share is not None and share > FEES_SHARE_ALERT:
            print(f"  Fees are {share:.0%} of the total: {_money(bd.fees)} of {_money(bd.total)}")
    else:
        print("Stay total: not provided by Google for this stay")

    if record.sellers:
        print()
        rows = sorted(
            record.sellers,
            key=lambda s: (comparable(s.nightly, "incl") is None, comparable(s.nightly, "incl") or 0),
        )
        table = []
        for s in rows:
            label = _clip(_seller_label(s), 34)
            note = ""
            if tie and s.basis == "single" and best is not None:
                mine = comparable(s.nightly, "incl")
                if mine is not None and mine < (comparable(best.nightly, "incl") or 0):
                    note = f" {TIE_LABEL}"
            table.append((
                label,
                _price_cell(s.nightly) + note,
                _price_cell(s.stay),
                _cancel_text(s),
                _clip("; ".join(s.rooms), 40) if s.rooms else "—",
            ))
        print(_table(("SELLER", "PER NIGHT", "STAY", "CANCELLATION", "ROOMS"), table))
        if record.unparsed_rows:
            print(f"({record.unparsed_rows} seller row(s) could not be read and are missing above.)")

    print()
    r = record.rating
    if r.score is not None:
        reviews = f" from {r.reviews:,} reviews" if r.reviews is not None else ""
        print(f"Rating: {r.score}{reviews}")
        if r.sources:
            print("  sources: " + "; ".join(
                f"{s.name} {round(s.score, 1):g}/{s.scale:g}" + (f" ({s.count:,})" if s.count else "")
                for s in r.sources if s.score is not None and s.scale
            ))
    _print_amenities(record)
    if record.rental:
        rn = record.rental
        facts = [f"sleeps {rn.sleeps}" if rn.sleeps else None,
                 f"{rn.bedrooms} bedroom(s)" if rn.bedrooms is not None else None,
                 f"{rn.bathrooms} bathroom(s)" if rn.bathrooms is not None else None,
                 f"{rn.beds} bed(s)" if rn.beds is not None else None]
        print("Rental: " + ", ".join(f for f in facts if f))
    facts = [record.address, record.phone, record.website]
    if record.checkin_time or record.checkout_time:
        facts.append(f"check-in {record.checkin_time or '?'}, check-out {record.checkout_time or '?'}")
    line = " · ".join(f for f in facts if f)
    if line:
        print(line)
    _footer(requests_used, single_rows=any(s.basis == "single" for s in record.sellers))


def cmd_quote(args) -> int:
    """"What does this hotel cost for these nights, from whom, and is that with tax?" """
    stay = _stay(args)
    ids, name, note = _hotel_from_args(args)
    client = _client(args, "quote")
    client.transport.plan(1, "a quote")
    record = client.quote(ids, stay)
    used = client.transport.requests_made
    if not args.json:
        _print_quote(record, stay, used)
    found = bool(record.sellers)
    return _emit(
        args,
        {
            "command": "quote",
            "query": _query(ids, name, stay, record, note),
            **_quote_payload(record, stay),
            "requests_used": used,
        },
        EXIT_OK if found else EXIT_NONE,
    )


# ---------------------------------------------------------------------------
# resolve — offline
# ---------------------------------------------------------------------------


def cmd_resolve(args) -> int:
    """"What are this hotel's ids, in every form?" Zero requests."""
    if bool(args.hotel) == bool(args.ids_from):
        raise QueryError("give an id (ftid, place_id, CID or token) or --ids-from FILE, not both")
    hotels: list[Candidate] = []
    skipped: list[str] = []
    files = list(args.ids_from or [])
    if args.hotel:
        hotels.append(Candidate(name=None, ids=_parse_id(args.hotel)))
        query = {"input": args.hotel, "input_kind": _input_kind(args.hotel), "files": []}
    else:
        for path in files:
            cands, missing, _ = _read_file(path)
            hotels.extend(cands)
            skipped.extend(missing)
        query = {"input": None, "input_kind": "ids-from", "files": files}
    if not args.json:
        for c in hotels:
            print(c.name or "(name not in input)")
            print(f"  ftid      {c.ids.ftid or '—'}")
            print(f"  place_id  {c.ids.place_id or '—'}")
            print(f"  cid       {c.ids.cid if c.ids.cid is not None else '—'}")
            print(f"  token     {c.ids.token}  (kind {c.ids.kind}: "
                  f"{'vacation rental' if c.ids.kind == KIND_RENTAL else 'hotel'})")
            # no review count: gmaps' --full payload carries none (Candidate.reviews is always None)
            extras = [f"{c.lat}, {c.lng}" if c.lat is not None else None,
                      f"rating {c.rating}" if c.rating is not None else None, c.address]
            if any(extras):
                print("  " + " · ".join(e for e in extras if e))
        if skipped and hotels:
            print(f"skipped {len(skipped)} entries without ftid/place_id (re-run gmaps.py "
                  f"search with --full): {', '.join(skipped)}")
        if hotels:
            print("requests used: 0 (offline)")
    if not hotels:
        # exit 2 is always the failure object, never ok:true with an empty list
        names = ", ".join(skipped[:10]) + (f", … {len(skipped) - 10} more" if len(skipped) > 10 else "")
        return _fail(
            args, EXIT_USAGE,
            f"{', '.join(files)}: no entry carries an id ({len(skipped)} skipped: {names}) — "
            f"re-run gmaps.py search with --full",
        )
    return _emit(
        args,
        {
            "command": "resolve",
            "query": query,
            "hotels": [c.to_dict() for c in hotels],
            "skipped": skipped,
            "requests_used": 0,
        },
        EXIT_OK,
    )


# ---------------------------------------------------------------------------
# shortlist
# ---------------------------------------------------------------------------


def _candidates(args) -> tuple[list[Candidate], list[str], object, str]:
    cands: list[Candidate] = []
    skipped: list[str] = []
    center = None
    seen: set = set()
    empty_files: list[str] = []
    for path in args.ids_from or []:
        found, missing, ctr = _read_file(path)
        if center is None and ctr is not None:
            center = ctr
        skipped.extend(missing)
        if not found:
            empty_files.append(path)
        for c in found:
            key = c.ids.cid if c.ids.cid is not None else c.ids.token
            if key not in seen:
                seen.add(key)
                cands.append(c)
    for text in args.hotel or []:
        ids = _parse_id(text)
        key = ids.cid if ids.cid is not None else ids.token
        if key not in seen:
            seen.add(key)
            cands.append(Candidate(name=None, ids=ids))
    if not cands:
        if empty_files:
            raise QueryError(
                f"{', '.join(empty_files)}: no entry carries an id ({len(skipped)} skipped) — "
                f"ids exist only under gmaps.py search --full --json"
            )
        raise QueryError("no candidates — give --ids-from FILE (from gmaps.py search --full --json) or --hotel ID")
    source = "ids-from" if args.ids_from else "hotel"  # 04 §3.3: one of the two
    return cands, skipped, center, source


def _km(lat1, lng1, lat2, lng2) -> float | None:
    if None in (lat1, lng1, lat2, lng2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 6371.0 * math.asin(math.sqrt(a)), 1)


def _row_passes(row: SellerRow, args) -> bool:
    """Seller-level filters: a hotel passes if any of its rows does."""
    if args.max_price is not None:
        v = comparable(row.nightly, "incl")
        if v is None or v > args.max_price:
            return False
    if args.max_total is not None:
        v = comparable(row.stay, "incl")
        if v is None or v > args.max_total:
            return False
    if args.free_cancellation and not row.free_cancellation.shown:
        return False
    return True


def cmd_shortlist(args) -> int:
    """"Which of these hotels is cheapest for these nights?" — these, never a town."""
    stay = _stay(args)
    _check_amenity_names(list(args.amenity or []))
    cands, skipped, center, source = _candidates(args)
    chosen = cands[: args.limit]
    client = _client(args, "shortlist")
    results = list(client.shortlist(chosen, stay))  # plan() refuses up front
    used = client.transport.requests_made

    rows, unpriced, failed = [], [], []
    fetched: list[tuple[Candidate, EntityRecord]] = []
    priced = 0
    filtered_out = 0
    amenity_states: dict[str, dict] = {}
    wanted = list(args.amenity or [])
    for cand, record, exc in results:
        name = cand.name or (record.name if record else None) or f"CID {cand.ids.cid}"
        if exc is not None:
            failed.append({"name": name, "cid": cand.ids.cid, "reason": _failure_reason(exc)})
            continue
        fetched.append((cand, record))
        if not record.sellers:
            unpriced.append({"name": record.name or name, "cid": cand.ids.cid})
            continue
        priced += 1
        states = {label: _amenity_state(record, n, free) for n, free in wanted
                  for label in [_amenity_label(n, free)]}
        amenity_states[record.name] = states
        match = None
        if states:
            match = "has" if all(s == "has" for s in states.values()) else next(
                s for s in states.values() if s != "has"
            )
        passing = tuple(s for s in record.sellers if _row_passes(s, args))
        ok = bool(passing)
        if args.min_rating is not None and (record.rating.score is None or record.rating.score < args.min_rating):
            ok = False
        if args.min_stars is not None and (record.star_class is None or record.star_class[1] < args.min_stars):
            ok = False
        if states and match != "has":
            ok = False
        if not ok:
            filtered_out += 1
            continue
        best, tie = cheapest_seller(passing, "incl")
        lat = cand.lat if cand.lat is not None else record.lat
        lng = cand.lng if cand.lng is not None else record.lng
        rows.append({
            "hotel": {
                "name": record.name, "cid": record.ids.cid, "token": record.ids.token,
                "star_class": {"label": record.star_class[0], "stars": record.star_class[1]} if record.star_class else None,
                "lat": lat, "lng": lng,
            },
            "distance_km": _km(center.lat, center.lng, lat, lng) if center is not None else None,
            "cheapest": _cheapest_dict(best),
            "near_tie": tie,
            "free_cancellation": best.free_cancellation.shown if best else None,
            "rating": {"score": record.rating.score, "reviews": record.rating.reviews},
            "highlights": [h.to_dict() for h in record.highlights] if record.highlights is not None else None,
            "amenity_match": match,
            "sellers_listed": len(record.sellers),
        })

    # amenity counts, over every hotel whose page was read (04 §3.3)
    amenity_counts = None
    amenity_lines = []
    if wanted:
        per_name: dict[str, dict] = {}
        for label in [_amenity_label(n, f) for n, f in wanted]:
            counts = {"has": 0, "has_other_terms": 0, "lacks": 0, "not_listed": 0}
            for _, record in fetched:
                state = next(
                    (_amenity_state(record, n, f) for n, f in wanted if _amenity_label(n, f) == label)
                )
                counts[state] += 1
            per_name[label] = counts
            amenity_lines.append(_amenity_counts_line(label, len(fetched), counts))
        amenity_counts = {"has": 0, "has_other_terms": 0, "lacks": 0, "not_listed": 0}
        for _, record in fetched:
            states = [_amenity_state(record, n, f) for n, f in wanted]
            combined = "has" if all(s == "has" for s in states) else next(s for s in states if s != "has")
            amenity_counts[combined] += 1
        amenity_counts["by_amenity"] = per_name

    sort_key = {
        "total": lambda r: comparable(_price_of(r, "stay"), "incl"),
        "nightly": lambda r: comparable(_price_of(r, "nightly"), "incl"),
        "rating": lambda r: -(r["rating"]["score"] or 0),
    }[args.sort]
    rows.sort(key=lambda r: (sort_key(r) is None, sort_key(r) or 0))

    considered = len(chosen)
    if rows:
        code = EXIT_OK
    elif failed:
        code = EXIT_NET   # a negative that could not check its candidates is not a negative
    else:
        code = EXIT_NONE

    if not args.json:
        where = f" near {center.label}" if center is not None and getattr(center, "label", None) else ""
        _header(None, f"{considered} hotel{'s' if considered != 1 else ''} checked{where}", stay, stay.currency,
                priced=bool(rows))
        print()
        for line in amenity_lines:
            print(line)
        if rows:
            # The header names the cheapest hotel on the same basis the table
            # is sorted on — the stay total by default (fees can make the
            # cheapest stay and the cheapest night different hotels), per
            # night under --sort nightly — whatever --sort rating did to the
            # row order.
            per = "nightly" if args.sort == "nightly" else "stay"

            def _incl(r: dict, which: str) -> float | None:
                return comparable(_price_of(r, which), "incl")

            top = min(rows, key=lambda r: (_incl(r, per) is None, _incl(r, per) or 0))
            best_price = top["cheapest"][per]
            basis_note = (f" ({SINGLE_LABEL})" if best_price["basis"] == "single" else " incl. tax")
            figure = best_price["amount"] if best_price["basis"] == "single" else best_price["incl_tax"]
            if per == "stay":
                nightly = _incl(top, "nightly")
                unit = " for the stay" + (f" ({_money(nightly)}/night)" if nightly is not None else "")
            else:
                unit = "/night"
            print(
                f"Cheapest of the {considered} checked{where}: {top['hotel']['name']} — "
                f"{_money(figure)}{unit}{basis_note} via {top['cheapest']['seller']}"
                + (f" ({filtered_out} filtered out)" if filtered_out else "")
            )
            table = []
            for r in rows:
                c = r["cheapest"]
                table.append((
                    _clip(r["hotel"]["name"] or "?", 34),
                    # the flag belongs to the single-figure seller, which this
                    # table does not show — say so rather than tag this cell's basis
                    _price_cell(_price_of(r, "nightly")) + (" (a single-figure seller ≈ same price)" if r["near_tie"] else ""),
                    _price_cell(_price_of(r, "stay")),
                    _clip(c["seller"], 24) + (" (own site)" if c["own_site"] else ""),
                    "free" if r["free_cancellation"] else "none shown",
                    f"{r['rating']['score']} ({r['rating']['reviews']:,})" if r["rating"]["score"] is not None and r["rating"]["reviews"] else str(r["rating"]["score"] or "—"),
                    r["hotel"]["star_class"]["label"] if r["hotel"]["star_class"] else "—",
                    f"{r['distance_km']} km" if r["distance_km"] is not None else "—",
                ))
            print()
            print(_table(("HOTEL", "PER NIGHT", "STAY", "CHEAPEST SELLER", "CANCEL", "RATING", "CLASS", "DIST"), table))
        else:
            print(
                f"None of the {considered} checked{where} has a rate"
                + (" passing the filters" if filtered_out else " listed for these nights")
                + (f" — {filtered_out} priced but filtered out" if filtered_out else "")
            )
        if unpriced:
            print(f"no rates listed for these nights: {', '.join(u['name'] for u in unpriced)}")
        if failed:
            print("could not be checked:")
            for f in failed:
                print(f"  {f['name']}: {f['reason']}")
        if skipped:
            print(f"skipped (no id in the file — re-run gmaps with --full): {', '.join(skipped)}")
        if len(cands) > considered:
            print(f"({len(cands) - considered} more candidates not checked — raise --limit, max {MAX_LIMIT})")
        _footer(used, single_rows=any(r["cheapest"]["basis"] == "single" for r in rows))

    if code == EXIT_NET:
        return _fail(
            args, EXIT_NET,
            f"no candidate passed the filters and {len(failed)} of {considered} could not be "
            f"checked ({priced} priced, {filtered_out} filtered out, {len(unpriced)} with no "
            f"rates) — first failure: {failed[0]['name']}: {failed[0]['reason']}",
        )
    return _emit(
        args,
        {
            "command": "shortlist",
            "query": {
                "hotels": [_hotel_dict(c.ids, c.name) for c in chosen],
                "requested": stay.to_dict(),
                "echoed": stay.to_dict() if fetched else None,
                "echo_matched": True,
            },
            "source": source,
            "place": center.to_dict() if center is not None else None,
            "candidates_considered": considered,
            "candidates_available": len(cands),
            "priced": priced,
            "filtered_out": filtered_out,
            "amenity_counts": amenity_counts,
            "rows": rows,
            "unpriced": unpriced,
            "failed": failed,
            "skipped": skipped,
            "sort": args.sort,
            "requests_used": used,
        },
        code,
    )


def _price_of(row: dict, which: str) -> Price:
    d = row["cheapest"][which]
    return Price(ex_tax=d["ex_tax"], incl_tax=d["incl_tax"], currency=d["currency"],
                 basis=d["basis"], amount=d.get("amount"))


# ---------------------------------------------------------------------------
# cheapest — a date sweep
# ---------------------------------------------------------------------------


def cmd_cheapest(args) -> int:
    """"Which check-in day for N nights is cheapest at this hotel?" """
    visited = len(range(0, args.days, args.step))
    if args.max_requests is None:
        args.max_requests = min(visited + RETRY_HEADROOM, MAX_MAX_REQUESTS)
    ids, name, note = _hotel_from_args(args)
    # validate the first stay before the transport exists: a refusal costs no request
    first = validate_stay(args.checkin, args.checkin + timedelta(days=args.nights), args.adults,
                          args.child_age or [], args.currency)
    client = _client(args, "cheapest")
    sweep = client.sweep(ids, args.checkin, args.nights, args.days, args.step, args.adults,
                         args.child_age or [], args.currency)

    rows = []
    record_seen: EntityRecord | None = None
    for stay, record, exc in sweep:
        row = {
            "checkin": stay.checkin.isoformat(),
            "checkout": stay.checkout.isoformat(),
            "weekday": stay.checkin.strftime("%a"),
            "cheapest": None, "near_tie": False, "headline": None, "breakdown": None,
            "failed": exc is not None,
            "reason": _failure_reason(exc) if exc is not None else None,
            "sellers_listed": 0,
        }
        if record is not None:
            record_seen = record_seen or record
            best, tie = cheapest_seller(record.sellers, "incl")
            row.update({
                "cheapest": _cheapest_dict(best), "near_tie": tie,
                "headline": record.headline.to_dict() if record.headline else None,
                "breakdown": record.breakdown.to_dict() if record.breakdown else None,
                "sellers_listed": len(record.sellers),
            })
        rows.append(row)
    used = client.transport.requests_made

    priced = [r for r in rows if r["cheapest"] is not None]
    failed = [r for r in rows if r["failed"]]
    empty = [r for r in rows if not r["failed"] and r["cheapest"] is None]
    reliable = not failed

    def stay_figure(r: dict) -> float | None:
        return comparable(_price_of(r, "stay"), "incl")

    best = min(priced, key=lambda r: stay_figure(r) or 0) if priced else None
    best_out = None
    if best is not None:
        best_out = {
            "checkin": best["checkin"], "checkout": best["checkout"], "weekday": best["weekday"],
            "stay_incl_tax": stay_figure(best), "stay_basis": best["cheapest"]["stay"]["basis"],
            "seller": best["cheapest"]["seller"],
        }

    if not args.json:
        hotel_name = (record_seen.name if record_seen else name) or f"CID {ids.cid}"
        word = "Cheapest" if reliable else "Lowest seen"
        print(
            f"{hotel_name} — {args.nights}-night stays, check-in {rows[0]['checkin']} to "
            f"{rows[-1]['checkin']} (step {args.step}) — for "
            f"{_party_text(first.adults, first.child_ages)} — prices in {first.currency}"
        )
        print()
        table = []
        for r in rows:
            mark = ""
            if best is not None and r is best:
                mark = " <- cheapest" if reliable else " <- lowest seen"
            if r["failed"]:
                table.append((r["checkin"], r["checkout"], r["weekday"], "failed", "—", r["reason"] or ""))
            elif r["cheapest"] is None:
                table.append((r["checkin"], r["checkout"], r["weekday"], "no rates listed", "—", ""))
            else:
                c = r["cheapest"]
                table.append((
                    r["checkin"], r["checkout"], r["weekday"],
                    _price_cell(_price_of(r, "nightly")),
                    _price_cell(_price_of(r, "stay")),
                    _clip(c["seller"], 24) + mark,
                ))
        print(_table(("CHECK-IN", "CHECKOUT", "DAY", "PER NIGHT", "STAY", "SELLER"), table))
        print()
        if best is not None:
            figure = _price_text(_price_of(best, "stay"))
            print(f"{word}: check in {best['checkin']} ({best['weekday']}) — stay {figure} via {best['cheapest']['seller']}")
        if failed:
            # "cheapest" is withheld: a failed day could undercut every priced one
            print(
                f"Of the {len(rows)} days checked, {len(failed)} failed — a failed day could be "
                f"lower, so the lowest seen is not called the lowest with confidence"
            )
        if empty and not failed:
            print(f"{len(empty)} of {len(rows)} days had no rates listed")
        _footer(used, single_rows=any(r["cheapest"]["basis"] == "single" for r in priced))

    if not priced and failed:
        return _fail(
            args, EXIT_NET,
            f"no date was priced and {len(failed)} of {len(rows)} failed — first failure: "
            f"{failed[0]['checkin']}: {failed[0]['reason']}",
        )
    return _emit(
        args,
        {
            "command": "cheapest",
            "query": _query(ids, name, first, record_seen, note) | {"echoed": None, "echo_matched": True},
            "hotel": _hotel_dict(record_seen.ids if record_seen else ids, record_seen.name if record_seen else name),
            "nights": args.nights,
            "window": {"start": rows[0]["checkin"], "end": rows[-1]["checkin"], "step": args.step},
            "occupancy": _occupancy_dict(first),
            "currency": first.currency,
            "rows": rows,
            "days_visited": len(rows),
            "days_priced": len(priced),
            "days_empty": len(empty),
            "days_failed": len(failed),
            "cheapest_is_reliable": reliable,
            "best": best_out,
            "requests_used": used,
        },
        EXIT_OK if priced else EXIT_NONE,
    )


# ---------------------------------------------------------------------------
# watch — one check; cron owns the loop
# ---------------------------------------------------------------------------


def cmd_watch(args) -> int:
    """"Tell me when this hotel drops under $X for these nights."

    `--under` is a trigger, never a filter: today's price is never a reason to
    refuse. Exit 0 fired, 1 keep waiting (above, or no rates), 2 dead config,
    3 the check itself failed and says nothing about the price.
    """
    if (args.under is None) == (args.under_total is None):
        raise QueryError("give exactly one of --under P (per night) or --under-total P (the stay)")
    stay = _stay(args)
    ids, name, note = _hotel_from_args(args)
    client = _client(args, "watch")
    client.transport.plan(1, "a watch check")
    record = client.quote(ids, stay)
    used = client.transport.requests_made
    observed = datetime.now(timezone.utc)

    per = "night" if args.under is not None else "stay"
    value = args.under if per == "night" else args.under_total
    row, tie = cheapest_row(record.sellers, args.basis, per)
    price = None if row is None else (row.nightly if per == "night" else row.stay)
    figure = comparable(price, args.basis)
    fired = figure is not None and figure <= value
    basis_word = "incl. tax" if args.basis == "incl" else "before tax"
    unit = "per night" if per == "night" else "for the stay"

    if not args.json:
        _header(record, name, stay, stay.currency)
        if row is None or price is None or figure is None:
            if record.sellers:
                print(f"No seller states a {basis_word} figure {unit} — nothing to compare; keep waiting")
            else:
                print("No rates listed for these nights — keep waiting (a cancellation is what this watch is for)")
        else:
            how = f"({SINGLE_LABEL})" if price.basis == "single" else basis_word
            line = (
                f"listed at {_money(figure)} {how} {unit} via {row.seller} at "
                f"{observed.strftime('%H:%M')} UTC, for {_party_text(stay.adults, stay.child_ages)}"
            )
            if fired:
                print(f"AT OR UNDER your {_money(value)} {unit} ({basis_word}): {line}")
            else:
                print(f"Cheapest {line} — still above your {_money(value)} {unit} ({basis_word}); keep waiting")
            if tie:
                print(f"  (a single-figure row sits within 1.00 of it — {TIE_LABEL})")
        _footer(used)
    return _emit(
        args,
        {
            "command": "watch",
            "query": _query(ids, name, stay, record, note),
            "hotel": _hotel_dict(record.ids, record.name),
            "stay": _stay_dict(stay),
            "occupancy": _occupancy_dict(stay),
            "currency": stay.currency,
            "threshold": {"value": value, "basis": args.basis, "per": per},
            "cheapest": _cheapest_dict(row),
            "compared": figure,
            "fired": fired,
            "observed_at": observed.isoformat(timespec="seconds"),
            "requests_used": used,
        },
        EXIT_OK if fired else EXIT_NONE,
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args) -> int:
    """Environment and transport report, one fetch of a fixed CID. Exit 0 or 3, never 1."""
    started = time.monotonic()
    try:
        import requests  # noqa: F401
        have_requests = True
    except ImportError:
        have_requests = False
    report = {
        "command": "doctor",
        "python": platform.python_version(),
        "requests": have_requests,
        "transport": None,
        "tls_path": None,
        "host_reachable": False,
        "healthy_page": False,
        "echo_matched": None,
        "currency": None,
        "hotel": {"cid": DOCTOR_CID, "token": DOCTOR_TOKEN, "name": None},
        "requests_used": 0,
        "elapsed_s": 0.0,
    }
    today = _today()
    stay = validate_stay(today + timedelta(days=DOCTOR_DAYS_AHEAD),
                         today + timedelta(days=DOCTOR_DAYS_AHEAD + 1), 2, [], "CAD")
    ids = HotelIds(ftid=None, place_id=None, cid=DOCTOR_CID, token=DOCTOR_TOKEN, kind=KIND_HOTEL)
    client = _client(args, "doctor")
    client.transport.plan(1, "the doctor's fetch")
    error = None
    try:
        html = client.fetch_entity(ids, stay)
        report["host_reachable"] = True
        report["healthy_page"] = not looks_blocked(html)
        if not report["healthy_page"]:
            error = "www.google.com answered, but without the ds:2 block every hotel page carries — a block or consent page"
        else:
            record = entity_record(html)
            report["hotel"]["name"] = record.name
            report["echo_matched"] = record.echo.matches(stay)
            report["currency"] = record.currency or record.echo.currency
            if not report["echo_matched"]:
                error = "the page priced a different stay than requested — the ts encoding is off"
    except HotelsHTTPError as e:
        error = str(e)
    except (UnknownEntity, PayloadError) as e:
        report["host_reachable"] = True
        error = f"{type(e).__name__}: {e}"
    t = client.transport
    report["transport"] = getattr(t, "transport_used", None)
    report["tls_path"] = getattr(t, "tls_path", None)
    report["requests_used"] = t.requests_made
    report["elapsed_s"] = round(time.monotonic() - started, 2)

    if not args.json:
        print(f"python          {report['python']}")
        print(f"requests        {'present' if have_requests else 'absent (curl/urllib fallback)'}")
        print(f"transport       {report['transport'] or '—'}")
        print(f"tls_path        {report['tls_path'] or '—'}")
        print(f"host_reachable  {report['host_reachable']}   (www.google.com)")
        print(f"healthy_page    {report['healthy_page']}   (ds:2 block present)")
        print(f"echo_matched    {report['echo_matched']}")
        print(f"currency        {report['currency'] or '—'}")
        print(f"hotel           {report['hotel']['name'] or '—'} (CID {DOCTOR_CID}, "
              f"{stay.checkin} → {stay.checkout}, 2 adults)")
        print(f"requests_used   {report['requests_used']}   elapsed {report['elapsed_s']}s")
        if not error:
            print("ok — the transport, the page and the stay echo all check out")
    if error:
        # the diagnostics are the point of `doctor`, so the failure object carries them
        return _fail(args, EXIT_NET, error, extra=report)
    return _emit(args, report, EXIT_OK)


# ---------------------------------------------------------------------------
# Argument wiring
# ---------------------------------------------------------------------------


def _add_stay(parser: argparse.ArgumentParser, checkout: bool = True) -> None:
    parser.add_argument("--checkin", type=_date, required=True,
                        help="check-in date, YYYY-MM-DD or +N days from today")
    if checkout:
        parser.add_argument("--checkout", type=_date, required=True,
                            help="checkout date, YYYY-MM-DD or +N days from today")
    parser.add_argument("--adults", type=_bounded("--adults", 1, 8), default=2,
                        help="adults in the one room (default 2, max 8)")
    parser.add_argument("--child-age", type=_bounded("--child-age", 0, 17), action="append",
                        metavar="AGE", help="a child's age, 0-17; repeat per child (max 6)")
    parser.add_argument("--currency", type=_currency, default="CAD",
                        help="price currency (default CAD); must be one Google offers")
    parser.add_argument("--country", type=_country, default="CA", metavar="CC",
                        help="point of sale (gl=), formatting only (default CA)")


def _add_common(parser: argparse.ArgumentParser, default_budget: int | None) -> None:
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (same exit codes)")
    parser.add_argument("--max-requests", type=_budget, default=None,
                        help=f"ceiling on requests to Google (default "
                             f"{default_budget if default_budget is not None else 'visited dates + ' + str(RETRY_HEADROOM)}; "
                             f"max {MAX_MAX_REQUESTS})")


def _add_hotel(parser: argparse.ArgumentParser, token: bool) -> None:
    parser.add_argument("hotel", nargs="?", metavar="HOTEL",
                        help="an id in any form: 0x…:0x… (ftid), ChIJ… (place_id), or a decimal "
                             "CID. A name is refused — find ids with gmaps.py search --full --json")
    parser.add_argument("--ids-from", metavar="FILE",
                        help="a `gmaps.py search --full --json` file holding exactly one hotel "
                             "(use shortlist for several)")
    if token:
        parser.add_argument("--token", metavar="TOK",
                            help="an entity token from a Google Hotels link (the only way to price a vacation rental)")


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that respects --json when it rejects the arguments (flights cli.py:802)."""

    #: Set by main() to the argv actually being parsed, so a library call like
    #: main([..., "--json"]) is honoured rather than sys.argv being consulted.
    _argv: list[str] | None = None

    def error(self, message: str):
        argv = self._argv if self._argv is not None else sys.argv[1:]
        if "--json" in argv:
            print(json.dumps(
                {"schema_version": SCHEMA_VERSION, "ok": False,
                 "error": f"{self.prog}: {message}"},
                indent=2, ensure_ascii=False,
            ))
            raise SystemExit(EXIT_USAGE)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="hotels",
        description="Price hotels on Google Hotels from the command line, by id. "
                    "Read-only: it never books, holds or pays for anything.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    p = sub.add_parser("resolve", help="a hotel's ids in every form (offline, zero requests)")
    p.add_argument("hotel", nargs="?", metavar="ID", help="ftid, place_id, CID or entity token")
    p.add_argument("--ids-from", metavar="FILE", action="append",
                   help="a `gmaps.py search --full --json` file (repeatable)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("quote", help="what one hotel costs for these nights, from every seller")
    _add_hotel(p, token=True); _add_stay(p); _add_common(p, DEFAULT_MAX_REQUESTS["quote"])
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("shortlist", help="which of these hotels is cheapest for these nights")
    p.add_argument("--ids-from", metavar="FILE", action="append",
                   help="a `gmaps.py search --full --json` file (repeatable)")
    p.add_argument("--hotel", metavar="ID", action="append", help="an id in any form (repeatable)")
    _add_stay(p)
    p.add_argument("--limit", type=_bounded("--limit", 1, MAX_LIMIT), default=DEFAULT_LIMIT,
                   help=f"most hotels to fetch, in the files' order (default {DEFAULT_LIMIT}, max {MAX_LIMIT})")
    p.add_argument("--max-price", type=_positive("--max-price"), metavar="P",
                   help="incl-tax nightly ceiling, applied after the fetches")
    p.add_argument("--max-total", type=_positive("--max-total"), metavar="P",
                   help="incl-tax stay ceiling, applied after the fetches")
    p.add_argument("--min-rating", type=_positive("--min-rating", 5), metavar="R")
    p.add_argument("--min-stars", type=_bounded("--min-stars", 1, 5), metavar="N")
    p.add_argument("--free-cancellation", action="store_true",
                   help="keep only sellers showing a free-cancellation deadline")
    p.add_argument("--amenity", type=_amenity, action="append", metavar="NAME[:free]",
                   help="keep hotels Google lists as having it (e.g. wifi:free, parking); repeatable")
    p.add_argument("--sort", choices=("total", "nightly", "rating"), default="total")
    _add_common(p, DEFAULT_MAX_REQUESTS["shortlist"])
    p.set_defaults(func=cmd_shortlist)

    p = sub.add_parser("cheapest", help="which check-in day for N nights is cheapest at one hotel")
    _add_hotel(p, token=False); _add_stay(p, checkout=False)
    p.add_argument("--nights", type=_bounded("--nights", 1, MAX_NIGHTS_FLAG), required=True,
                   help="stay length; it slides with the check-in")
    p.add_argument("--days", type=_bounded("--days", 1, MAX_DAYS), default=DEFAULT_DAYS,
                   help=f"how many check-in dates to try, from --checkin (default {DEFAULT_DAYS}, max {MAX_DAYS})")
    p.add_argument("--step", type=_bounded("--step", 1, MAX_DAYS), default=1,
                   help="stride in days; 7 = the same weekday each week (default 1)")
    _add_common(p, None)
    p.set_defaults(func=cmd_cheapest)

    p = sub.add_parser("watch", help="one check against a price threshold, for cron")
    _add_hotel(p, token=True); _add_stay(p)
    p.add_argument("--under", type=_positive("--under"), metavar="P",
                   help="fire (exit 0) when the cheapest seller's nightly price is at or under P")
    p.add_argument("--under-total", type=_positive("--under-total"), metavar="P",
                   help="fire when the cheapest seller's stay price is at or under P")
    p.add_argument("--basis", choices=("incl", "ex"), default="incl",
                   help="compare incl-tax (default; what leaves the card) or before-tax figures")
    _add_common(p, DEFAULT_MAX_REQUESTS["watch"])
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("doctor", help="environment and transport report (one request)")
    p.add_argument("--country", type=_country, default="CA", metavar="CC")
    _add_common(p, DEFAULT_MAX_REQUESTS["doctor"])
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = build_parser()
    _Parser._argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (QueryError, RequestBudgetError, UnknownEntity, IdError) as e:
        return _fail(args, EXIT_USAGE, str(e))
    except (HotelsHTTPError, PayloadError) as e:
        return _fail(args, EXIT_NET, str(e))
    except Exception as e:  # never a traceback in an agent's transcript
        return _fail(args, EXIT_NET, f"unexpected {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
