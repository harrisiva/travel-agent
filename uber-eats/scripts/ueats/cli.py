"""CLI — Lane C. Nine commands (design 02 §3), human tables by default, --json
one object via model.envelope(); exit codes 0/1/2/3 identical under --json;
one stderr line on 2/3; catch-all → 3; Ctrl-C → 130.

Commands: locate, nearby, find, menu, item, deals, compare, watch, doctor.

Budgets: every command works out its whole plan — resolving --at (0 when the
address is a token or cached, else 2), the command's own calls, and any
per-store fan-out (`compare --stores`, `deals --items --limit`) — and hands it
to transport.plan() before the first request. The default ceiling is exactly
that plan, capped at HARD_CAP; `--max-requests` lowers it (never raises it past
the cap). DEFAULT_BUDGET documents the table in design 02 §1, which assumes a
cached address and an id-form STORE.

Money is rendered only through Money.display() / Money.amount, which round
half-even (lead's ruling): 1087.5 → "10.88", a BOGO effective_each of 2149/2
→ "10.74".
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
from decimal import Decimal
from typing import Sequence

from . import client as _client_mod
from . import ids as idmod
from .client import (
    DOCTOR_LOCATION, DOCTOR_QUERY, LOCATE_REQUESTS, MAX_PAGES, NOT_IN_FEED, Ambiguous, Client,
    CompareRow, OutsideServiceArea, QueryError, StoreFailure, deal_type, eta_text, hours_today,
    keep_deal, next_opening,
)
from .http import (
    BLOCKED_MESSAGE, Blocked, LookupFailure, NotAllowed, PayloadError, RequestBudgetError, Transport,
    UEHTTPError,
)
from .location import LocationCache
from .model import Dish, ItemDetail, Location, OptionGroup, Store, StoreRow, envelope, fold
from . import location as locmod

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3
HARD_CAP = 25
#: Spare requests above the plan so one transient 5xx retry does not end the run.
RETRY_HEADROOM = 2
DEFAULT_BUDGET = {"locate": 2, "nearby": 3, "find": 3, "menu": 4, "item": 5,
                  "deals": 3, "compare": 11, "watch": 4, "doctor": 3}

DEFAULT_LIMIT_NEARBY = 25
DEFAULT_LIMIT_DEALS = 30
DEFAULT_LIMIT_DEALS_ITEMS = 10      # with --items each store costs a request
DEFAULT_STORES_COMPARE, MAX_STORES_COMPARE = 10, 24
MAX_LIMIT = 500

DEAL_TYPE_CHOICES = ("bogo", "percent", "dollar", "free-delivery", "other")
PUBLIC_ONLY = "Public deals only — Uber One and account offers aren't visible."
NO_ADDRESS = "no address given — ETA and distance not shown"


# ---------------------------------------------------------------------------
# argparse types
# ---------------------------------------------------------------------------

def _budget(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not 1 <= count <= HARD_CAP:
        raise argparse.ArgumentTypeError(f"--max-requests must be between 1 and {HARD_CAP}")
    return count


def _bounded(name: str, low: int, high: int | None = None):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} expects a whole number, got {value!r}")
        if number < low or (high is not None and number > high):
            limit = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"{name} must be {limit} (got {number})")
        return number
    return parse


def _positive(name: str, high: float | None = None):
    def parse(value: str) -> float:
        try:
            number = float(value.replace("$", "").replace(",", ""))
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} expects a number, got {value!r}")
        if not math.isfinite(number) or number <= 0 or (high is not None and number > high):
            top = f" and at most {high:g}" if high is not None else ""
            raise argparse.ArgumentTypeError(f"{name} must be above 0{top} (got {value})")
        return number
    return parse


def _locale(value: str) -> str:
    v = value.strip().lower()
    if not (len(v) == 2 and v.isalpha()):
        raise argparse.ArgumentTypeError(f"--locale expects a 2-letter code like ca or us, got {value!r}")
    return v


def _text(name: str):
    def parse(value: str) -> str:
        v = " ".join(value.split())
        if not v:
            raise argparse.ArgumentTypeError(f"{name} must not be empty")
        return v
    return parse


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return fill + text if right else text + fill


def _clip(text: str, width: int) -> str:
    if _width(text) <= width:
        return text
    cut = text
    while cut and _width(cut) + 1 > width:
        cut = cut[:-1]
    return cut.rstrip(" ,-") + "…"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()) -> str:
    if not rows:
        return ""
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(str(cell)))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(_pad(str(c), widths[i], right=i in right) for i, c in enumerate(cells)).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def _dash(value) -> str:
    return "—" if value in (None, "") else str(value)


def _rating(row: StoreRow) -> str:
    if row.rating is None:
        return "—"
    return f"{row.rating:.1f}" + (f" ({row.rating_count_text})" if row.rating_count_text else "")


def _km(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _deals_text(deals) -> str:
    return " · ".join(d.text for d in deals) if deals else ""


def _price_text(dish: Dish) -> str:
    """"$10.88 (was $14.50, 25% off)" — the sale price first, always."""
    out = dish.price.display()
    bits = []
    if dish.was:
        bits.append(f"was {dish.was.display()}")
    if dish.deal:
        bits.append(dish.deal.text)
    if bits:
        out += f" ({', '.join(bits)})"
    if dish.price_unclear:
        out += " [price unclear: two amounts shown, no strikethrough]"
    return out


def _dish_flags(dish: Dish) -> str:
    flags = []
    if dish.sold_out:
        flags.append("sold out")
    if dish.has_options:
        flags.append("options")
    if dish.note:
        flags.append(f"note: {dish.note}")
    return f"  [{', '.join(flags)}]" if flags else ""


def _hours_text(store: Store, at) -> tuple[str, str | None, list[dict]]:
    """("open now · today 11:00–23:30", "16:30" or None, spans as dicts)."""
    spans = hours_today(store, at)
    today = ", ".join(f"{s.to_dict()['start']}–{s.to_dict()['end']}" for s in spans) if spans else None
    opens = next_opening(spans, at)
    if store.is_open and store.is_orderable:
        state = "open now"
    elif store.is_open:
        state = "open but not taking orders"
    else:
        state = "CLOSED"
        if store.closed_message:
            state += f" ({store.closed_message})"
        elif opens:
            state += f" (opens {opens})"
        elif spans is None:
            state += " (no hours listed for today)"
    if today:
        state += f" · today {today}"
    elif spans is not None:
        state += " · closed all day today"
    return state, opens, [s.to_dict() for s in spans] if spans else []


# ---------------------------------------------------------------------------
# Output envelope
# ---------------------------------------------------------------------------

def _emit(args, payload: dict, code: int) -> int:
    if args.json:
        print(json.dumps(envelope(args.command, True, **payload), indent=2, ensure_ascii=False))
    return code


def _fail(args, code: int, message: str, kind: str, extra: dict | None = None) -> int:
    message = " ".join(str(message).split())
    if getattr(args, "json", False):
        print(json.dumps(
            {**envelope(getattr(args, "command", None), False, error=message, kind=kind), **(extra or {})},
            indent=2, ensure_ascii=False))
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# Wiring: client, budget, location
# ---------------------------------------------------------------------------

def _client(args) -> Client:
    transport = Transport(max_requests=HARD_CAP, locale=args.locale)
    try:
        cache = LocationCache(locmod.default_cache_dir())
    except Exception:
        cache = None          # the cache is a convenience, never a reason to fail
    return Client(transport, cache)


def _plan(args, client: Client, count: int, what: str, fixed: bool = False) -> None:
    """Set the ceiling and refuse up front. Ceiling = plan + RETRY_HEADROOM (a
    transient 5xx retry must not end the run), capped at HARD_CAP; --max-requests
    only ever lowers it. `fixed` (doctor) ignores --max-requests: doctor exits 0 or 3."""
    ceiling = min(HARD_CAP, count + RETRY_HEADROOM)
    if args.max_requests and not fixed:
        ceiling = min(ceiling, args.max_requests)
    client.transport.max_requests = ceiling
    client.planned = count
    client.transport.plan(count, what)


def _usage(client: Client) -> dict:
    t = client.transport
    return {"requests_planned": getattr(client, "planned", None), "requests_ceiling": t.max_requests,
            "requests_used": t.requests_made}


def _address_json(loc: Location | None) -> dict | None:
    """The resolved location as an object (null without --at); the human header keeps the label."""
    return None if loc is None else {**loc.to_dict(), "label": loc.label}


def _at_cost(args, client: Client) -> int:
    return client.location_cost(getattr(args, "at", None))


def _address_line(loc: Location | None, pickup: bool = False) -> str:
    if loc is None:
        return NO_ADDRESS
    return f"{'Pickup' if pickup else 'Delivery'} to {loc.label}"


def _resolve_store(args, client: Client, loc: Location | None) -> tuple[str, str | None]:
    uuid, matched = client.resolve_store(args.store, loc)
    return uuid, matched


def _failed_kind(failed: list[StoreFailure]) -> str:
    """The error kind when every store failed: lookup if all were unknown ids, usage if the budget ran out."""
    kinds = {f.kind for f in failed}
    if kinds == {"lookup"}:
        return "lookup"
    if "usage" in kinds and kinds <= {"usage", "lookup"}:
        return "usage"
    return "network"


def _store_cost(args) -> int:
    return 0 if idmod.store_uuid((args.store or "").strip()) else 1


def _filter_rows(rows: list[StoreRow], args) -> list[StoreRow]:
    out = []
    want_type = deal_type(args.deal_type) if getattr(args, "deal_type", None) else None
    name = fold(args.name) if getattr(args, "name", None) else None
    for r in rows:
        if getattr(args, "deals", False) and not r.deals:
            continue
        if want_type and not any(d.type == want_type for d in r.deals):
            continue
        if getattr(args, "min_rating", None) is not None and (r.rating is None or r.rating < args.min_rating):
            continue
        if getattr(args, "max_eta", None) is not None and (r.eta_max is None or r.eta_max > args.max_eta):
            continue
        if getattr(args, "max_km", None) is not None and (r.distance_km is None or r.distance_km > args.max_km):
            continue
        if name and name not in fold(r.name):
            continue
        out.append(r)
    return out


def _sort_rows(rows: list[StoreRow], how: str) -> list[StoreRow]:
    inf = float("inf")
    if how == "rating":
        return sorted(rows, key=lambda r: -(r.rating if r.rating is not None else -1))
    if how == "eta":
        return sorted(rows, key=lambda r: r.eta_max if r.eta_max is not None else inf)
    if how == "distance":
        return sorted(rows, key=lambda r: r.distance_km if r.distance_km is not None else inf)
    return list(rows)


def _by_distance(rows: list[StoreRow]) -> list[StoreRow]:
    return _sort_rows(rows, "distance")


def _store_rows_table(rows: list[StoreRow], start: int = 1) -> str:
    table = []
    for i, r in enumerate(rows, start):
        table.append([str(i), _clip(r.name, 40), _rating(r), _dash(eta_text(r)), _km(r.distance_km),
                      _clip(_deals_text(r.deals), 40), r.uuid])
    return _table(["#", "Store", "Rating", "ETA", "km", "Deals", "UUID"], table, right=(0, 4))


def _feed_header(shown: int, meta_total: int, loc: Location, pages: int, extra: str = "") -> str:
    page = f", {pages} feed pages" if pages > 1 else ""
    return (f"{shown} of the {meta_total} stores Uber returned near {loc.label}"
            f"{page}{extra} — Uber's list, not the whole market")


# ---------------------------------------------------------------------------
# locate
# ---------------------------------------------------------------------------

def cmd_locate(args) -> int:
    client = _client(args)
    _plan(args, client, LOCATE_REQUESTS, "locate (place search + coordinates)")
    candidates, loc = client.locate(args.address, args.pick)
    token = locmod.token(loc)
    rows = []
    for i, c in enumerate(candidates, 1):
        d = c.to_dict()
        d["location"] = token if i == args.pick else None
        rows.append(d)
    payload = {"query": args.address, "picked": args.pick, "candidates": rows,
               "location": loc.to_dict(), "token": token,
               **_usage(client)}
    if not args.json:
        print(f"Uber resolved {args.address!r} to: {loc.label}"
              + (f"  ({loc.latitude}, {loc.longitude})" if loc.latitude is not None else ""))
        print(f"location token (use as --at, costs no requests): {token}")
        others = [(i, c) for i, c in enumerate(candidates, 1) if i != args.pick]
        if others:
            print("other candidates (re-run with --pick N):")
            for i, c in others:
                print(f"  {i}. {c.line1}, {c.line2}  [{c.provider}]")
    return _emit(args, payload, EXIT_OK)


# ---------------------------------------------------------------------------
# nearby
# ---------------------------------------------------------------------------

def cmd_nearby(args) -> int:
    client = _client(args)
    _plan(args, client, _at_cost(args, client) + args.pages, f"nearby ({args.pages} feed page(s))")
    loc = client.location_for(args.at)
    rows, meta = client.feed(loc, args.pages)
    kept = _sort_rows(_filter_rows(rows, args), args.sort)
    shown = kept[: args.limit]
    payload = {"address": _address_json(loc), "mode": "delivery", "exhaustive": False,
               "stores_returned": meta.stores_returned, "filtered_out": len(rows) - len(kept),
               "pages": args.pages, "has_more": meta.has_more,
               "rows": [r.to_dict() for r in shown], **_usage(client)}
    if not args.json:
        extra = f" ({len(rows) - len(kept)} filtered out)" if len(rows) != len(kept) else ""
        print(_feed_header(len(shown), meta.stores_returned, loc, args.pages, extra))
        if shown:
            print(_store_rows_table(shown))
        else:
            print("No stores left after filtering.")
    return _emit(args, payload, EXIT_OK if shown else EXIT_NONE)


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------

def cmd_find(args) -> int:
    client = _client(args)
    _plan(args, client, _at_cost(args, client) + args.pages, f"find ({args.pages} feed page(s))")
    loc = client.location_for(args.at)
    candidates, meta = client.find(args.name, loc, args.pages)
    payload = {"address": _address_json(loc), "query": args.name, "exhaustive": False,
               "stores_returned": meta.stores_returned, "pages": args.pages,
               "candidates": [c.to_dict() for c in candidates],
               **_usage(client)}
    if not candidates:
        payload["message"] = (f"{args.name!r} is not among the {meta.stores_returned} stores Uber's feed "
                              f"returned near {loc.label} — {NOT_IN_FEED}")
        if not args.json:
            print(payload["message"])
        return _emit(args, payload, EXIT_NONE)
    if not args.json:
        print(f"{len(candidates)} of the {meta.stores_returned} stores Uber returned near {loc.label} "
              f"match {args.name!r} — Uber's list, not the whole market")
        table = [[c.match.replace("_", " "), _clip(c.row.name, 40), _rating(c.row), _dash(eta_text(c.row)),
                  _km(c.row.distance_km), _clip(_deals_text(c.row.deals), 30), c.row.uuid] for c in candidates]
        print(_table(["Match", "Store", "Rating", "ETA", "km", "Deals", "UUID"], table, right=(4,)))
    return _emit(args, payload, EXIT_OK)


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------

def _menu_header_lines(store: Store, loc: Location | None, pickup: bool, matched: str | None,
                       store_arg: str, at) -> tuple[list[str], dict]:
    state, opens, spans = _hours_text(store, at)
    bits = [state]
    if store.rating is not None:
        bits.append(f"{store.rating:.1f}" + (f" ({store.rating_count_text})" if store.rating_count_text else ""))
    if loc is not None:
        bits.append(f"delivery {store.eta_text}" if store.eta_text else "delivery ETA —")
        if store.pickup_eta_text:
            bits.append(f"pickup {store.pickup_eta_text}")
        if store.distance_km is not None:
            bits.append(f"{store.distance_km:.1f} km")
        if store.within_range is False:
            bits.append("OUTSIDE delivery range (pickup may still work)")
        elif store.within_range:
            bits.append("within delivery range")
    bits.append(store.currency)
    lines = [f"{store.title}" + (f" — {store.address}" if store.address else ""), " · ".join(bits)]
    if store.cuisines:
        lines.append("Cuisine: " + ", ".join(store.cuisines))
    if store.deals:
        lines.append("Deals: " + " · ".join(d.text for d in store.deals))
    lines.append(_address_line(loc, pickup))
    if matched:
        lines.append(f"Matched by name: {store_arg!r} → {store.title} ({store.uuid})")
    header_json = {"hours_today": spans, "opens": opens}
    return lines, header_json


def cmd_menu(args) -> int:
    client = _client(args)
    at = _client_mod.now()
    _plan(args, client, _at_cost(args, client) + _store_cost(args) + 1, "menu")
    loc = client.location_for(args.at)
    uuid, matched = _resolve_store(args, client, loc)
    store = client.menu(uuid, loc, args.pickup)
    lines, extra = _menu_header_lines(store, loc, args.pickup, matched, args.store, at)

    hidden_sold_out = 0
    dishes: list[Dish] = []
    section_filter = fold(args.section) if args.section else None
    match = fold(args.match) if args.match else None
    for d in store.dishes:
        if section_filter and section_filter not in fold(d.section or ""):
            continue
        if match and match not in fold(d.title) and match not in fold(d.description or ""):
            continue
        if args.deals and d.deal is None:
            continue
        if args.under is not None and d.price.value > Decimal(str(args.under)):
            continue
        if d.sold_out and not args.sold_out:
            hidden_sold_out += 1
            continue
        dishes.append(d)

    sections: dict[str, list[Dish]] = {}
    for d in dishes:
        sections.setdefault(d.section or "Menu", []).append(d)
    payload = {"address": _address_json(loc), "mode": "pickup" if args.pickup else "delivery",
               "store": store.to_dict(), "matched_by_name": matched,
               "deals": [d.to_dict() for d in store.deals],
               "sections": [{"title": t, "dishes": [d.to_dict() for d in ds]} for t, ds in sections.items()],
               "dishes_shown": len(dishes), "dishes_total": len(store.dishes),
               "dishes_sold_out": sum(1 for d in store.dishes if d.sold_out),
               "sold_out_hidden": hidden_sold_out, "duplicates_removed": store.duplicates_removed,
               **_usage(client), **extra}
    if not args.json:
        for line in lines:
            print(line)
        filt = []
        if args.match: filt.append(f"matching {args.match!r}")
        if args.section: filt.append(f"section {args.section!r}")
        if args.deals: filt.append("with a deal")
        if args.under is not None: filt.append(f"under {args.under:g}")
        summary = f"Showing {len(dishes)} of {len(store.dishes)} dishes"
        if filt:
            summary += " " + ", ".join(filt)
        notes = []
        if hidden_sold_out:
            notes.append(f"{hidden_sold_out} sold out hidden; --sold-out shows them")
        if store.duplicates_removed:
            notes.append(f"{store.duplicates_removed} duplicate listings removed")
        if notes:
            summary += f" ({'; '.join(notes)})"
        print(summary)
        for title, ds in sections.items():
            print(f"\n{title}")
            for d in ds:
                print(f"  {_price_text(d)}  {d.title}{_dish_flags(d)}")
        if not dishes and store.dishes:
            print("No dishes match those filters.")
        elif not store.dishes:
            print("Uber returned an empty menu for this store.")
    return _emit(args, payload, EXIT_OK if dishes else EXIT_NONE)


# ---------------------------------------------------------------------------
# item
# ---------------------------------------------------------------------------

def _print_groups(groups: Sequence[OptionGroup], indent: int = 0) -> None:
    pad = "  " * indent
    for g in groups:
        rule = "required" if g.required else "optional"
        if g.min_permitted == g.max_permitted:
            choose = f"choose {g.min_permitted}"
        elif g.max_permitted and g.max_permitted > 0:
            choose = f"choose {g.min_permitted}–{g.max_permitted}"
        else:
            choose = f"choose {g.min_permitted}+"
        print(f"{pad}{g.title}  ({rule}, {choose})")
        for o in g.options:
            price = "free" if o.price.cents == 0 else f"+{o.price.display()}"
            flags = "  [sold out]" if o.sold_out else ""
            print(f"{pad}  {_pad(o.title, 44)} {price}{flags}")
            if o.groups:
                _print_groups(o.groups, indent + 2)


def cmd_item(args) -> int:
    client = _client(args)
    _plan(args, client, _at_cost(args, client) + _store_cost(args) + 2, "item (menu + options)")
    loc = client.location_for(args.at)
    uuid, matched = _resolve_store(args, client, loc)
    store = client.menu(uuid, loc, args.pickup)
    dish = client.match_dish(store, args.dish)
    detail: ItemDetail = client.item_for(store, dish, loc)
    payload = {"address": _address_json(loc), "mode": "pickup" if args.pickup else "delivery",
               "store": {"uuid": store.uuid, "title": store.title, "is_open": store.is_open},
               "matched_by_name": matched,
               "dish": {"uuid": detail.uuid, "title": detail.title, "price": detail.price.to_dict(),
                        "was": detail.was.to_dict() if detail.was else None,
                        "deal": detail.deal.to_dict() if detail.deal else None,
                        "sold_out": detail.sold_out, "section": dish.section},
               "groups": [g.to_dict() for g in detail.groups],
               "from_price": detail.from_price.to_dict(), "required_groups": detail.required_groups,
               **_usage(client)}
    if not args.json:
        price = detail.price.display()
        if detail.was:
            price += f" (was {detail.was.display()}"
            price += f", {detail.deal.text})" if detail.deal else ")"
        elif detail.deal:
            price += f" ({detail.deal.text})"
        print(f"{detail.title} — {price} at {store.title}" + ("  [SOLD OUT]" if detail.sold_out else ""))
        if matched:
            print(f"Matched by name: {args.store!r} → {store.title} ({store.uuid})")
        print(_address_line(loc, args.pickup))
        if detail.required_groups:
            print(f"From {detail.from_price.display()} with the {detail.required_groups} required "
                  f"choice(s) made at their cheapest; optional add-ons extra")
        else:
            print(f"From {detail.from_price.display()} — no required choices; add-ons extra")
        if detail.groups:
            print()
            _print_groups(detail.groups)
        else:
            print("No options on this dish.")
    return _emit(args, payload, EXIT_OK)


# ---------------------------------------------------------------------------
# deals
# ---------------------------------------------------------------------------

def cmd_deals(args) -> int:
    client = _client(args)
    limit = args.limit if args.limit is not None else (DEFAULT_LIMIT_DEALS_ITEMS if args.items else DEFAULT_LIMIT_DEALS)
    per_store = limit if args.items else 0
    what = f"deals ({args.pages} feed page(s)" + (f" + up to {limit} menus for --items" if args.items else "") + ")"
    _plan(args, client, _at_cost(args, client) + args.pages + per_store, what)
    loc = client.location_for(args.at)
    rows, meta = client.feed(loc, args.pages)
    want_type = deal_type(args.type) if args.type else None

    kept: list[tuple[StoreRow, list]] = []
    for r in _filter_rows(rows, args):
        deals = [d for d in r.deals if keep_deal(d, want_type, args.min_spend_at_most)]
        if deals:
            kept.append((r, deals))
    with_deals = sum(1 for r in rows if r.deals)
    shown = kept[:limit]

    dishes_by_store: dict[str, list[Dish]] = {}
    failed: list[StoreFailure] = []
    if args.items and shown:
        dishes_by_store, failed = client.deal_dishes([r for r, _ in shown], loc, False)

    by_type: dict[str, list[dict]] = {"bogo": [], "percent": [], "dollar": [], "free_delivery": [], "other": []}
    stores_json = []
    for r, deals in shown:
        entry = {**r.to_dict(), "eta": eta_text(r), "deals": [d.to_dict() for d in deals]}
        if args.items:
            ds = dishes_by_store.get(r.uuid)
            entry["dishes"] = [d.to_dict() for d in ds] if ds is not None else None
        stores_json.append(entry)
        for d in deals:
            by_type.setdefault(d.type, []).append({"store_uuid": r.uuid, "store": r.name, **d.to_dict()})

    payload = {"address": _address_json(loc), "public_only": True, "exhaustive": False,
               "stores_returned": meta.stores_returned, "stores_with_deals": with_deals,
               "pages": args.pages, "by_type": by_type, "stores": stores_json,
               "failed": [f.to_dict() for f in failed] if args.items else [],
               **_usage(client)}
    if not args.json:
        print(_feed_header(len(shown), meta.stores_returned, loc, args.pages,
                           f" carry a deal that matches ({with_deals} with any public deal)"))
        print(PUBLIC_ONLY)
        labels = {"bogo": "Buy one, get one", "percent": "% off", "dollar": "$ off",
                  "free_delivery": "$0 delivery fee", "other": "Other offers"}
        for t, label in labels.items():
            group = [(r, [d for d in deals if d.type == t]) for r, deals in shown]
            group = [(r, ds) for r, ds in group if ds]
            if not group:
                continue
            print(f"\n{label} ({len(group)} store{'s' if len(group) != 1 else ''})")
            for r, ds in group:
                line = f"  {_clip(r.name, 40)}  {_rating(r)}  {_dash(eta_text(r))}  {_km(r.distance_km)} km  {' · '.join(d.text for d in ds)}"
                print(line)
                if args.items:
                    dishes = dishes_by_store.get(r.uuid)
                    if dishes is None:
                        print("      (menu could not be read — see below)")
                    elif not dishes:
                        print("      no dish on the menu carries a deal marker")
                    else:
                        for d in dishes[:12]:
                            print(f"      {_price_text(d)}  {d.title}{_dish_flags(d)}")
                        if len(dishes) > 12:
                            print(f"      … and {len(dishes) - 12} more (menu {r.uuid} --deals)")
                elif any(d.select_items for d in ds):
                    print(f"      which dishes: run `menu {r.uuid} --deals`")
        if failed:
            print(f"\nCould not read {len(failed)} menu(s):")
            for f in failed:
                print(f"  {f.name} ({f.uuid}): {f.reason}")
        if not shown:
            print("\nNo public deals match those filters.")
    if args.items and shown and len(failed) == len(shown):
        return _fail(args, EXIT_NET, f"every one of the {len(shown)} store menus failed to load: "
                     f"{failed[0].reason}", _failed_kind(failed), payload)
    return _emit(args, payload, EXIT_OK if shown else EXIT_NONE)


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def _compare_row_cells(i: int, r: CompareRow) -> list[str]:
    dish = _clip(r.dish.title, 40) + (" [sold out]" if r.dish.sold_out else "")
    if r.second_free and r.effective_each:
        dish += f" (second free → {r.effective_each.display()} each)"
    return [str(i), r.dish.price.display(), r.dish.was.display() if r.dish.was else "—",
            r.dish.deal.text if r.dish.deal else "—", dish, _clip(r.store.name, 30),
            _rating(r.store), _dash(eta_text(r.store)), _km(r.store.distance_km)]


def cmd_compare(args) -> int:
    client = _client(args)
    _plan(args, client, _at_cost(args, client) + 1 + args.stores, f"compare (feed + up to {args.stores} menus)")
    loc = client.location_for(args.at)
    rows, meta = client.feed(loc, 1)
    chosen = _by_distance(_filter_rows(rows, args))[: args.stores]
    strong, weaker, failed = client.compare(args.dish, chosen, loc, args.pickup)
    payload = {"address": _address_json(loc), "query": args.dish, "mode": "pickup" if args.pickup else "delivery",
               "stores_checked": len(chosen) - len(failed), "stores_planned": len(chosen),
               "stores_returned": meta.stores_returned, "exhaustive": False,
               "rows": [r.to_dict() for r in strong], "weaker": [r.to_dict() for r in weaker],
               "failed": [f.to_dict() for f in failed], **_usage(client)}
    if not args.json:
        print(f"Cheapest {args.dish!r} among the {len(chosen)} nearest stores "
              f"(of {meta.stores_returned} Uber returned near {loc.label}; "
              f"{'pickup' if args.pickup else 'delivery'} prices) — Uber's list, not the whole market")
        headers = ["#", "Price", "Was", "Deal", "Dish", "Store", "Rating", "ETA", "km"]
        if strong:
            print(_table(headers, [_compare_row_cells(i, r) for i, r in enumerate(strong, 1)], right=(0, 1, 2, 8)))
            if any(r.second_free for r in strong):
                print("BOGO dishes are ranked at the price of one; the second is free.")
        else:
            print(f"No dish named like {args.dish!r} on those menus.")
        if weaker:
            print(f"\nWeaker matches ({args.dish!r} appears only in the description):")
            print(_table(headers, [_compare_row_cells(i, r) for i, r in enumerate(weaker, 1)], right=(0, 1, 2, 8)))
        if failed:
            print(f"\nCould not read {len(failed)} store menu(s):")
            for f in failed:
                print(f"  {f.name} ({f.uuid}): {f.reason}")
        if not chosen:
            print("No stores to check after filtering.")
    if chosen and len(failed) == len(chosen):
        return _fail(args, EXIT_NET, f"every one of the {len(chosen)} store menus failed to load: "
                     f"{failed[0].reason}", _failed_kind(failed), payload)
    return _emit(args, payload, EXIT_OK if strong else EXIT_NONE)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

def _watch_condition(args) -> str:
    """Exactly one condition, or a QueryError."""
    picked = []
    if args.open:
        picked.append("open")
    if args.back_in_stock:
        picked.append("back-in-stock")
    if args.item:
        if args.under is not None and args.deal:
            raise QueryError("--item takes one of --under X or --deal, not both")
        if args.under is None and not args.deal:
            raise QueryError("--item needs a condition: --under X (price at or below X) or --deal")
        picked.append("item-under" if args.under is not None else "item-deal")
    elif args.deal:
        picked.append("deal")
    if args.under is not None and not args.item:
        raise QueryError("--under only makes sense with --item \"<dish>\"")
    if len(picked) != 1:
        raise QueryError("watch needs exactly one condition: --open, --deal, --item X --under N, "
                         "--item X --deal, or --back-in-stock X"
                         + (f" (got {', '.join(picked)})" if picked else ""))
    return picked[0]


def cmd_watch(args) -> int:
    condition = _watch_condition(args)
    client = _client(args)
    _plan(args, client, _at_cost(args, client) + _store_cost(args) + 1, "watch (one menu read)")
    loc = client.location_for(args.at)
    uuid, matched = _resolve_store(args, client, loc)
    store = client.menu(uuid, loc, args.pickup)
    at = _client_mod.now()
    stamp = at.strftime("%H:%M")
    state, opens, spans = _hours_text(store, at)
    observed: dict = {"is_open": store.is_open, "is_orderable": store.is_orderable,
                      "hours_today": spans, "opens": opens}
    label = condition

    if condition == "open":
        fired = store.is_open and store.is_orderable
        line = "open and taking orders" if fired else f"still {state.lower()}"
    elif condition == "deal":
        observed["deals"] = [d.to_dict() for d in store.deals]
        fired = bool(store.deals)
        line = ("deal on: " + " · ".join(d.text for d in store.deals)) if fired else "no public deal on any dish"
    else:
        dish = client.match_dish(store, args.item or args.back_in_stock)
        observed["dish"] = dish.to_dict()
        if condition == "item-under":
            label = f"item {dish.title!r} under {args.under:g}"
            observed["threshold"] = args.under
            fired = dish.price.value <= Decimal(str(args.under))
            line = f"{dish.title} is {_price_text(dish)}" + (f" — at or under {args.under:g}" if fired else f" — above {args.under:g}")
        elif condition == "item-deal":
            label = f"item {dish.title!r} deal"
            fired = dish.deal is not None
            line = f"{dish.title}: " + (f"{dish.deal.text} ({_price_text(dish)})" if fired else f"no deal ({dish.price.display()})")
        else:
            label = f"back in stock {dish.title!r}"
            fired = not dish.sold_out
            line = f"{dish.title} is {'available' if fired else 'sold out'} ({dish.price.display()})"
    if condition != "open" and not store.is_open:
        line += f"; store is closed" + (f" (opens {opens})" if opens else "")

    kinds = {"open": "open", "deal": "deal", "item-under": "item_under", "item-deal": "item_deal",
             "back-in-stock": "back_in_stock"}
    dish_name = dish.title if condition not in ("open", "deal") else None
    under = str(Decimal(str(args.under)).quantize(Decimal("0.01"))) if condition == "item-under" else None
    condition_json = {"kind": kinds[condition], "dish": dish_name, "under": under}
    payload = {"address": _address_json(loc), "store": {"uuid": store.uuid, "title": store.title},
               "matched_by_name": matched, "condition": condition_json, "condition_text": label,
               "fired": bool(fired), "observed": observed,
               "checked_at": at.isoformat(timespec="seconds"), **_usage(client)}
    if not args.json:
        print(f"{'FIRED' if fired else 'Not yet'} at {stamp}: {line}")
        print(f"{store.title} ({store.uuid}) — {_address_line(loc, args.pickup)}")
        if matched:
            print(f"Matched by name: {args.store!r} → {store.title}")
    return _emit(args, payload, EXIT_OK if fired else EXIT_NONE)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _blocker(provider: str | None) -> str:
    """Who served the challenge, for doctor's lines."""
    if provider == "recaptcha":
        return "blocked by Uber bot defense (reCAPTCHA)"
    if provider in (None, "cloudflare"):
        return "blocked by Cloudflare"
    return f"blocked by Uber bot defense ({provider})"


