"""Command line interface.

Every command answers a question a person actually asks, rather than wrapping
an endpoint. Exit codes are mapped in exactly one place - `main` - so no
command can invent its own convention:

    0  found bookable vehicles
    1  the query worked, nothing is bookable   <- the only "keep waiting" code
    2  usage error, ambiguous branch, age refusal, refused route
    3  network or API failure (including a TLS block)

Only 1 means try again later. An age refusal or a refused cross-border route
is a 2 precisely so `watch` cannot loop forever on a rental that can never be
booked.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from . import filters as vfilters
from .errors import (
    BotBlocked, EnterpriseError, LocationNotFound, TransportError, UsageError,
)
from .http import Transport
from .locations import LocationClient
from .model import (
    DRIVE_2WD, DRIVE_AWD, FUEL_DIESEL, FUEL_ELECTRIC, FUEL_HYBRID, FUEL_PETROL,
    Location, Quote, QuoteRequest, Vehicle, check_dates, format_price,
    parse_age_policy, parse_amount, parse_hours, parse_when,
)
from .rentals import MAX_WORKERS, Plan, RentalClient, date_windows

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _mileage(vehicle: Vehicle) -> str:
    """Never blank: a mileage cap changes which car you would recommend."""
    return "unlimited" if vehicle.unlimited_mileage else "CAPPED"


def _model(vehicle: Vehicle) -> str:
    """'or sim.' whenever the exact model is not guaranteed."""
    name = vehicle.model or vehicle.sub_category or vehicle.code
    return name if vehicle.guaranteed else f"{name} or sim."


def _no_match_reason(bookable: int) -> str:
    """Why a branch produced no row.

    "nothing bookable" is false when the branch has cars and a filter excluded
    them - that is how "Halifax has no cars" gets reported when Halifax has
    five.
    """
    return "nothing bookable" if bookable == 0 else f"no match ({bookable} bookable)"


def _transmission(vehicle: Vehicle) -> str:
    """Abbreviated, and translated-locale-proof via the code."""
    if vehicle.transmission_code == "25":
        return "auto"
    if vehicle.transmission_code == "26":
        return "MANUAL"
    return (vehicle.transmission or "-")[:6]


def _vehicle_row(vehicle: Vehicle, days: int) -> tuple[str, ...]:
    per_day = vehicle.per_day(days)
    return (
        vehicle.code,
        _model(vehicle)[:34],
        str(vehicle.seats or "-"),
        str(vehicle.bags or "-"),
        _drive_label(vehicle.drive, vehicle.drive_code),
        _fuel_label(vehicle.fuel, vehicle.fuel_code),
        _transmission(vehicle),
        _mileage(vehicle),
        str(per_day) if per_day else "-",
        format_price(vehicle.total),
    )


VEHICLE_HEADERS = (
    "CLASS", "VEHICLE", "SEAT", "BAG", "DRIVE", "FUEL", "TRANS", "MILEAGE",
    "PER DAY", "TOTAL",
)


def _width(text: str) -> int:
    """Display width, counting CJK and other wide glyphs as two columns.

    `len()` treats a full-width character as one column, so a Tokyo branch list
    skews every column to its right.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return fill + text if right else text + fill


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(str(cell)))
    # Numeric-ish trailing columns read better right-aligned.
    def line(cells: Sequence[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(_pad(str(cell), widths[i], right=i >= len(widths) - 2))
        return "  ".join(out).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def _drive_label(value: str | None, code: str | None = None) -> str:
    """Abbreviate by CODE, so it works in every locale.

    Matching the English words left German rows printing
    "Vierrad- oder Allradantrieb" in full, widening the whole table.
    """
    if code == DRIVE_AWD:
        return "4WD/AWD"
    if code == DRIVE_2WD:
        return "2WD"
    if not value:
        return "-"
    low = value.lower()
    if "4 wheel" in low:
        return "4WD/AWD"
    if "2 wheel" in low:
        return "2WD"
    return value[:9]


def _fuel_label(value: str | None, code: str | None = None) -> str:
    """Fuel, short and readable in any language.

    A blind 8-character slice turned `Benzinfahrzeug` into `Benzinfa`, which
    reads as corrupt data rather than as an abbreviation.
    """
    by_code = {
        FUEL_PETROL: "petrol", FUEL_DIESEL: "diesel",
        FUEL_HYBRID: "hybrid", FUEL_ELECTRIC: "electric",
    }
    if code in by_code:
        return by_code[code]
    if not value:
        return "-"
    text = value.replace(" Vehicle", "")
    return text if len(text) <= 9 else text[:8] + "\u2026"


def _render_quote(
    quote: Quote, shown: list[Vehicle], args, matched: int | None = None,
    **extra: Any,
) -> str:
    request = quote.request
    where = ", ".join(x for x in (request.pickup.city, request.pickup.country) if x)
    head = [
        f"{quote.pickup_label}  [id {request.pickup.id}]"
        + (f"   {where}" if where else ""),
    ]
    if request.is_one_way:
        head.append(f"  returning to {request.dropoff.label} [id {request.dropoff.id}]")
    head.append(
        f"  {request.pickup_time.replace('T', ' ')} -> "
        f"{request.return_time.replace('T', ' ')}   renter age {request.age}"
    )
    if request.pickup.is_exotic:
        head.append("  NOTE: this is an Exotic branch - a different, pricier fleet.")

    days = request.rental_days
    rows = [_vehicle_row(vehicle, days) for vehicle in shown]

    body = _table(VEHICLE_HEADERS, rows) if rows else "  (nothing matches)"

    matched = len(shown) if matched is None else matched
    note = f"{len(quote.bookable)} of {len(quote.vehicles)} classes bookable"
    if matched != len(quote.bookable):
        note += f"; {matched} match the filters"
    if len(shown) != matched:
        note += f"; showing {len(shown)} (--limit)"
    footer = [note]
    sold_out = quote.sold_out_categories
    if sold_out:
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(sold_out.items()))
        footer.append(f"sold out: {detail}")
    if quote.restricted:
        # Otherwise these vanish from the arithmetic: bookable + sold out
        # would not add up to the total and the reader cannot tell why.
        footer.append(
            f"{len(quote.restricted)} class(es) restricted at this branch "
            f"(not bookable online): "
            + ", ".join(v.code for v in quote.restricted[:6])
        )
    active = vfilters.describe(args, **extra)
    if active:
        footer.append(f"filters: {active}")
    footer.append(
        f"PER DAY is the trip total divided by {days} rental day(s), not the "
        f"API's own rate line"
    )
    odd = {v.rate_note for v in quote.bookable if v.rate_note}
    if odd:
        # The API sometimes prices in weeks; say so rather than hiding it.
        footer.append(
            f"note: Enterprise quotes some of these at a {'/'.join(sorted(odd))} "
            f"rate, not a daily one"
        )
    footer.append("prices are what the branch charges; quotes are live and move")

    return "\n".join(head) + "\n\n" + body + "\n\n" + "\n".join(f"  {f}" for f in footer)


def _vehicle_json(vehicle: Vehicle, days: int = 1) -> dict:
    def money(price) -> dict | None:
        if price is None:
            return None
        out = {
            "amount": str(price.charged.amount),
            "currency": price.charged.currency,
        }
        if price.converted is not None:
            out["converted_estimate"] = {
                "amount": str(price.converted.amount),
                "currency": price.converted.currency,
                "note": "display conversion only; not the amount charged",
            }
        return out

    return {
        "class_code": vehicle.code,
        "model": vehicle.model,
        "guaranteed_model": vehicle.guaranteed,
        "category": vehicle.category,
        # Codes as well as names: the names are translated per locale, so a
        # caller diagnosing a filter needs the code the filter actually matches.
        "category_code": vehicle.category_code,
        "sub_category": vehicle.sub_category,
        "seats": vehicle.seats,
        "luggage": vehicle.bags,
        "luggage_small": vehicle.small_bags,
        "luggage_large": vehicle.large_bags,
        "drive": vehicle.drive,
        "drive_code": vehicle.drive_code,
        "fuel": vehicle.fuel,
        "fuel_code": vehicle.fuel_code,
        "transmission": vehicle.transmission,
        "transmission_code": vehicle.transmission_code,
        "unlimited_mileage": vehicle.unlimited_mileage,
        "fuel_consumption_l_per_100km": vehicle.fuel_consumption,
        "status": vehicle.status.value,
        "bookable": vehicle.bookable,
        "total_charged": money(vehicle.total),
        # The API's own rate line, WITH its period - it is not always daily.
        "api_rate": money(vehicle.daily),
        "api_rate_period": vehicle.rate_period,
        "api_rate_quantity": vehicle.rate_quantity,
        # >1 means the API priced this across several lines (e.g. WEEKLY x4 +
        # EXTRA_DAILY x2), so api_rate is only the first of them.
        "api_rate_lines": vehicle.rate_lines,
        # Points are PER DAY, and the API never says how many days a
        # redemption covers - so a trip-level points cost is not derivable and
        # any "cents per point" figure would be invented. Report the raw
        # per-day number and say what it is.
        "points_per_day": vehicle.points_per_day,
        "points_note": (
            "points are a PER-DAY redemption rate; the API does not state how "
            "many days a redemption covers, so the trip points cost cannot be "
            "computed from this response"
            if vehicle.points_per_day else None
        ),
        "rental_days": days,
        # Trip total / rental days. The only daily figure that is always what
        # the renter pays per day.
        "per_day_effective": (
            {"amount": str(vehicle.per_day(days).amount),
             "currency": vehicle.per_day(days).currency}
            if vehicle.per_day(days) else None
        ),
    }


def _location_json(location: Location) -> dict:
    return {
        "id": location.id,
        "name": location.name,
        "kind": location.kind,
        "airport_code": location.airport_code,
        "city": location.city,
        "country": location.country,
        "currency": location.currency,
        "is_exotic": location.is_exotic,
        "bookable": location.bookable,
    }


def _emit(args, payload: Any, human: str, found: bool) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(human)
    return EXIT_OK if found else EXIT_NONE


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _clients(args) -> tuple[LocationClient, RentalClient]:
    transport = Transport(verbose=getattr(args, "verbose", False))
    locations = LocationClient(
        transport,
        brand=args.brand,
        country=args.country,
        locale=args.locale,
        use_cache=not args.no_cache,
    )
    rentals = RentalClient(transport, locale=args.locale, brand=args.brand)
    return locations, rentals


def _require_priceable_brand(args) -> None:
    """Refuse a brand this tool cannot actually price.

    The location service accepts NATIONAL and ALAMO, but their pricing hosts
    were never found, so a quote silently falls back to an unhelpful empty
    response. Better to say why than to let the user think there are no cars.
    """
    brand = (args.brand or "ENTERPRISE").upper()
    if brand != "ENTERPRISE":
        raise UsageError(
            f"--brand {brand} works for `locations` but not for pricing: only "
            f"Enterprise's pricing host is known. Drop --brand to quote, or "
            f"use `locations --brand {brand}` to look branches up."
        )


def _build_request(args, pickup: Location, dropoff: Location, age: int) -> QuoteRequest:
    pickup_time = parse_when(args.pickup_time)
    return_time = parse_when(args.return_time)
    check_dates(pickup_time, return_time)
    return QuoteRequest(
        pickup=pickup,
        dropoff=dropoff,
        pickup_time=pickup_time,
        return_time=return_time,
        age=age,
        residency=args.residency or args.country,
        currency=args.currency,
    )


def _validated_window(args) -> tuple[str, str]:
    """Parse and sanity-check the date pair before anything hits the network.

    Branch resolution is itself an HTTP call, so validating dates only inside
    `_build_request` - which runs after `resolve()` - still spent a request on
    a query that could never succeed, and reported the branch error instead of
    the date error.
    """
    pickup_time = parse_when(args.pickup_time)
    return_time = parse_when(args.return_time)
    check_dates(pickup_time, return_time)
    return pickup_time, return_time


def _warn_country(client: LocationClient, *branches: Location) -> None:
    """Say so, loudly, when a resolved branch is not in the requested country.

    `--country` is a search hint the service does not enforce: asking for
    Australia returns Sydney, Nova Scotia, and nothing else in the output would
    reveal the wrong hemisphere. The comparison itself lives on the client, so
    there is one definition of "mismatch" rather than two that can drift.
    """
    seen: set[str] = set()
    for branch in branches:
        # A round trip passes the same branch twice; warn once.
        if branch.id in seen:
            continue
        seen.add(branch.id)
        actual = client.country_mismatch(branch)
        if actual:
            print(
                f"  WARNING: you asked for {client.country.upper()} but "
                f"{branch.label} [id {branch.id}] is in {actual}"
                + (f" ({branch.city})" if branch.city else "")
                + ". --country is a search hint, not a filter.",
                file=sys.stderr,
            )
        elif branch.country is None:
            # "Cannot tell" is not "matches" - say which it is.
            print(
                f"  NOTE: could not confirm the country of {branch.label} "
                f"[id {branch.id}].",
                file=sys.stderr,
            )


def _ages(args, *, multi: bool = False) -> list[int]:
    """Renter ages, defaulting to 25.

    Only `quote` prices several ages. The others take the first, so accepting a
    repeated `--age` there and silently dropping the rest would report one age's
    prices as if they were a comparison. Refuse instead.
    """
    values = args.age or [25]
    if len(values) > 1 and not multi:
        raise UsageError(
            f"--age may only be repeated on `quote`, which prices each age "
            f"side by side. `{args.command}` uses a single age - pass one, or "
            f"run `quote` twice."
        )
    for age in values:
        if age < 15 or age > 99:
            raise UsageError(f"implausible renter age {age}")
    return values


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_locations(args) -> int:
    locations, _ = _clients(args)
    found = locations.search(args.query)
    if not found:
        # A typo is a lookup error. Exit 1 would tell a retry loop to wait for
        # a branch that will never appear.
        raise LocationNotFound(args.query)

    shown = found[: args.limit]
    rows = [
        (
            c.id,
            c.airport_code or "-",
            c.name[:40] + ("  [EXOTIC]" if c.is_exotic else ""),
            c.kind,
            c.city or "-",
            # The column that stops "Sydney" being quoted in Nova Scotia.
            c.country or "?",
            c.currency or "-",
        )
        for c in shown
    ]
    notes = [f"{len(found)} match(es)"]
    if len(shown) < len(found):
        notes.append(f"showing {len(shown)} (--limit)")
    notes.append("check the CTRY column before quoting")
    wanted = (args.country or "").upper()
    if wanted and any((c.country or "").upper() != wanted for c in shown):
        # --country is a search hint, not a filter: the service happily returns
        # branches elsewhere, which is how "Sydney AU" resolves to Canada.
        notes.append(
            f"WARNING: some matches are not in {wanted} - --country is a search "
            f"hint, not a filter"
        )
    human = _table(
        ("ID", "CODE", "NAME", "KIND", "CITY", "CTRY", "CUR"), rows
    ) + "\n\n  " + "\n  ".join(notes)
    payload = [
        dict(_location_json(c), matches_total=len(found), matches_shown=len(shown))
        for c in shown
    ]
    return _emit(args, payload, human, True)


def cmd_quote(args) -> int:
    _validated_window(args)
    _require_priceable_brand(args)
    locations, rentals = _clients(args)
    pickup = locations.resolve(args.pickup)
    dropoff = locations.resolve(args.dropoff) if args.dropoff else pickup
    _warn_country(locations, pickup, dropoff)
    ages = _ages(args, multi=True)

    blocks: list[str] = []
    payload: list[dict] = []
    results: list[tuple[int, Quote, list[Vehicle]]] = []

    refusals: list[tuple[int, str]] = []
    for age in ages:
        request = _build_request(args, pickup, dropoff, age)
        try:
            quote = rentals.quote(request)
        except UsageError as exc:
            # One refused age must not throw away the ages that succeeded -
            # "your 19-year-old cannot rent, but you can, for $436" is the
            # useful answer.
            refusals.append((age, str(exc)))
            blocks.append(f"=== renter age {age} ===\n  refused: {exc}")
            continue
        matching = vfilters.apply(quote.bookable, vfilters.build(args))
        shown = matching[: args.limit]
        results.append((age, quote, matching))
        header = f"=== renter age {age} ===\n" if len(ages) > 1 else ""
        blocks.append(header + _render_quote(quote, shown, args, matched=len(matching)))
        payload.append(
            {
                "renter_age": age,
                "pickup": _location_json(pickup),
                "dropoff": _location_json(dropoff),
                "country_requested": (args.country or "").upper() or None,
                "country_mismatch": locations.country_mismatch(pickup),
                "pickup_time": request.pickup_time,
                "return_time": request.return_time,
                "classes_total": len(quote.vehicles),
                "classes_bookable": len(quote.bookable),
                "classes_matching_filters": len(matching),
                "vehicles_shown": len(shown),
                "sold_out_by_category": quote.sold_out_categories,
                "restricted_classes": [v.code for v in quote.restricted],
                "error": None,
                "vehicles": [_vehicle_json(v, request.rental_days) for v in shown],
            }
        )

    if not results and refusals:
        # Every age was refused: the query cannot succeed as asked.
        raise UsageError(refusals[0][1])

    for age, message in refusals:
        payload.append({
            "renter_age": age,
            "pickup": _location_json(pickup),
            "dropoff": _location_json(dropoff),
            "country_requested": (args.country or "").upper() or None,
            "country_mismatch": locations.country_mismatch(pickup),
            "pickup_time": parse_when(args.pickup_time),
            "return_time": parse_when(args.return_time),
            "classes_total": 0, "classes_bookable": 0,
            "classes_matching_filters": 0, "vehicles_shown": 0,
            "sold_out_by_category": {}, "restricted_classes": [],
            "vehicles": [], "error": message.split("\n")[0],
        })
    if len(ages) > 1:
        blocks.append(_age_summary(results, refusals))
    return _emit(args, payload, "\n\n".join(blocks), any(m for _, _, m in results))


def _age_summary(
    results: list[tuple[int, Quote, list[Vehicle]]],
    refusals: list[tuple[int, str]] = (),
) -> str:
    """Under-25 is a fleet restriction as well as a surcharge - show both.

    Reporting only the price difference misses the bigger story: at Halifax,
    a 21-year-old loses seven classes outright, not just money.
    """
    lines = ["  Age comparison:"]
    for age, quote, matching in results:
        cheapest = matching[0] if matching else None
        price = (
            str(cheapest.total.charged) if cheapest and cheapest.total
            else _no_match_reason(len(quote.bookable))
        )
        lines.append(
            f"    age {age:>2}: "
            f"{len(quote.bookable):>2} of {len(quote.vehicles)} classes, "
            f"cheapest {price}"
        )
    for age, _ in refusals:
        lines.append(f"    age {age:>2}: refused - cannot rent at this branch")
    return "\n".join(lines)


def cmd_sweep(args) -> int:
    # Validate the plan before spending a network call on branch lookup.
    windows = date_windows(args.start, args.end, args.nights, args.step)
    # Check the extremes before fanning out: a typo'd year would otherwise send
    # up to --max-requests pricing calls at a horizon the API refuses one by
    # one, against a host with an unwarned volumetric limit.
    for start, end in (windows[0], windows[-1]):
        check_dates(parse_when(f"{start}T{args.time}"),
                    parse_when(f"{end}T{args.time}"))
    _require_priceable_brand(args)
    locations, rentals = _clients(args)
    pickup = locations.resolve(args.pickup)
    dropoff = locations.resolve(args.dropoff) if args.dropoff else pickup
    _warn_country(locations, pickup, dropoff)

    plan = Plan(len(windows), f"{len(windows)} window(s) of {args.nights} night(s)")
    plan.check(args.max_requests)
    print(f"  plan: {plan.describe()}", file=sys.stderr)

    age = _ages(args)[0]
    predicates = vfilters.build(args)
    results: list[tuple[str, str, Vehicle | None, int]] = []
    problems: list[tuple[str, str]] = []
    refusals: list[str] = []

    def collect(request: QuoteRequest, outcome) -> None:
        if isinstance(outcome, Exception):
            problems.append((request.pickup_time[:10], str(outcome).split("\n")[0]))
            if isinstance(outcome, UsageError):
                refusals.append(str(outcome))
            return
        matching = vfilters.apply(outcome.bookable, predicates)
        results.append(
            (request.pickup_time[:10], request.return_time[:10],
             matching[0] if matching else None, len(outcome.bookable))
        )

    requests = [
        QuoteRequest(
            pickup=pickup, dropoff=dropoff,
            pickup_time=parse_when(f"{start}T{args.time}"),
            return_time=parse_when(f"{end}T{args.time}"),
            age=age, residency=args.residency or args.country,
            currency=args.currency,
        )
        for start, end in windows
    ]
    rentals.map_quotes(requests, on_result=collect, workers=args.workers)
    results.sort(key=lambda r: r[0])

    priced = [r for r in results if r[2] is not None]
    priced.sort(key=lambda r: r[2].total.sort_key)  # type: ignore[union-attr]

    shown = priced[: args.limit]
    rows = [
        (start, end, v.code, _model(v)[:30], _mileage(v), format_price(v.total))
        for start, end, v, _ in shown
    ]
    # Name the branch: a sweep of an Exotic id returns a very different fleet,
    # and without a header the output is indistinguishable from the mainstream
    # branch's.
    header = f"{pickup.label}  [id {pickup.id}]"
    if dropoff.id != pickup.id:
        header += f"  returning to {dropoff.label} [id {dropoff.id}]"
    if pickup.is_exotic:
        header += "\n  NOTE: this is an Exotic branch - a different, pricier fleet."
    human = header + "\n\n" + _table(
        ("PICKUP", "RETURN", "CLASS", "VEHICLE", "MILEAGE", "TOTAL"), rows
    )
    # Nothing came back at all. Exit 1 would tell a polling caller to keep
    # waiting, so distinguish the two reasons it can happen:
    #   a refusal (age, booking horizon, refused route) can NEVER succeed -> 2
    #   an outage might succeed later, but is still not "no cars"        -> 3
    if not results and problems:
        if refusals:
            return _fail(
                args, EXIT_USAGE,
                f"every window was refused, so no date in this range can "
                f"work:\n{refusals[0]}",
            )
        return _fail(
            args, EXIT_NET,
            f"all {len(problems)} window(s) failed; first: "
            f"{problems[0][0]}: {problems[0][1]}",
        )

    footer = [f"{len(priced)} of {len(windows)} window(s) had a match"]
    if len(priced) > args.limit:
        footer.append(
            f"showing {args.limit} of {len(priced)} priced window(s) (--limit)"
        )
    if priced:
        footer.append(f"cheapest: {priced[0][0]} -> {priced[0][1]}")
    # Name the windows that came back empty. Without this a window simply
    # vanishes from the table, and the reader cannot tell a genuine sell-out
    # from one that was never checked - while being told the whole range was.
    active = vfilters.describe(args)
    if active:
        footer.append(f"filters: {active}")
    empty = [start for start, _, vehicle, _ in results if vehicle is None]
    if empty:
        listed = ", ".join(empty[:6]) + ("..." if len(empty) > 6 else "")
        footer.append(
            f"{len(empty)} window(s) returned nothing matching (checked, not "
            f"skipped): {listed}"
        )
    if problems:
        footer.append(
            f"{len(problems)} window(s) failed: {problems[0][0]}: {problems[0][1]}"
        )
    if len(results) < len(windows):
        footer.append(
            f"PARTIAL: swept {len(results)} of {len(windows)} windows - the "
            f"cheapest may not have been seen"
        )

    # One base shape for every row: a consumer must never have to ask which
    # outcome a row represents before it can read a key.
    base = {
        "windows_planned": len(windows),
        "windows_priced": len(priced),
        "windows_shown": len(shown),
        "branch": _location_json(pickup),
        "country_requested": (args.country or "").upper() or None,
        "country_mismatch": locations.country_mismatch(pickup),
        "dropoff": _location_json(dropoff) if dropoff.id != pickup.id else None,
        "pickup_date": None, "return_date": None,
        "classes_bookable": 0, "cheapest": None,
        "error": None, "note": None,
    }
    payload = [
        {
            **base,
            "pickup_date": start, "return_date": end,
            "classes_bookable": count,
            "cheapest": _vehicle_json(v, args.nights) if v else None,
        }
        for start, end, v, count in shown
    ]
    payload += [
        {
            **base,
            "pickup_date": start, "return_date": end,
            "classes_bookable": count,
            "note": "checked; nothing matched the filters",
        }
        for start, end, vehicle, count in results if vehicle is None
    ]
    # Failures ride in the same array so a --json consumer cannot miss them.
    payload += [
        {**base, "pickup_date": date, "error": message}
        for date, message in problems
    ]
    return _emit(
        args, payload, human + "\n\n" + "\n".join(f"  {f}" for f in footer), bool(priced)
    )


def cmd_compare(args) -> int:
    _validated_window(args)
    _require_priceable_brand(args)
    locations, rentals = _clients(args)
    branches = [locations.resolve(q) for q in args.pickups]
    _warn_country(locations, *branches)
    dropoff_for = {b.id: b for b in branches}

    plan = Plan(len(branches), f"{len(branches)} branch(es)")
    plan.check(args.max_requests)
    print(f"  plan: {plan.describe()}", file=sys.stderr)

    age = _ages(args)[0]
    predicates = vfilters.build(args)
    rows: list[tuple[Location, Vehicle | None, int, str | None]] = []
    compare_refusals: list[str] = []

    def collect(request: QuoteRequest, outcome) -> None:
        if isinstance(outcome, Exception):
            rows.append((request.pickup, None, 0, str(outcome).split("\n")[0]))
            if isinstance(outcome, UsageError):
                compare_refusals.append(str(outcome))
            return
        matching = vfilters.apply(outcome.bookable, predicates)
        resolved = request.pickup
        if outcome.branch_name and resolved.name.startswith("Location "):
            resolved = replace(resolved, name=outcome.branch_name)
        rows.append(
            (resolved, matching[0] if matching else None,
             len(outcome.bookable), None)
        )

    requests = [
        _build_request(args, branch, dropoff_for[branch.id], age) for branch in branches
    ]
    rentals.map_quotes(requests, on_result=collect, workers=args.workers)

    if rows and all(note is not None for _, _, _, note in rows):
        code = EXIT_USAGE if compare_refusals else EXIT_NET
        return _fail(
            args, code,
            f"all {len(rows)} branch(es) failed; first: {rows[0][3]}",
        )

    currencies = {
        r[1].total.charged.currency for r in rows if r[1] and r[1].total
    }
    # Duration only - deliberately not via _build_request, which also emits the
    # country warning and would double it.
    days = QuoteRequest(
        pickup=branches[0], dropoff=branches[0],
        pickup_time=parse_when(args.pickup_time),
        return_time=parse_when(args.return_time),
    ).rental_days if branches else 1
    priced_rows = [r for r in rows if r[3] is None]
    failed_rows = [r for r in rows if r[3] is not None]
    ranked = sorted(
        priced_rows,
        key=lambda r: r[1].total.sort_key if r[1] and r[1].total else Decimal("inf"),
    )[: args.limit] + failed_rows

    table_rows = [
        (
            branch.airport_code or branch.id,
            branch.name[:26],
            branch.country or "?",
            vehicle.code if vehicle else "-",
            _model(vehicle)[:24] if vehicle
            else (note or _no_match_reason(count)),
            f"{vehicle.seats}/{vehicle.bags}" if vehicle else "-",
            _mileage(vehicle) if vehicle else "-",
            format_price(vehicle.total) if vehicle else "-",
        )
        # `ranked` already truncates priced rows and keeps every failure, so
        # the table and the payload are built from the same list. Building them
        # separately is what let them disagree.
        for branch, vehicle, count, note in ranked
    ]
    human = _table(
        ("CODE", "BRANCH", "CTRY", "CLASS", "VEHICLE", "SEAT/BAG", "MILEAGE", "TOTAL"),
        table_rows,
    )
    footer = []
    active = vfilters.describe(args)
    if active:
        footer.append(f"filters: {active}")
    failed = [(b, note) for b, _, _, note in rows if note]
    if failed:
        # Never present a comparison built on half the branches as if it were
        # complete.
        footer.append(
            f"{len(failed)} of {len(rows)} branch(es) could not be priced: "
            + ", ".join(f"{b.airport_code or b.id}" for b, _ in failed[:5])
        )
    if len(ranked) < len(rows):
        footer.append(
            f"showing {len(ranked)} of {len(rows)} branch(es) (--limit; "
            f"failures are always shown)"
        )
    # Each branch's cheapest class may be a different vehicle entirely - a
    # mystery compact against a minivan is not an airport-vs-downtown answer.
    classes = {v.code for _, v, _, _ in rows if v}
    if len(classes) > 1:
        footer.append(
            "NOT like-for-like: each row is that branch's cheapest MATCHING "
            "class, and they differ. Add filters (e.g. --class suv --seats 5) "
            "to compare comparable vehicles."
        )
    if len(currencies) > 1:
        # Ranking 302.80 USD against 436.00 CAD by raw number is nonsense.
        footer.append(
            f"MIXED CURRENCIES ({', '.join(sorted(currencies))}) - rows are NOT "
            f"directly comparable; convert before naming a winner"
        )
    footer.append("prices are what each branch charges, in its own currency")

    payload = [
        {
            "branches_total": len(rows),
            "branches_shown": len(ranked),
            "branches_failed": len(failed_rows),
            "branch": _location_json(b),
            "country_requested": (args.country or "").upper() or None,
            "country_mismatch": locations.country_mismatch(b),
            "classes_bookable": n,
            "cheapest": _vehicle_json(v, days) if v else None,
            "error": note,
            # Mirrors the table's warning so a JSON consumer cannot rank
            # 302.80 USD above 436.00 CAD by raw number.
            "currencies_mixed": len(currencies) > 1,
            "currencies": sorted(currencies),
            # Mirrored for the same reason as currencies_mixed: a JSON consumer
            # ranking these rows needs the vehicle guard as well as the
            # currency one.
            "like_for_like": len(classes) <= 1,
        }
        for b, v, n, note in ranked
    ]
    return _emit(
        args, payload, human + "\n\n" + "\n".join(f"  {f}" for f in footer),
        any(r[1] for r in rows),
    )


def cmd_watch(args) -> int:
    _validated_window(args)
    _require_priceable_brand(args)
    locations, rentals = _clients(args)
    pickup = locations.resolve(args.pickup)
    dropoff = locations.resolve(args.dropoff) if args.dropoff else pickup
    _warn_country(locations, pickup, dropoff)
    request = _build_request(args, pickup, dropoff, _ages(args)[0])

    if request.maybe_cross_border:
        # Cross-border one-way is usually refused outright; polling it would
        # never succeed.
        raise UsageError(
            "refusing to watch a one-way that is (or may be) cross-border - "
            "Enterprise generally does not permit these, so a watch would "
            "poll a route that can never succeed. Quote it once to confirm."
        )

    predicates = vfilters.build(args)
    deadline = time.time() + args.for_minutes * 60
    attempt = 0
    while True:
        attempt += 1
        quote = rentals.quote(request)
        matching = vfilters.apply(quote.bookable, predicates)
        if args.below is not None:
            matching = [
                v for v in matching
                if v.total is not None and v.total.sort_key <= args.below
            ]
        if matching:
            human = _render_quote(
                quote, matching[: args.limit], args, matched=len(matching),
                below=args.below,
            )
            return _emit(
                args,
                [_vehicle_json(v, request.rental_days)
                 for v in matching[: args.limit]],
                f"MATCH on check {attempt}\n\n{human}", True,
            )
        if time.time() >= deadline or attempt >= args.checks:
            break
        remaining = max(0, deadline - time.time())
        if not args.json:
            print(
                f"  check {attempt}: nothing yet "
                f"({len(quote.bookable)} bookable); next in {args.every}min",
                file=sys.stderr,
            )
        time.sleep(min(args.every * 60, remaining))

    return _emit(
        args, [],
        f"no match after {attempt} check(s)"
        + (f" against {vfilters.describe(args, below=args.below)}"
           if vfilters.describe(args, below=args.below) else "")
        + f". {len(quote.bookable)} class(es) were bookable. Exit 1 means keep "
        f"waiting - re-run to continue.",
        False,
    )


def cmd_branch(args) -> int:
    """Hours, age rules and terms - the three things that stop a collection."""
    # Validate the date before resolving the branch: resolution is itself an
    # HTTP call, so checking afterwards still spent one to be told the date was
    # unparseable - and in the API's words rather than ours.
    today = (
        parse_when(args.date)[:10] if args.date
        else datetime.now().strftime("%Y-%m-%d")
    )
    locations, rentals = _clients(args)
    branch = locations.resolve(args.pickup)
    _warn_country(locations, branch)

    days = parse_hours(locations.hours(branch.id, today))
    age_note = parse_age_policy(locations.renter_ages(branch.id))

    lines = [
        f"{branch.label}  [id {branch.id}]",
        f"  {branch.city or '-'}, {branch.country or '-'}   "
        f"billing currency {branch.currency or 'unknown'}",
    ]
    if branch.phone:
        lines.append(f"  phone {branch.phone}")
    if branch.is_exotic:
        lines.append("  NOTE: Exotic branch - different fleet, different prices.")

    shown = days[: args.limit]
    if len(days) > len(shown):
        lines.append(
            f"  showing {len(shown)} of {len(days)} date(s) (--limit)"
        )
    if shown:
        lines.append("")
        lines.append(_table(
            ("DATE", "COUNTER OPEN", "AFTER-HOURS DROP"),
            [(d.date, d.counter, d.drop) for d in shown],
        ))
    lines.append("")
    lines.append(f"  {age_note}")

    payload = {
        "branch": _location_json(branch),
        "dates_total": len(days),
        "dates_shown": len(shown),
        "hours": [
            {"date": d.date, "counter": d.counter, "after_hours_drop": d.drop}
            for d in shown
        ],
        "age_policy": age_note,
    }
    return _emit(args, payload, "\n".join(lines), True)


def cmd_doctor(args) -> int:
    """Prove the environment works, so a failure is never read as 'no cars'."""
    report: dict[str, Any] = {}

    def say(label: str, value: str, key: str | None = None) -> None:
        report[key or label.strip()] = value
        if not args.json:
            print(f"  {label:<13} {value}")

    def finish(code: int) -> int:
        report["ok"] = code == EXIT_OK
        if args.json:
            print(json.dumps(report, indent=2))
        elif code == EXIT_OK:
            print("\n  All checks passed.")
        return code

    if not args.json:
        print("enterprise doctor")
    say("python", sys.version.split()[0])
    try:
        import requests  # noqa: F401
        say("requests", "installed")
    except ImportError:
        say("requests", "MISSING - run: python3 -m pip install -q requests")
        return finish(EXIT_NET)
    import ssl
    say("openssl", ssl.OPENSSL_VERSION)
    from . import cache as _cache
    say("cache dir", _cache.describe(), "cache_directory")

    transport = Transport(verbose=True)
    locations = LocationClient(transport, use_cache=False)
    try:
        found = locations.search("Halifax")
        say("location API", f"OK ({len(found)} results)", "location_api")
    except EnterpriseError as exc:
        say("location API", f"FAILED: {exc}", "location_api")
        return finish(EXIT_NET)

    rentals = RentalClient(transport)
    branch = next((c for c in found if c.airport_code and not c.is_exotic), None)
    if branch is None:
        say("pricing API", "SKIPPED (no airport branch found)", "pricing_api")
        return finish(EXIT_NET)
    # ~5 weeks out: comfortably inside the 395-day booking horizon and far
    # enough ahead that a branch is unlikely to be entirely sold out.
    from datetime import timedelta
    start = datetime.now() + timedelta(days=35)
    request = QuoteRequest(
        pickup=branch, dropoff=branch,
        pickup_time=start.strftime("%Y-%m-%dT10:00"),
        return_time=(start + timedelta(days=3)).strftime("%Y-%m-%dT10:00"),
    )
    try:
        quote = rentals.quote(request)
    except EnterpriseError as exc:
        say("pricing API", f"FAILED: {exc}", "pricing_api")
        say("transport", transport.transport_used)
        return finish(EXIT_NET)
    say("pricing API",
        f"OK ({len(quote.bookable)} of {len(quote.vehicles)} classes bookable "
        f"at {branch.label})", "pricing_api")
    say("transport", transport.transport_used)
    return finish(EXIT_OK)


def cmd_cache(args) -> int:
    from . import cache as _cache
    message = _cache.clear() if args.clear else f"cache directory: {_cache.describe()}"
    if args.json:
        print(json.dumps({"cache_directory": _cache.describe(),
                          "cleared": bool(args.clear), "message": message}, indent=2))
    else:
        print(message)
    return EXIT_OK


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _money_arg(text: str) -> Decimal:
    """argparse wrapper around `model.parse_amount`.

    Named `amount` below so argparse's own message reads "invalid amount
    value" rather than leaking the function name.
    """
    try:
        return parse_amount(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


_money_arg.__name__ = "amount"


def _positive(text: str) -> int:
    """argparse type for counts that are meaningless at zero or below."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _add_geo(parser) -> None:
    group = parser.add_argument_group("locale and currency")
    group.add_argument("--country", default="CA", metavar="CC",
                       help="country whose branches to search (default CA)")
    group.add_argument("--residency", metavar="CC",
                       help="renter's country of residence (defaults to --country)")
    group.add_argument("--currency", metavar="CUR",
                       help="display currency; the branch still bills in its own")
    group.add_argument("--locale", default="en_CA", metavar="LOC")
    group.add_argument("--brand", default="ENTERPRISE", metavar="BRAND")


def _add_rental(parser, *, dates: bool = True) -> None:
    parser.add_argument("pickup", help="branch name, airport code, or numeric id")
    parser.add_argument("--dropoff", metavar="LOC",
                        help="return to a different branch (one-way)")
    parser.add_argument("--age", type=int, action="append", metavar="N",
                        help="renter age (default 25). Repeat to compare ages.")
    if dates:
        parser.add_argument("--pickup-time", required=True, metavar="WHEN",
                            help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
        parser.add_argument("--return-time", required=True, metavar="WHEN")


def _add_output(parser) -> None:
    group = parser.add_argument_group("output")
    group.add_argument("--json", action="store_true", help="machine-readable output")
    group.add_argument("--limit", type=_positive, default=20, metavar="N",
                       help="maximum rows to show (default 20)")
    group.add_argument("--no-cache", action="store_true",
                       help="bypass the branch/hours cache (prices are never cached)")
    group.add_argument("-v", "--verbose", action="store_true")


def _add_fanout(parser) -> None:
    group = parser.add_argument_group("fan-out limits")
    group.add_argument("--max-requests", type=_positive, default=40, metavar="N",
                       help="refuse before sending if the plan exceeds this (default 40)")
    group.add_argument("--workers", type=_positive, default=MAX_WORKERS, metavar="N",
                       help=f"concurrent requests, capped at {MAX_WORKERS}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise",
        description="Price Enterprise rental cars. Read-only: never books.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("locations", help="find a branch and its id")
    p.add_argument("query", help="city, airport code, or branch name")
    _add_geo(p); _add_output(p)
    p.set_defaults(func=cmd_locations)

    p = sub.add_parser("quote", help="priced fleet for one branch and date pair")
    _add_rental(p); _add_geo(p); vfilters.add_filter_flags(p); _add_output(p)
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("sweep", help="cheapest dates across a range")
    p.add_argument("pickup")
    p.add_argument("--dropoff", metavar="LOC")
    p.add_argument("--start", required=True, metavar="DATE")
    p.add_argument("--end", required=True, metavar="DATE")
    p.add_argument("--nights", type=int, required=True, metavar="N")
    p.add_argument("--step", type=int, default=1, metavar="N",
                   help="days between consecutive windows (default 1)")
    p.add_argument("--time", default="10:00", metavar="HH:MM")
    p.add_argument("--age", type=int, action="append", metavar="N")
    _add_geo(p); vfilters.add_filter_flags(p); _add_fanout(p); _add_output(p)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("compare", help="same dates, several branches")
    p.add_argument("pickups", nargs="+", metavar="LOC")
    p.add_argument("--pickup-time", required=True, metavar="WHEN")
    p.add_argument("--return-time", required=True, metavar="WHEN")
    p.add_argument("--age", type=int, action="append", metavar="N")
    _add_geo(p); vfilters.add_filter_flags(p); _add_fanout(p); _add_output(p)
    p.set_defaults(func=cmd_compare, dropoff=None)

    p = sub.add_parser("watch", help="poll until something matches")
    _add_rental(p)
    p.add_argument("--below", type=_money_arg, metavar="AMOUNT",
                   help="only match a trip total at or below this, in the "
                        "branch's BILLING currency (--currency does not "
                        "change what this is compared against)")
    p.add_argument("--every", type=int, default=30, metavar="MIN",
                   help="minutes between checks (minimum 15)")
    p.add_argument("--checks", type=int, default=2, metavar="N",
                   help="how many checks this invocation makes (default 2)")
    p.add_argument("--for-minutes", type=int, default=60, metavar="MIN",
                   help="give up after this long (default 60)")
    _add_geo(p); vfilters.add_filter_flags(p); _add_output(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("branch", help="hours, age rules and terms for one branch")
    p.add_argument("pickup", help="branch name, airport code, or numeric id")
    p.add_argument("--date", metavar="DATE", help="hours from this date (default today)")
    _add_geo(p); _add_output(p)
    p.set_defaults(func=cmd_branch, dropoff=None)

    p = sub.add_parser("doctor", help="check this environment can reach the API")
    _add_output(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("cache", help="show or clear the branch cache")
    p.add_argument("--clear", action="store_true")
    _add_output(p)
    p.set_defaults(func=cmd_cache)
    return parser


def _fail(args, code: int, message: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"error": message, "exit_code": code}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "every", None) is not None and args.command == "watch":
        args.every = max(15, args.every)  # never poll a 700KB endpoint faster

    try:
        return args.func(args)
    except (BotBlocked,) as exc:
        return _fail(args, EXIT_NET, str(exc))
    except TransportError as exc:
        return _fail(args, EXIT_NET, str(exc))
    except UsageError as exc:
        return _fail(args, EXIT_USAGE, str(exc))
    except KeyboardInterrupt:
        return _fail(args, EXIT_USAGE, "interrupted")
    except EnterpriseError as exc:
        return _fail(args, EXIT_NET, str(exc))
    except Exception as exc:  # never let a crash look like "nothing available"
        return _fail(args, EXIT_NET, f"unexpected {type(exc).__name__}: {exc}")
