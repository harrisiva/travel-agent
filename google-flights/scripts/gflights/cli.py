"""Command line interface: python3 -m gflights <command> ...

Exit codes (identical under --json):
    0  found what was asked for
    1  query succeeded, nothing matched
    2  usage / lookup error (bad code, impossible date, filter that cannot match)
    3  network, blocking, or payload error

The 1/3 split is what makes a polling loop safe. Only 1 means "keep waiting":
3 covers Google serving a captcha, which would otherwise look exactly like an
empty route and send a watch loop into a silent forever-loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from .client import Client, Filters, QueryError, build_query, unsatisfiable
from .http import (
    DEFAULT_MAX_REQUESTS,
    MAX_MAX_REQUESTS,
    FlightsHTTPError,
    RequestBudgetError,
    Transport,
)
from .model import Itinerary
from .parse import PayloadError, UnresolvableQuery

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET = 0, 1, 2, 3

#: Spare requests added to a sweep's auto-budget so one transient failure does
#: not end the run.
RETRY_HEADROOM = 3

SCHEMA_VERSION = 1


def _fix_console() -> None:
    """Windows consoles default to cp1252 and raise on accented airport names."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


#: Bound on a relative offset. Anything beyond the booking horizon is refused
#: later anyway; this exists so an absurd value fails as a usage error instead
#: of raising OverflowError out of timedelta, which argparse does not convert
#: and main() therefore never sees — the process died with a traceback and
#: exit 1, the code a watch loop reads as "keep waiting".
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
        return date.today() + timedelta(days=offset)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date (expected YYYY-MM-DD, or +N days from today)"
        )


def _clock(value: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a time (expected HH:MM)")


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
    """An int flag with real bounds.

    Unbounded, several of these produced a *confident wrong answer* rather than
    an error: --step 0 crashed inside range(), --step -1 swept nothing and
    called it "no flights", --limit 0 returned an empty list beside a count of
    18, and a zero ceiling read as "no filter" instead of "impossible".
    """
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


def _codes(value: str) -> tuple[str, ...]:
    return tuple(part.strip().upper() for part in value.split(",") if part.strip())


def _airlines(it) -> str:
    """Every operating carrier, not Google's headline label.

    A multi-carrier itinerary carries the literal string "multi" in the carrier
    slot and a *list* of names. Printing only the first said "Porter Airlines"
    for a trip flown Porter then Qatar — a wrong answer the user acts on when
    they go looking for the booking.
    """
    names = getattr(it, "carrier_names", None) or [it.carrier_name]
    return " + ".join(n for n in names if n) or it.carrier_name


def _rollover(depart: str, arrive: str) -> str:
    """"+1" when the flight lands on a later day than it left.

    Without it "22:45–01:58" reads as a 3-hour evening hop, and a user shown
    that books believing they land the same night.
    """
    if len(depart) < 10 or len(arrive) < 10 or arrive[:10] == depart[:10]:
        return ""
    try:
        nights = (date.fromisoformat(arrive[:10]) - date.fromisoformat(depart[:10])).days
    except ValueError:
        return ""
    return f"+{nights}" if nights > 0 else ""


def _span(low, high, fmt=str) -> str:
    """A "low – high" range that degrades when either end is missing."""
    if low is not None and high is not None:
        return f"{fmt(low)} – {fmt(high)}"
    if low is not None:
        return f"from {fmt(low)}"
    if high is not None:
        return f"up to {fmt(high)}"
    return ""


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit(args, payload: dict, found: bool) -> int:
    if args.json:
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": True, **payload},
            indent=2, ensure_ascii=False,
        ))
    return EXIT_OK if found else EXIT_NONE


def _fail(args, code: int, message: str) -> int:
    if args.json:
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": False, "error": message},
            indent=2, ensure_ascii=False,
        ))
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _party(args) -> str:
    """"2 adults, 1 lap infant" — so a party total is never read as per person."""
    parts = [
        (args.adults, "adult", "adults"),
        (args.children, "child", "children"),
        (args.infants_in_seat, "infant in seat", "infants in seat"),
        (args.infants_on_lap, "lap infant", "lap infants"),
    ]
    said = [f"{n} {one if n == 1 else many}" for n, one, many in parts if n]
    return ", ".join(said) or "1 adult"