def _layer(provider: str | None) -> str:
    if provider == "recaptcha":
        return "Uber's own bot defense (reCAPTCHA)"
    if provider in (None, "cloudflare"):
        return "Cloudflare"
    return f"Uber's own bot defense ({provider})"


def cmd_doctor(args) -> int:
    from . import location as loc_mod
    from . import parse_feed, parse_store
    client = _client(args)
    _plan(args, client, 3, "doctor (place search, feed, one menu)", fixed=True)
    t = client.transport
    steps: list[dict] = []
    healthy = True
    first_store: StoreRow | None = None

    def step(name: str, fn) -> bool:
        nonlocal healthy
        entry = {"name": name, "ok": False, "blocked": False, "provider": None, "detail": "",
                 "transport_used": None, "tls_path": None}
        try:
            entry["detail"] = fn()
            entry["ok"] = True
        except Blocked as e:
            entry["blocked"] = True
            entry["provider"] = getattr(e, "provider", None) or "cloudflare"
            entry["detail"] = f"{_blocker(entry['provider'])}: {e}"
        except (UEHTTPError, PayloadError, LookupFailure, QueryError, RequestBudgetError) as e:
            entry["detail"] = f"{type(e).__name__}: {e}"
        except Exception as e:      # doctor must always produce its report
            entry["detail"] = f"unexpected {type(e).__name__}: {e}"
        entry["transport_used"] = getattr(t, "transport_used", None)
        entry["tls_path"] = getattr(t, "tls_path", None)
        steps.append(entry)
        healthy = healthy and entry["ok"]
        return entry["ok"]

    def s1() -> str:
        cands = loc_mod.search(t, DOCTOR_QUERY)
        if not cands:
            raise PayloadError(f"mapsSearchV1 returned no candidates for {DOCTOR_QUERY!r}")
        return f"{len(cands)} candidate(s) for {DOCTOR_QUERY!r}; first: {cands[0].line1}"

    def s2() -> str:
        nonlocal first_store
        rows, meta = client.feed(DOCTOR_LOCATION, 1)
        if len(rows) < 20:
            raise PayloadError(f"only {len(rows)} stores parsed from the feed (expected ≥ 20)")
        first_store = next((r for r in rows if r.deals), rows[0])
        deals = sum(1 for r in rows if r.deals)
        return f"{len(rows)} stores near {DOCTOR_LOCATION.label}, {deals} with a public deal, currency {meta.currency}"

    def s3() -> str:
        if first_store is None:
            raise PayloadError("skipped: step 2 produced no store to open")
        store = client.menu(first_store.uuid, DOCTOR_LOCATION)
        if not store.dishes:
            raise PayloadError(f"{store.title}: menu parsed but holds 0 dishes")
        return (f"{store.title}: {len(store.dishes)} dishes, {store.duplicates_removed} duplicates removed, "
                f"{'open' if store.is_open else 'closed'}")

    step("mapsSearchV1", s1)
    step("getFeedV1", s2)          # independent of step 1: the doctor location is fixed
    step("getStoreV1", s3)         # s3 reports "skipped" itself when step 2 produced no store

    try:
        import requests  # noqa: F401
        have_requests = True
    except ImportError:
        have_requests = False
    report = {"transport": getattr(t, "transport_used", None), "tls_path": getattr(t, "tls_path", None),
              "requests_module": have_requests, "python": sys.version.split()[0],
              "steps": steps, **_usage(client)}
    if not args.json:
        print(f"python     {report['python']}   requests module {'present' if have_requests else 'absent (curl/urllib fallback)'}")
        print(f"transport  {report['transport'] or '—'}   tls_path {report['tls_path'] or '—'}")
        for s in steps:
            mark = "ok" if s["ok"] else ("BLOCKED" if s["blocked"] else "FAIL")
            print(f"  {_pad(s['name'], 22)} {_pad(mark, 8)} {s['detail']}")
        print(f"requests_used {t.requests_made}")
    if not healthy:
        blocked = [s["name"] for s in steps if s["blocked"]]
        failed = [s["name"] for s in steps if not s["ok"]]
        providers = sorted({s["provider"] for s in steps if s["blocked"]})
        who = " and ".join(_layer(p) for p in providers)
        msg = (f"{who} is challenging {', '.join(blocked)} — the site is refusing us, not the skill breaking"
               if blocked else f"step(s) failed: {', '.join(failed)}")
        return _fail(args, EXIT_NET, msg, "blocked" if blocked else "network", report)
    if not args.json:
        print("ok — place search, the feed and a menu all parse")
    return _emit(args, report, EXIT_OK)