def _print_itineraries(
    itineraries: list[Itinerary], limit: int, travellers: str = "1 adult"
) -> None:
    if not itineraries:
        print("No flights matched.")
        return
    covers = itineraries[0].price_covers
    currency = itineraries[0].currency
    print(
        f"{len(itineraries)} option(s) — prices are {covers} totals in "
        f"{currency} for {travellers}\n"
    )
    for it in itineraries[:limit]:
        stops = "nonstop" if it.nonstop else (
            f"{it.stops} stop ({', '.join(it.layovers)})" if it.stops == 1
            else f"{it.stops} stops ({', '.join(it.layovers)})"
        )
        price = f"{it.price}" if it.price is not None else "—"
        print(
            f"  {price:>6} {currency}  {it.depart[11:16]}–{it.arrive[11:16]}"
            f"{_rollover(it.depart, it.arrive):<3}"
            f"{_hm(it.duration_minutes):>8}  {stops:<28} {_airlines(it)}"
        )
        for leg in it.legs:
            extras = " · ".join(
                x for x in (leg.aircraft, f"{leg.legroom} legroom" if leg.legroom else None)
                if x
            )
            print(
                f"           {leg.carrier}{leg.flight_number:<5} "
                f"{leg.origin}→{leg.destination} "
                f"{leg.depart[11:16]}–{leg.arrive[11:16]}"
                f"{_rollover(leg.depart, leg.arrive)}"
                + (f"  {extras}" if extras else "")
            )
        for stop in getattr(it, "layover_details", ()) or ():
            change = (
                f" — CHANGE AIRPORTS to {stop.depart_code}"
                if stop.depart_code and stop.depart_code != stop.code else ""
            )
            # minutes is int | None: Google omits it on some connections, and
            # a shifted index yields None too. Printing "layover in Doha" is
            # fine; crashing here after three legs are already on screen is not
            # — in `watch` the itinerary prints only on a hit, so this turned a
            # fare that DID drop into exit 3.
            wait = f"{_hm(stop.minutes)} " if stop.minutes is not None else ""
            print(
                f"           layover {wait}in "
                f"{stop.city or stop.name or stop.code} ({stop.code}){change}"
            )
        if it.co2_percent_vs_typical is not None:
            direction = "below" if it.co2_percent_vs_typical < 0 else "above"
            print(
                f"           CO2 {abs(it.co2_percent_vs_typical)}% {direction} typical "
                f"for this route"
            )
        print()
    if len(itineraries) > limit:
        print(f"  ... {len(itineraries) - limit} more (raise --limit to see them)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _client(args) -> Client:
    return Client(
        Transport(max_requests=args.max_requests or DEFAULT_MAX_REQUESTS),
        country=args.country,
        currency=args.currency,
    )


def _filters(args) -> Filters:
    return Filters(
        max_price=getattr(args, "max_price", None),
        airlines=getattr(args, "airlines", ()) or (),
        exclude_airlines=getattr(args, "exclude_airlines", ()) or (),
        max_duration_minutes=getattr(args, "max_duration", None),
        depart_after=getattr(args, "depart_after", None),
        depart_before=getattr(args, "depart_before", None),
        arrive_before=getattr(args, "arrive_before", None),
        arrive_same_day=getattr(args, "arrive_same_day", False),
        avoid_layovers=getattr(args, "avoid_layovers", ()) or (),
        max_co2_percent=getattr(args, "max_co2", None),
        min_legroom_inches=getattr(args, "min_legroom", None),
        avoid_aircraft=getattr(args, "avoid_aircraft", ()) or (),
    )


def _query(args):
    return build_query(
        args.origin.upper(),
        args.destination.upper(),
        args.depart,
        args.ret,
        adults=args.adults,
        children=args.children,
        infants_in_seat=args.infants_in_seat,
        infants_on_lap=args.infants_on_lap,
        cabin=args.cabin,
        max_stops=args.max_stops,
    )


def _refuse_impossible(args, result, **checks) -> None:
    """Refuse filters this route proves can never match.

    Returning "nothing matched" (exit 1) for an impossible filter is a false
    negative, and exit 1 is the code a watch loop reads as "keep waiting" — so
    an unsatisfiable threshold made a cron poll forever and never fire. It
    covers price, duration, carrier and time windows, in every command that
    filters; `checks` switches off the bounds that only hold for one date on
    one day (see `unsatisfiable`).
    """
    reason = unsatisfiable(_filters(args), result.filters, **checks)
    if reason:
        raise QueryError(reason)


def cmd_search(args) -> int:
    """"What flies YYZ to YHZ on the 25th, nonstop, under $400?" """
    result = _client(args).search(_query(args))
    _refuse_impossible(args, result)
    matched = _filters(args).apply(result.itineraries)
    matched.sort(key=lambda i: (i.price is None, i.price, i.duration_minutes))

    if not args.json:
        _print_itineraries(matched, args.limit, _party(args))
        if result.unparsed:
            print(f"({result.unparsed} option(s) Google returned could not be read "
                  f"and were skipped — this list is incomplete.)")
        if result.itineraries and not matched:
            print(
                f"({len(result.itineraries)} flights exist on this route — "
                f"your filters excluded all of them.)"
            )
    return _emit(
        args,
        {
            "query": _describe_query(args),
            "returned": len(matched[: args.limit]),
            "count": len(matched),
            "total_before_filters": len(result.itineraries),
            "unparsed": result.unparsed,
            "itineraries": [i.to_dict() for i in matched[: args.limit]],
        },
        bool(matched),
    )


def cmd_price_check(args) -> int:
    """"Is $249 a good price, or should I wait?" """
    result = _client(args).search(_query(args))
    context = result.price_context
    cheapest = min(
        (i.price for i in result.itineraries if i.price is not None), default=None
    )

    if context is None:
        # Google does not always ship the price-history block — it depends on
        # the route, the dates, and sometimes simply on the response variant it
        # chooses. That is a missing *answer*, not a failed query, so it exits 1
        # and still reports what the search did find rather than throwing the
        # whole result away.
        spread = result.filters
        if not args.json:
            if cheapest is not None:
                basis = "round trip" if args.ret else "one way"
                print(f"Cheapest right now:  {cheapest} {args.currency} "
                      f"({basis} total for {_party(args)})")
            if spread and spread.price_min is not None:
                print(f"Price-filter bounds: "
                      f"{_span(spread.price_min, spread.price_max)} "
                      f"{spread.currency} (Google's price slider for this "
                      f"search — not a history, and the top is not a fare "
                      f"to quote)")
            # "The fare above is real" only holds when a fare was printed. With
            # no priced itinerary the only number on screen is the slider
            # bounds, which SKILL.md says must never be quoted as a fare range
            # — so the sentence would point the agent straight at the one
            # figure it is told not to use.
            closing = (
                "The fare above is real; there is just nothing to judge it "
                "against."
                if cheapest is not None else
                "No priced option was returned either, so there is nothing "
                "to quote — the bounds above are a filter slider, not fares."
            )
            print(
                "\nNo price verdict available — Google publishes its "
                "typical/low/high history only for some routes and dates. "
                + closing
            )
        return _emit(
            args,
            {
                "query": _describe_query(args),
                "cheapest_now": cheapest,
                "price_context": None,
                "route_fare_range": (
                    {"min": spread.price_min, "max": spread.price_max,
                     "currency": spread.currency} if spread else None
                ),
            },
            False,
        )

    if not args.json:
        basis = "round trip" if args.ret else "one way"
        if cheapest is not None:
            print(f"Cheapest right now:  {cheapest} {context.currency} "
                  f"({basis} total for {_party(args)})")
        else:
            # A history block can arrive with no priced itinerary behind it.
            # "Cheapest right now: None" is worse than saying what happened.
            print("Cheapest right now:  no priced option was returned")
        if context.verdict:
            print(f"Google's verdict:    prices are currently {context.verdict}")
        if context.typical is not None and cheapest is not None:
            delta = cheapest - context.typical
            word = "below" if delta < 0 else "above"
            print(f"Versus typical:      {abs(delta)} {context.currency} {word} the usual {context.typical}")
        if context.low is not None and context.high is not None:
            print(f"Range seen:          {context.low}–{context.high} {context.currency}")
        if context.history:
            print(f"History:             {len(context.history)} daily points, "
                  f"{context.history[0][0]} to {context.history[-1][0]}")
        if context.advice:
            print(f"Booking advice:      cheapest to book {context.advice}")
        if context.verdict is None:
            print(
                "\nGoogle published no verdict word for these dates — the band "
                "above is real, but it is not a judgement. Do not report this "
                "as 'a good price'; say the verdict was unavailable."
            )
            if context.verdict_code is not None:
                # The payload DID carry a verdict; only the rendered wording we
                # scrape it from was missing. That is the signature of Google
                # rewording its banner, which would otherwise make every
                # price-check on every route exit 1 forever while still printing
                # a confident band.
                print(
                    f"  Note: the payload still carries a verdict code "
                    f"({context.verdict_code}) even though the wording could "
                    f"not be read. If this happens on every route, the scrape "
                    f"has broken — run test_flights.py and report it."
                )

    return _emit(
        args,
        {
            "query": _describe_query(args),
            "cheapest_now": cheapest,
            "price_context": context.to_dict(),
        },
        # Exit 0 means "there is a verdict to report", which is what this
        # command exists for and what SKILL.md documents. A band without the
        # rendered verdict string still exits 1, so an agent can branch on the
        # code instead of null-checking a field the docs called authoritative.
        context.verdict is not None,
    )


def cmd_cheapest(args) -> int:
    """"Which departure date in the next three weeks is cheapest?" """
    # A sweep needs one request per date, so the single-search default of 5
    # made every `cheapest` run fail before issuing anything — including both
    # documented examples. Asking for --days N *is* asking for N requests, so
    # that becomes the budget unless the user set one deliberately.
    if args.max_requests is None:
        dates = len(range(0, args.days, args.step))
        # Headroom for retries. Budgeting exactly one request per date meant a
        # single 5xx or TLS recovery exhausted the ceiling and failed with
        # "narrow the range" — blaming the caller for a Google-side hiccup, and
        # discarding every date already fetched.
        args.max_requests = min(dates + RETRY_HEADROOM, MAX_MAX_REQUESTS)
    client = _client(args)
    sweep = client.sweep(_query(args), args.days, step=args.step)
    filters = _filters(args)

    rows = []
    # Why each date's carrier chips rule the airline filters out (None if they
    # do not). Refused only when EVERY date does — a carrier that flies some
    # weekdays and not others must not sink the whole sweep on the first date.
    carrier_reasons = []
    for index, (day, result) in enumerate(sweep):
        if index == 0:
            # Only what holds on every date: the clock window. The first date's
            # fare, duration and carrier bounds say nothing about the later
            # ones, and refusing on them threw away sweeps where a later date
            # would have matched.
            _refuse_impossible(args, result, check_price=False,
                               check_duration=False, check_airlines=False)
        if filters.airlines or filters.exclude_airlines:
            carrier_reasons.append(unsatisfiable(
                filters, result.filters, check_price=False,
                check_duration=False))
        matched = filters.apply(result.itineraries)
        prices = [i.price for i in matched if i.price is not None]
        rows.append({
            "depart": day.isoformat(),
            # The return moves with the departure: --return sets the trip
            # LENGTH, not a fixed date. Emitted per row so the trip actually
            # priced is never left to inference.
            "return": (
                (day + (args.ret - args.depart)).isoformat() if args.ret else None
            ),
            "cheapest": min(prices) if prices else None,
            "options": len(matched),
        })

    if carrier_reasons and all(carrier_reasons):
        raise QueryError(
            f"on no date in this range: {carrier_reasons[0]}"
        )

    priced = [r for r in rows if r["cheapest"] is not None]
    best = min(priced, key=lambda r: r["cheapest"]) if priced else None

    if not args.json:
        if not priced:
            print("No priced options on any date in that window.")
        else:
            basis = "round trip" if args.ret else "one way"
            print(
                f"Cheapest departure dates — {basis} totals in "
                f"{args.currency} for {_party(args)}:\n"
            )
            if args.ret:
                # --return sets the trip LENGTH, so the return slides with the
                # departure. Showing only the departure column invites quoting
                # a saving against a trip the user never asked for.
                nights = (args.ret - args.depart).days
                print(f"  (each row is a {nights}-night trip; the return moves "
                      f"with the departure)\n")
            for row in rows:
                mark = " <- cheapest" if best and row["depart"] == best["depart"] else ""
                price = row["cheapest"] if row["cheapest"] is not None else "—"
                back = f" → {row['return']}" if row.get("return") else ""
                print(f"  {row['depart']}{back}  {str(price):>6}  "
                      f"{row['options']:>3} option(s){mark}")
            print(f"\nBest: {best['depart']} at {best['cheapest']} {args.currency}")
    return _emit(
        args,
        {"query": _describe_query(args), "days": rows, "best": best},
        best is not None,
    )


def cmd_route(args) -> int:
    """"What can I actually filter on for this route, and where does it connect?" """
    result = _client(args).search(_query(args))
    filters = result.filters
    if filters is None:
        return _fail(args, EXIT_NET, "no route metadata in the response")

    if not args.json:
        print(f"{args.origin.upper()} -> {args.destination.upper()} on {args.depart}\n")
        # Both bounds are nullable. Printing "249–None", or a max of "0h 00m"
        # from `or 0`, is a wrong value rather than a missing one.
        fares = _span(filters.price_min, filters.price_max)
        if fares:
            print(f"  Fares seen:      {fares} {filters.currency}")
        duration = _span(
            filters.duration_min_minutes, filters.duration_max_minutes, _hm
        )
        if duration:
            print(f"  Duration range:  {duration}")
        print(f"\n  Airlines ({len(filters.airlines)}) — Google's filter chips "
              f"for this route, which include codeshare and interline entries. "
              f"NOT a list of carriers you can fly:")
        for code, name in filters.airlines:
            print(f"    {code}  {name}")
        if filters.connection_airports:
            print(f"\n  Possible connections ({len(filters.connection_airports)}):")
            for code, city in filters.connection_airports:
                print(f"    {code}  {city}")
    return _emit(
        args,
        {
            "query": _describe_query(args),
            "filters": filters.to_dict(),
            "airports": [a.to_dict() for a in result.airports],
        },
        True,
    )


def cmd_watch(args) -> int:
    """"Tell me when this drops below $220." One check; exit 0 when it has.

    Designed for cron: exit 0 means the threshold was met (act on it), exit 1
    means not yet (keep waiting), exit 3 means the check itself failed and the
    result says nothing about the fare.
    """
    result = _client(args).search(_query(args))
    # A watch waits for the fare to fall, so today's cheapest fare is not a
    # floor for it — neither for --under (a threshold below today's price is
    # the normal case) nor for --max-price. Refusing on either turned the
    # command's whole purpose into exit 2.
    _refuse_impossible(args, result, check_price=False)

    matched = _filters(args).apply(result.itineraries)
    priced = [i for i in matched if i.price is not None]
    if not priced:
        if not args.json:
            print("No priced options matched — nothing to compare against yet.")
        # `query` is emitted on every path: this is the branch a watch takes on
        # most polls, and an agent keying on it should not break precisely there.
        return _emit(
            args,
            {
                "query": _describe_query(args),
                "hit": False,
                "under": args.under,
                "cheapest": None,
                "itinerary": None,
            },
            False,
        )

    best = min(priced, key=lambda i: i.price)
    hit = best.price <= args.under

    if not args.json:
        verdict = "AT OR BELOW" if hit else "still above"
        basis = "round trip" if args.ret else "one way"
        print(
            f"Cheapest {best.price} {best.currency} ({basis} total for "
            f"{_party(args)}) — {verdict} your {args.under} threshold"
        )
        if hit:
            _print_itineraries([best], 1, _party(args))
    return _emit(
        args,
        {
            "query": _describe_query(args),
            "hit": hit,
            "under": args.under,
            "cheapest": best.price,
            "itinerary": best.to_dict(),
        },
        hit,
    )


def cmd_airports(args) -> int:
    """"What's the airport code for Toronto? What else is near Halifax?" """
    resolved, others = _client(args).resolve(args.place)

    if not args.json:
        if resolved:
            print(f"Airports Google routes to for {args.place}:")
            for a in resolved:
                # City AND country, always. Note these do NOT disambiguate a
                # repeated name — Springfield MO and Springfield IL are both
                # "Springfield, United States". The airport NAME and the
                # coordinates in the JSON are what separate them; this line
                # exists so the reader can see that a choice was made at all.
                where = ", ".join(x for x in (a.city, a.country) if x)
                print(f"  {a.code}  {a.name}" + (f"  ({where})" if where else ""))
        else:
            print(
                f"Google returned no flights to {args.place!r}, so there is "
                f"nothing to resolve. Check the spelling, try a nearby larger "
                f"city, or pass the IATA code directly if you know it."
            )
    return _emit(
        args,
        {
            "place": args.place,
            "matched": [a.to_dict() for a in resolved],
            # Everything else Google happened to name while answering. NOT
            # "airports near this place" — do not offer these as alternatives.
            "also_named_on_the_page": [a.code for a in others],
        },
        bool(resolved),
    )


def _describe_query(args) -> dict:
    return {
        "origin": args.origin.upper(),
        "destination": args.destination.upper(),
        "depart": args.depart.isoformat(),
        "return": args.ret.isoformat() if args.ret else None,
        "trip": "round trip" if args.ret else "one way",
        "cabin": args.cabin,
        "price_covers_travellers": (
            args.adults + args.children + args.infants_in_seat + args.infants_on_lap
        ),
        "passengers": {
            "adults": args.adults,
            "children": args.children,
            "infants_in_seat": args.infants_in_seat,
            "infants_on_lap": args.infants_on_lap,
        },
        "country": args.country,
        "currency": args.currency,
    }


# ---------------------------------------------------------------------------
# Argument wiring
# ---------------------------------------------------------------------------


def _add_trip(parser: argparse.ArgumentParser) -> None:
    """The route, dates and travellers — shared by every command."""
    parser.add_argument("origin", help="origin IATA code, e.g. YYZ")
    parser.add_argument("destination", help="destination IATA code, e.g. YHZ")
    parser.add_argument("--depart", type=_date, required=True,
                        help="departure date, YYYY-MM-DD or +N days from today")
    parser.add_argument("--return", dest="ret", type=_date,
                        help="return date; omit for a one-way search")
    parser.add_argument("--adults", type=_bounded("--adults", 1, 9), default=1)
    parser.add_argument("--children", type=_bounded("--children", 0, 8), default=0)
    parser.add_argument("--infants-in-seat",
                        type=_bounded("--infants-in-seat", 0, 8), default=0)
    parser.add_argument("--infants-on-lap",
                        type=_bounded("--infants-on-lap", 0, 8), default=0)
    parser.add_argument("--cabin", default="economy",
                        choices=("economy", "premium-economy", "business", "first"),
                        help="cabin class (default economy)")
    parser.add_argument("--max-stops", type=int, choices=(0, 1, 2),
                        help="0 = nonstop only. Applied by Google, not locally")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    """Filters applied to results after fetching. No extra requests."""
    parser.add_argument("--max-price", type=_bounded("--max-price", 1),
                        help="ceiling on the trip total")
    parser.add_argument("--airlines", type=_codes,
                        help="only these carrier codes, comma separated (AC,WS)")
    parser.add_argument("--exclude-airlines", type=_codes,
                        help="drop these carrier codes")
    parser.add_argument("--max-duration", type=_bounded("--max-duration", 1),
                        metavar="MINUTES", help="longest acceptable total travel time")
    parser.add_argument("--depart-after", type=_clock, metavar="HH:MM")
    parser.add_argument("--depart-before", type=_clock, metavar="HH:MM")
    parser.add_argument("--arrive-before", type=_clock, metavar="HH:MM",
                        help="lands by this local time of day at the destination")
    parser.add_argument("--arrive-same-day", action="store_true",
                        help="must land on the departure date — use with "
                             "--arrive-before to exclude red-eyes")
    parser.add_argument("--avoid-layovers", type=_codes,
                        help="reject itineraries connecting through these airports")
    parser.add_argument("--min-legroom", type=_bounded("--min-legroom", 0),
                        metavar="INCHES",
                        help="every leg must state at least this much legroom")
    parser.add_argument("--avoid-aircraft", type=_codes,
                        help="reject itineraries using these aircraft, e.g. 737MAX")
    parser.add_argument("--max-co2", type=int, metavar="PERCENT",
                        help="emissions versus typical, e.g. -20 for 20%% cleaner")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (same exit codes)")
    parser.add_argument("--currency", type=_currency, default="CAD",
                        help="fare currency (default CAD)")
    parser.add_argument("--country", type=_country, default="CA", metavar="CC",
                        help="point of sale, sent explicitly for reproducible "
                             "results. Varying it alone has not been observed "
                             "to change fares (default CA)")
    parser.add_argument("--max-requests", type=_budget, default=None,
                        help=f"ceiling on requests to Google (default "
                             f"{DEFAULT_MAX_REQUESTS}; `cheapest` defaults to "
                             f"one per visited date plus {RETRY_HEADROOM} for "
                             f"retries; max {MAX_MAX_REQUESTS})")


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that respects --json when it rejects the arguments.

    argparse writes a usage block to stderr and exits, so a caller parsing
    stdout got nothing at all for a bad flag — while the very same mistake
    caught later (a past date, say) returned a proper error object. The split
    was invisible from outside, so it is closed here.
    """

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
        prog="flights",
        description="Search Google Flights from the command line. Read-only: "
                    "it never books, holds or pays for anything.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    p = sub.add_parser("airports", help="resolve a place name to airport codes")
    p.add_argument("place", help='a city or airport, e.g. "Toronto"')
    _add_common(p)
    p.set_defaults(func=cmd_airports)

    p = sub.add_parser("search", help="what flies this route on these dates")
    _add_trip(p); _add_filters(p); _add_common(p)
    # Only `search` renders a list of itineraries, so only `search` takes
    # --limit. Offering it on the others would be a flag that silently does
    # nothing — the thing this repo treats as worse than a missing feature.
    p.add_argument("--limit", type=_bounded("--limit", 1), default=10,
                   help="most itineraries to show (default 10)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("price-check",
                       help="is the current fare good, or is it worth waiting")
    _add_trip(p); _add_common(p)
    p.set_defaults(func=cmd_price_check)

    p = sub.add_parser("cheapest", help="which departure date in a window is cheapest")
    _add_trip(p); _add_filters(p); _add_common(p)
    p.add_argument("--days", type=_bounded("--days", 1, 60), default=14,
                   help="how many departure dates to try, from --depart (default 14)")
    p.add_argument("--step", type=_bounded("--step", 1), default=1,
                   help="stride in days; 2 halves the requests (default 1)")
    p.set_defaults(func=cmd_cheapest)

    p = sub.add_parser("route",
                       help="fare and duration bounds for these dates, "
                            "connection airports, and Google's filter chips "
                            "(not a list of who flies it)")
    _add_trip(p); _add_common(p)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("watch", help="one check against a price threshold, for cron")
    _add_trip(p); _add_filters(p); _add_common(p)
    p.add_argument("--under", type=_bounded("--under", 1), required=True,
                   help="alert threshold: exit 0 when the fare is at or below this")
    p.set_defaults(func=cmd_watch)

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
    except (QueryError, RequestBudgetError, UnresolvableQuery) as e:
        return _fail(args, EXIT_USAGE, str(e))
    except (FlightsHTTPError, PayloadError) as e:
        return _fail(args, EXIT_NET, str(e))
    except Exception as e:  # never a traceback in an agent's transcript
        return _fail(args, EXIT_NET, f"unexpected {type(e).__name__}: {e}")