# ---------------------------------------------------------------------------
# Argument wiring
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """Respects --json when rejecting arguments: the JSON error object, exit 2."""

    _argv: list[str] | None = None

    def error(self, message: str):
        argv = self._argv if self._argv is not None else sys.argv[1:]
        if "--json" in argv:
            command = next((a for a in argv if not a.startswith("-")), None)
            print(json.dumps(envelope(command, False, error=f"{self.prog}: {message}", kind="usage"),
                             indent=2, ensure_ascii=False))
            raise SystemExit(EXIT_USAGE)
        # Human mode: one stderr line, never the multi-line usage block (repo rule for exit 2/3).
        print(f"error: {self.prog}: {message} (see --help)", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _add_common(p: argparse.ArgumentParser, command: str) -> None:
    p.add_argument("--json", action="store_true", help="one JSON object on stdout (same exit codes)")
    p.add_argument("--max-requests", type=_budget, default=None, metavar="N",
                   help=f"refuse up front if the plan needs more than N requests "
                        f"(default: the plan itself, typically {DEFAULT_BUDGET[command]}; hard cap {HARD_CAP})")
    p.add_argument("--locale", type=_locale, default="ca", metavar="CC",
                   help="Uber localeCode (default ca; others unverified)")


def _add_at(p: argparse.ArgumentParser, required: bool) -> None:
    p.add_argument("--at", type=_text("--at"), required=required, metavar="ADDRESS|TOKEN",
                   help="delivery address (resolved via locate and cached 30 days) or a location token from locate"
                        + ("" if required else "; optional — without it ETA and distance are not shown"))


def _add_pickup(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pickup", action="store_true", help="pickup instead of delivery (diningMode PICKUP)")


def _add_pages(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pages", type=_bounded("--pages", 1, MAX_PAGES), default=1, metavar="N",
                   help=f"feed pages to read (default 1, max {MAX_PAGES}); each costs a request")


def _add_store_filters(p: argparse.ArgumentParser, km: bool = True, name: bool = True) -> None:
    p.add_argument("--min-rating", type=_positive("--min-rating", 5), metavar="R", help="keep stores rated R or better")
    p.add_argument("--max-eta", type=_bounded("--max-eta", 1), metavar="MIN", help="keep stores whose latest ETA is at most MIN minutes")
    if km:
        p.add_argument("--max-km", type=_positive("--max-km"), metavar="KM", help="keep stores within KM of the address")
    if name:
        p.add_argument("--name", type=_text("--name"), metavar="TEXT", help="keep stores whose name contains TEXT")


def _add_store_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("store", metavar="STORE", type=_text("STORE"),
                   help="a store UUID, its Uber Eats link (or the id segment of one), or a restaurant name "
                        "(a name needs --at and is looked up in the feed first)")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="ubereats",
                     description="Uber Eats menus, prices and public deals near an address. "
                                 "Read-only: it never carts, orders or logs in.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    p = sub.add_parser("locate", help="resolve an address to the location Uber will price against (2 requests)")
    p.add_argument("address", type=_text("ADDRESS"), metavar="ADDRESS")
    p.add_argument("--pick", type=_bounded("--pick", 1, 5), default=1, metavar="N", help="take the Nth candidate (default 1)")
    _add_common(p, "locate"); p.set_defaults(func=cmd_locate)

    p = sub.add_parser("nearby", help="what Uber's feed shows near an address")
    _add_at(p, True)
    p.add_argument("--deals", action="store_true", help="only stores carrying a public deal")
    p.add_argument("--deal-type", choices=DEAL_TYPE_CHOICES, metavar="TYPE",
                   help="only stores with this deal type: " + ", ".join(DEAL_TYPE_CHOICES))
    _add_store_filters(p)
    p.add_argument("--sort", choices=("feed", "rating", "eta", "distance"), default="feed",
                   help="row order (default feed = Uber's own order)")
    p.add_argument("--limit", type=_bounded("--limit", 1, MAX_LIMIT), default=DEFAULT_LIMIT_NEARBY, metavar="N",
                   help=f"rows shown (default {DEFAULT_LIMIT_NEARBY})")
    _add_pages(p); _add_common(p, "nearby"); p.set_defaults(func=cmd_nearby)

    p = sub.add_parser("find", help="which store in the feed is this restaurant (name matching, not search)")
    p.add_argument("name", type=_text("NAME"), metavar="NAME")
    _add_at(p, True); _add_pages(p); _add_common(p, "find"); p.set_defaults(func=cmd_find)

    p = sub.add_parser("menu", help="a store's menu with prices, sale prices and deals")
    _add_store_arg(p); _add_at(p, False); _add_pickup(p)
    p.add_argument("--under", type=_positive("--under"), metavar="PRICE", help="dishes whose (sale) price is at most PRICE")
    p.add_argument("--match", type=_text("--match"), metavar="TEXT", help="dishes whose title or description contains TEXT")
    p.add_argument("--section", type=_text("--section"), metavar="TEXT", help="only sections whose title contains TEXT")
    p.add_argument("--deals", action="store_true", help="only discounted / BOGO dishes")
    p.add_argument("--sold-out", action="store_true", help="include sold-out dishes (hidden but counted by default)")
    _add_common(p, "menu"); p.set_defaults(func=cmd_menu)

    p = sub.add_parser("item", help="one dish's option groups and add-on prices")
    _add_store_arg(p)
    p.add_argument("dish", type=_text("DISH"), metavar="DISH", help="the dish name as listed (exact beats partial)")
    _add_at(p, False); _add_pickup(p); _add_common(p, "item"); p.set_defaults(func=cmd_item)

    p = sub.add_parser("deals", help="every public deal in the feed near an address, grouped by type")
    _add_at(p, True)
    p.add_argument("--type", choices=DEAL_TYPE_CHOICES, metavar="TYPE", help="one deal type: " + ", ".join(DEAL_TYPE_CHOICES))
    _add_store_filters(p, name=False)
    p.add_argument("--min-spend-at-most", type=_positive("--min-spend-at-most"), metavar="AMOUNT",
                   help="keep deals with no minimum spend or one at most AMOUNT")
    p.add_argument("--limit", type=_bounded("--limit", 1, MAX_LIMIT), default=None, metavar="N",
                   help=f"stores shown (default {DEFAULT_LIMIT_DEALS}; {DEFAULT_LIMIT_DEALS_ITEMS} with --items)")
    p.add_argument("--items", action="store_true",
                   help="read each store's menu and list the dishes carrying the deal (one request per store, planned up front)")
    _add_pages(p); _add_common(p, "deals"); p.set_defaults(func=cmd_deals)

    p = sub.add_parser("compare", help="where a dish is cheapest across the nearest stores' menus")
    p.add_argument("dish", type=_text("DISH"), metavar="DISH")
    _add_at(p, True); _add_pickup(p)
    p.add_argument("--stores", type=_bounded("--stores", 1, MAX_STORES_COMPARE), default=DEFAULT_STORES_COMPARE, metavar="N",
                   help=f"nearest stores whose menus to read (default {DEFAULT_STORES_COMPARE}, max {MAX_STORES_COMPARE}; one request each)")
    _add_store_filters(p, km=False)
    p.add_argument("--deals", action="store_true", help="only stores carrying a public deal, before reading menus")
    _add_common(p, "compare"); p.set_defaults(func=cmd_compare)

    p = sub.add_parser("watch", help="one check of a condition on a store; exit 0 fired, 1 not yet")
    _add_store_arg(p); _add_at(p, False); _add_pickup(p)
    p.add_argument("--open", action="store_true", help="fire when the store is open and taking orders")
    p.add_argument("--deal", action="store_true", help="fire when any dish carries a deal (with --item: when that dish does)")
    p.add_argument("--item", type=_text("--item"), metavar="DISH", help="the dish to watch (with --under or --deal)")
    p.add_argument("--under", type=_positive("--under"), metavar="PRICE", help="with --item: fire when its sale price is at or under PRICE")
    p.add_argument("--back-in-stock", type=_text("--back-in-stock"), metavar="DISH", help="fire when this dish is no longer sold out")
    _add_common(p, "watch"); p.set_defaults(func=cmd_watch)

    p = sub.add_parser("doctor", help="is Uber blocking us? three fixed requests, exit 0 or 3")
    _add_common(p, "doctor"); p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = build_parser()
    _Parser._argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:              # argparse's own exits (help → 0, usage → 2)
        return int(e.code or 0)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Ambiguous as e:
        return _fail(args, EXIT_USAGE, str(e), "lookup",
                     {"candidates": [c.to_dict() for c in e.candidates]})
    except OutsideServiceArea as e:
        return _fail(args, EXIT_USAGE, str(e), "lookup")
    except LookupFailure as e:
        return _fail(args, EXIT_USAGE, f"Uber does not know that store id: {e}", "lookup")
    except (QueryError, RequestBudgetError) as e:
        return _fail(args, EXIT_USAGE, str(e), "usage")
    except Blocked as e:
        return _fail(args, EXIT_NET, str(e) or BLOCKED_MESSAGE, "blocked")
    except UEHTTPError as e:
        return _fail(args, EXIT_NET, str(e), "network")
    except PayloadError as e:
        return _fail(args, EXIT_NET, f"Uber's response did not have the expected shape: {e}", "payload")
    except NotAllowed as e:
        return _fail(args, EXIT_NET, f"refused to call a non-read-only endpoint: {e}", "internal")
    except Exception as e:               # never a traceback in an agent's transcript
        return _fail(args, EXIT_NET, f"unexpected {type(e).__name__}: {e}", "payload")


if __name__ == "__main__":
    sys.exit(main())
