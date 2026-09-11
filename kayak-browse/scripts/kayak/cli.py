"""Command line interface: python3 -m kayak <command> ...

Exit codes, identical under human and `--json` output:

    0  found what was asked for, and the search finished
    1  the search finished and nothing matched
    2  usage, lookup, or budget error
    3  network or API error
    4  API key missing, rejected, or expired
    5  the search timed out before finishing — results are partial

The contract that matters to an agent polling this tool is that **`1` is the
only code meaning "keep waiting"**. Everything else means stop and do
something else. Two consequences follow, and both are deliberate:

* A partial search that found nothing is `5`, not `1`. `1` asserts "the search
  completed and the answer is genuinely no", and that assertion is the whole
  basis of a safe polling loop. A partial search has not earned it.
* A partial search that found ten offers is also `5`, not `0`, because
  "cheapest" is unproven while providers are still reporting.

The exception is `--until second-phase`: if the caller asked to stop once the
major providers were in and that happened, the requested condition was met, so
it exits `0`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from . import filters as filter_mod
from . import format as fmt
from .auth import ENV_API_KEY, read_key_file, resolve_key, store_key
from .cache import HOURLY_LIMITS, Cache
from .client import (
    DEFAULT_MAX_POLL_SECONDS,
    SANDBOX_HOST,
    STATUS_COMPLETE,
    STATUS_SECOND,
    Client,
    ceiling_polls,
)
from .errors import AuthError, KayakError, SearchTimeout, UsageError

EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET, EXIT_AUTH, EXIT_PARTIAL = 0, 1, 2, 3, 4, 5

#: Which exit code each exception carries. Kept as one table rather than an
#: attribute per class so the whole contract is legible in one place.
EXIT_FOR = {
    "UsageError": EXIT_USAGE,
    "BudgetError": EXIT_USAGE,
    "TransportError": EXIT_NET,
    "RateLimited": EXIT_NET,
    "AuthError": EXIT_AUTH,
    "SearchTimeout": EXIT_PARTIAL,
}

ENV_BASE_HOST = "KAYAK_HOST"

#: Default ceiling for `sweep`, matching `campsites find`. A sweep costs one
#: search per candidate day, and a search costs up to a dozen polls, so the old
#: default of 40 refused three days — including the ten-day sweep `recipes.md`
#: documents. 200 keeps a ten-day sweep inside the cars endpoint's 250/hour
#: quota while still stopping an agent that asks for a whole season.
DEFAULT_SWEEP_MAX_REQUESTS = 200

#: How many of the best days `sweep` re-polls to `complete`. Held here as well
#: as in the client so the refusal below quotes the same arithmetic the client
#: budgets on — it is passed explicitly rather than left to the default.
SWEEP_CONFIRM_TOP = 2

#: What a hotel rate covers. The client sends `rooms=DEFAULT_ROOMS` ("2" — one
#: room, two adults) whenever dates are given, and there is no party-size flag
#: yet, so every rate this command prints is for that occupancy. It is reported
#: on `meta` and under the table because a nightly rate quoted without the
#: occupancy it assumes is the wrong number for a family of four.
HOTEL_OCCUPANCY = "1 room, 2 adults"


def _fix_console() -> None:
    """Windows consoles default to cp1252 and raise on accented place names."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date (expected YYYY-MM-DD)")


def _clock(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a time (expected HH:MM, 24-hour)")
    return parsed.hour, parsed.minute


def _month(value: str) -> str:
    """A YYYY-MM month, validated here so a typo fails before the request.

    Returned as the string the API wants rather than a date: the calendar
    endpoint aggregates by month or by day within a month range, and a
    `date` would invent a day-of-month that is not part of the query.
    """
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a month (expected YYYY-MM)")
    return value


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _exit_for(exc: Exception) -> int:
    return EXIT_FOR.get(type(exc).__name__, EXIT_NET)


def _client(args) -> Client:
    cache = Cache(enabled=not getattr(args, "no_cache", False))
    key = resolve_key(getattr(args, "api_key", None),
                      getattr(args, "key_file", None), cache)
    return Client(
        api_key=key,
        host=getattr(args, "host", None) or os.environ.get(ENV_BASE_HOST) or SANDBOX_HOST,
        cache=cache,
        max_poll_seconds=getattr(args, "max_poll_seconds", DEFAULT_MAX_POLL_SECONDS),
    )


def _emit(args, command: str, results, found: bool, *, query=None, meta=None) -> int:
    if getattr(args, "json", False):
        code = EXIT_OK if found else EXIT_NONE
        fmt.emit_json(fmt.envelope(command, results, ok=True, exit_code=code,
                                   query=query, meta=meta))
    return EXIT_OK if found else EXIT_NONE


def _fail(args, code: int, message: str, command: str = "",
          results=(), query=None, meta=None) -> int:
    """The only failure exit. `code` is an argument, never derived here.

    That is what keeps the exit code identical with and without `--json`:
    the number is decided by the caller, and `--json` only changes what gets
    written to stdout.
    """
    if getattr(args, "json", False):
        fmt.emit_json(fmt.envelope(command or getattr(args, "command", ""),
                                   results, ok=False, exit_code=code,
                                   error=message, query=query, meta=meta))
    print(f"error: {message}", file=sys.stderr)
    return code


# --------------------------------------------------------------- commands


def cmd_login(args) -> int:
    """Store a key so later calls need no flag and no environment variable.

    Exists mainly for claude.ai, where there is no shell profile to export a
    variable into. The key is validated with one autocomplete call before it is
    written, so a typo fails here rather than three commands later looking like
    "nothing available".
    """
    cache = Cache()
    key = read_key_file(args.key_file) if args.key_file else (args.api_key or "").strip()
    if not key:
        return _fail(args, EXIT_USAGE,
                     "give --key-file <path> (or - for stdin), or --api-key",
                     "login")
    probe = Client(api_key=key,
                   host=args.host or os.environ.get(ENV_BASE_HOST) or SANDBOX_HOST,
                   cache=cache)
    # refresh=True: a cached autocomplete would validate nothing. The whole
    # job of this call is to make the API judge the key.
    probe.places("JFK", "cars", refresh=True)   # raises AuthError if key is bad
    path = store_key(cache, key)
    results = [{"stored": True, "path": str(path),
                "key_fingerprint": probe._key_fingerprint(),
                "base_url": probe.host, "sandbox": probe.sandbox}]
    if not args.json:
        print(f"key validated and stored in {path}")
    return _emit(args, "login", results, True)


def cmd_check(args) -> int:
    """Is there a usable key, and which one is being used?

    Cheapest possible call (autocomplete, 100/hour). Run it first whenever the
    key state is unknown — the alternative is discovering a dead key halfway
    through a sweep.

    `refresh=True` is the whole command. Served from the place cache this made
    zero requests and reported "key OK" for the seven days the entry lived —
    including after the key expired, which is the one moment `check` exists for.
    """
    client = _client(args)
    client.places("JFK", "cars", refresh=True)
    results = [{
        "key_valid": True,
        "key_source": _key_source(args),
        # A derived fingerprint, never a substring of the key: this output
        # lands in chat transcripts and bug reports.
        "key_fingerprint": client._key_fingerprint(),
        "base_url": client.host,
        "sandbox": client.sandbox,
        "prices_are_mocked": client.sandbox,
        "probe": "autocomplete/cars",
    }]
    if not args.json:
        print(f"key OK (fingerprint {results[0]['key_fingerprint']}, "
              f"source {results[0]['key_source']})")
        print(f"host {client.host}")
        if client.sandbox:
            print(fmt.SANDBOX_BANNER)
    return _emit(args, "check", results, True,
                 meta={"sandbox": client.sandbox,
                       "prices_are_mocked": client.sandbox})


def _key_source(args) -> str:
    if getattr(args, "api_key", None):
        return "flag:--api-key"
    if os.environ.get(ENV_API_KEY):
        return f"env:{ENV_API_KEY}"
    if getattr(args, "key_file", None):
        return f"file:{args.key_file}"
    return "file:stored"


def cmd_places(args) -> int:
    """Resolve a name to the ids the search endpoints accept."""
    client = _client(args)
    places = client.places(args.query, args.__dict__.get("for_") or "cars")
    results = [p.summary() for p in places]
    if not args.json:
        if not places:
            print(f"no places match {args.query!r}")
        else:
            # The entity key column appears only when a row actually has one,
            # which in practice means `--for hotels`. It is printed because
            # `hotels --destination` needs exactly this value: leaving it to
            # `--json` meant the help text pointed at a column a human running
            # `places` could not see.
            show_key = any(p.entity_key for p in places)
            headers = ["place", "type", "iata", "city id", "place id"]
            if show_key:
                headers.append("entity key")
            rows = []
            for p in places:
                row = [p.label, p.place_type or "?", p.iata or "",
                       str(p.city_id or ""), str(p.place_id or "")]
                if show_key:
                    row.append(p.entity_key or "")
                rows.append(row)
            print(fmt.table(rows, headers))
    return _emit(args, "places", results, bool(places),
                 query={"query": args.query,
                        "for": args.__dict__.get("for_") or "cars"})


def _render_cars(args, client, search, outcome, command: str) -> tuple[list[dict], dict]:
    """Shared rendering for `cars`. Returns (rows, meta).

    `--limit` trims the JSON array as well as the table. That is the whole
    point of the projection: a 500-row response is the thing most likely to
    fill an agent's context window, and a limit that only shortened the
    human-readable table would leave that unsolved. `meta.kept` still reports
    how many matched, so trimming never hides the true count.

    Which is exactly why the rows are **sorted before they are trimmed**. The
    API's own order is not price order, so trusting it and then truncating to
    `--limit` can drop the cheapest offer out of the answer entirely and leave
    a plausible, expensive row sitting at the top. Under `--sort distance` the
    API's order is the answer and is preserved — but then `meta.ranked_by`
    says `distance`, so nothing downstream can call the first row "cheapest".
    """
    ranked_by = getattr(args, "sort", "price") or "price"
    kept = list(outcome.kept)
    if ranked_by == "price":
        # `sort_key` pins priceless offers last: an offer with no price cannot
        # be the cheapest anything, and must never head the list.
        kept.sort(key=lambda offer: offer.sort_key())
    if args.full:
        # The raw `results[]` array, with the lookup maps that give it meaning
        # moved onto meta. `results` stays a list, so the envelope contract
        # holds for `--full` exactly as for the projection. The rows keep their
        # shape; only their order follows the ranking, and `priceIsReal` is
        # added so a row read on its own still carries the sandbox marker.
        ordered = _ordered_results(search, kept if ranked_by == "price" else None)
        rows = [fmt.full_row(row, client.sandbox) for row in ordered][: args.limit]
    else:
        rows = [fmt.offer_row(o, client.sandbox) for o in kept][: args.limit]
    meta = fmt.search_meta(search, sandbox=client.sandbox,
                           requests_made=client.requests_made,
                           kept=len(kept), parsed=outcome.parsed,
                           rejected=outcome.rejected)
    meta["ranked_by"] = ranked_by
    meta["returned"] = len(rows)
    meta["truncated"] = len(rows) < (len(ordered) if args.full else len(kept))
    if args.full:
        meta["full"] = True
        raw = getattr(search, "raw", {}) or {}
        meta["maps"] = {key: raw.get(key, {}) for key in
                        ("agencies", "providers", "carLocations")}
    if not args.json:
        show_price = not client.sandbox or args.sandbox_ok
        fmt.print_offers(kept, sandbox=client.sandbox,
                         show_price=show_price, limit=args.limit,
                         total=len(kept), ranked_by=ranked_by)
        fmt.print_rejections(outcome.rejected, len(kept))
        if search and not getattr(search, "complete", False):
            print(f"\nsearch was still {search.status} when it stopped — "
                  f"these are partial results, not a final cheapest")
    return rows, meta


def _list_results(search) -> list:
    """The API's own `results[]` array, for `--full`.

    Deliberately the unmodified rows: the reason to reach for `--full` is that
    the projection dropped a field, so re-shaping here would defeat it.
    """
    raw = getattr(search, "raw", {}) or {}
    results = raw.get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _ordered_results(search, ranked) -> list:
    """The raw rows, put in the same order as the ranked offers.

    `--limit` truncates `--full` too, so a raw row list left in API order has
    the same failure as the projection did: the cheapest row falls off the end.
    Rows are matched back by `id`, ties keep the API's order (the sort is
    stable), and a row that no kept offer points at sorts last rather than
    being dropped — `--full` is the escape hatch, so it never removes data.
    Pass `ranked=None` to leave the API's order alone.
    """
    rows = _list_results(search)
    if ranked is None:
        return rows
    rank: dict[str, int] = {}
    for position, offer in enumerate(ranked):
        rank.setdefault(str(offer.result_id), position)
    unranked = len(rank)
    return sorted(rows, key=lambda row: rank.get(str(row.get("id")), unranked))


def cmd_cars(args) -> int:
    client = _client(args)

    # Sandbox honesty. `cars` still answers — which agencies serve the airport,
    # what classes, what mileage and cancellation terms — because all of that
    # is genuine and is what a sandbox is for. What it withholds without
    # --sandbox-ok is the price column, because a number on screen gets read
    # and quoted whatever caveat sits beside it. `sweep`, whose whole purpose
    # is ranking on price, refuses outright instead; see cmd_sweep.

    query = {"pickup": args.pickup, "dropoff": args.drop or args.pickup,
             "from": args.from_date.isoformat(), "to": args.to_date.isoformat(),
             "until": args.until}
    try:
        search = client.search_cars(
            args.pickup, args.from_date, args.to_date,
            pickup_type=args.pickup_type, dropoff=args.drop,
            dropoff_type=args.drop_type, pickup_time=args.pickup_time,
            drop_time=args.drop_time, per_day=args.per_day,
            currency=args.currency, sort=args.sort, until=args.until,
        )
    except SearchTimeout as exc:
        partial = exc.partial
        outcome = filter_mod.apply(partial.offers if partial else (),
                                   filter_mod.build(args))
        rows, meta = _render_cars(args, client, partial, outcome, "cars")
        return _fail(args, EXIT_PARTIAL, str(exc), "cars",
                     results=rows, query=query, meta=meta)

    outcome = filter_mod.apply(search.offers, filter_mod.build(args))
    rows, meta = _render_cars(args, client, search, outcome, "cars")
    return _emit(args, "cars", rows, bool(outcome.kept), query=query, meta=meta)


def _sweep_budget_refusal(args) -> str:
    """The refusal message for an unaffordable sweep, or "" if it fits.

    The client refuses too, and on the same arithmetic — this exists because
    the client's message states the total only. "168 requests, over 40" leaves
    the reader guessing which knob moves it; showing the multiplication and
    naming a ceiling that works turns a dead end into one re-run.
    """
    span = (args.to_date - args.from_date).days + 1
    if span < 1:
        return ""                      # the client raises the date error itself
    # The deadline actually in force, not the client default: a longer
    # --max-poll-seconds buys more polls per search, so costing the plan at
    # the default would promise a budget the sweep then blows through.
    per_search = ceiling_polls(args.max_poll_seconds)
    confirm = SWEEP_CONFIRM_TOP * per_search
    planned = span * per_search + confirm
    if planned <= args.max_requests:
        return ""
    arithmetic = (f"{span} days x up to {per_search} requests each = "
                  f"{span * per_search}, plus up to {confirm} to confirm the "
                  f"best days = {planned} requests, over the --max-requests "
                  f"ceiling of {args.max_requests}")
    quota = HOURLY_LIMITS.get("cars")
    if quota and planned > quota:
        # Raising the ceiling would not help: the hourly quota would refuse it
        # a moment later. Name the range that does fit instead.
        fits = max(1, (quota - confirm) // per_search)
        return (f"{arithmetic} — and over the {quota}/hour cars quota, so "
                f"raising the ceiling will not help. Sweep about {fits} days "
                f"at a time, or shorten --max-poll-seconds")
    return (f"{arithmetic} — narrow the date range, or pass --max-requests "
            f"{max(DEFAULT_SWEEP_MAX_REQUESTS, planned)}")


def cmd_sweep(args) -> int:
    """Cheapest pickup day across a range."""
    client = _client(args)
    if client.sandbox and not args.sandbox_ok:
        return _fail(args, EXIT_USAGE,
                     "sandbox prices are mock data; a cheapest-day sweep ranks "
                     "on price, so it would rank fiction. Re-run with "
                     "--sandbox-ok only to exercise the mechanics.",
                     "sweep")

    refusal = _sweep_budget_refusal(args)
    if refusal:
        # Up front, before the first request goes out. A sweep that discovers
        # it is unaffordable halfway through has already spent the quota.
        return _fail(args, EXIT_USAGE, refusal, "sweep")

    days = client.sweep_cars(
        args.pickup, args.from_date, args.to_date, args.nights,
        max_requests=args.max_requests, confirm_top=SWEEP_CONFIRM_TOP,
        pickup_type=args.pickup_type, per_day=args.per_day,
        currency=args.currency,
    )
    built = filter_mod.build(args)
    results, rows = [], []
    for day, search, error in days:
        if search is None:
            results.append({"date": day.isoformat(), "error": str(error) if error else "no result"})
            continue
        outcome = filter_mod.apply(search.offers, built)
        best = min(outcome.kept, key=type(outcome.kept[0]).sort_key) if outcome.kept else None
        results.append({
            "date": day.isoformat(),
            "status": search.status,
            "complete": search.complete,
            "offers": len(outcome.kept),
            "cheapest": fmt.offer_row(best, client.sandbox) if best else None,
            "partial": not search.complete,
        })
        if best:
            rows.append([day.isoformat(), best.price_label,
                         best.car.name, best.agency_label,
                         "" if search.complete else f"({search.status})"])

    # Rank the days by what the command was asked: which pickup day is
    # cheapest. Appending in calendar order and then trimming to --limit would
    # return the first N days rather than the best N, hiding a cheaper day
    # later in the range — the same defect that let --limit hide the cheapest
    # car, in a third place. Days with no priced offer sort last.
    def _day_key(entry: dict) -> tuple[int, float]:
        cheapest = entry.get("cheapest") or {}
        total = ((cheapest.get("price") or {}).get("total")
                 if isinstance(cheapest.get("price"), dict) else None)
        return (1, 0.0) if total is None else (0, float(total))

    results.sort(key=_day_key)
    rows.sort(key=lambda r: _day_key(
        next((e for e in results if e["date"] == r[0]), {})))

    if not args.json:
        if rows:
            print(fmt.SANDBOX_BANNER + "\n" if client.sandbox else "", end="")
            print(fmt.table(rows[: args.limit],
                            ["pickup", "price", "car", "agency", "note"]))
        else:
            print("no priced offers on any day in that range")
    found = any(r.get("cheapest") for r in results)
    results = results[: args.limit]
    return _emit(args, "sweep", results, found,
                 query={"pickup": args.pickup, "from": args.from_date.isoformat(),
                        "to": args.to_date.isoformat(), "nights": args.nights},
                 meta={"sandbox": client.sandbox,
                       "prices_are_mocked": client.sandbox,
                       "requests": client.requests_made,
                       "days_scanned": len(days),
                       "ranked_by": "price",
                       "returned": len(results),
                       "truncated": len(results) < len(days)})


def _render_hotels(args, client, search) -> tuple[list[dict], dict]:
    """Shared rendering for `hotels`, used on the complete and partial paths.

    Hotels poll like cars do, so they get the same completion honesty: `meta`
    carries `complete` and `partial`, and the human output says so rather than
    presenting a still-arriving list as the whole market.

    They get the same *ordering* honesty too, and for the same reason the cars
    table does: `--limit` trims after the sort, so a list left in the
    endpoint's own relevance order and then cut to ten rows can drop the
    cheapest hotel out of the answer. Rates are ranked when there are rates to
    rank — when nothing carries one, the endpoint's order stands and
    `meta.ranked_by` says so, because there is nothing to rank on.
    """
    hotels = list(getattr(search, "hotels", ()) or ())
    complete = bool(getattr(search, "complete", False))
    priced = any(h.lowest_rate is not None for h in hotels)
    ranked_by = "price" if priced else "relevance"
    if priced:
        # sort_key pins rate-less hotels last, as Offer.sort_key does for
        # priceless offers: a row with no rate cannot be the cheapest room.
        hotels.sort(key=lambda hotel: hotel.sort_key())
    results = [h.summary() for h in hotels][: args.limit]
    meta = {
        "complete": complete,
        "partial": not complete,
        "ranked_by": ranked_by,
        "total_count": getattr(search, "total_count", None),
        "currency": getattr(search, "currency", ""),
        "occupancy": HOTEL_OCCUPANCY,
        "returned": len(results),
        "truncated": len(results) < len(hotels),
        "matched": len(hotels),
        "requests": client.requests_made,
        "sandbox": client.sandbox,
        "prices_are_mocked": client.sandbox,
    }
    if not args.json:
        if client.sandbox:
            print(fmt.SANDBOX_BANNER + "\n")
        rows = [[h.name, h.star_rating or "?", h.guest_rating or "?",
                 h.lowest_rate if h.lowest_rate is not None else "?"]
                for h in hotels[: args.limit]]
        print(fmt.table(rows, ["hotel", "stars", "rating", "from"])
              if rows else "no hotels found")
        footer = fmt.ranking_footer(
            ranked_by, "no hotel in this result carried a rate to rank on")
        if rows and footer:
            print(f"\n{footer}")
        if rows:
            print(f"\nrates are for {HOTEL_OCCUPANCY} — there is no "
                  f"party-size option yet")
        if rows and len(hotels) > len(rows):
            print(f"\n{len(rows)} of {len(hotels)} shown — raise --limit for more")
        if not complete:
            print("\nthe hotel search had not finished when it stopped — "
                  "these are partial results, not the whole market")
    return results, meta


def cmd_hotels(args) -> int:
    # `--id` addresses one hotel by its own key and needs no destination; the
    # endpoint takes one or the other, so only the destination search insists.
    destination = args.destination or args.place or ""
    if not destination and not args.id:
        return _fail(args, EXIT_USAGE,
                     "give --destination <EntityKey>, e.g. khotel:2589314 "
                     "(resolve one with `places <name> --for hotels`), "
                     "or --id for one specific hotel",
                     "hotels")
    client = _client(args)
    query = {"destination": destination or None,
             "checkin": args.checkin.isoformat(),
             "checkout": args.checkout.isoformat(), "hotel_id": args.id,
             "complete": bool(args.complete),
             "occupancy": HOTEL_OCCUPANCY}
    try:
        search = client.hotels(destination, args.checkin, args.checkout,
                               hotel_id=args.id,
                               only_if_complete=bool(args.complete))
    except SearchTimeout as exc:
        # The caller asked for a finished search and did not get one: exit 5,
        # with the partial rows still rendered so the work is not wasted.
        results, meta = _render_hotels(args, client, exc.partial)
        return _fail(args, EXIT_PARTIAL, str(exc), "hotels",
                     results=results, query=query, meta=meta)

    results, meta = _render_hotels(args, client, search)
    return _emit(args, "hotels", results, bool(results), query=query, meta=meta)


def cmd_when(args) -> int:
    """Cheapest date to fly a route — one call, no polling."""
    client = _client(args)
    search = client.calendar(args.origin, args.destination,
                             args.date_from, args.date_to,
                             aggregation=args.aggregation,
                             round_trip=args.round_trip,
                             non_stop=args.non_stop)
    days = list(getattr(search, "days", ()) or ())
    # Cheapest first, days with no price last — the same rule Offer.sort_key
    # applies, and for the same reason: an unpriced day answers nothing.
    ordered = sorted(days, key=lambda d: d.sort_key())
    results = [d.summary() for d in ordered][: args.limit]
    if not args.json:
        if client.sandbox:
            print(fmt.SANDBOX_BANNER + "\n")
        rows = [[d.key, d.price if d.price is not None else "?",
                 "predicted" if d.predicted else "quoted"]
                for d in ordered[: args.limit]]
        print(fmt.table(rows, ["date", "price", "kind"])
              if rows else "no calendar data for that route")
        if rows and len(ordered) > len(rows):
            print(f"\n{len(rows)} of {len(ordered)} shown — raise --limit for more")
    return _emit(args, "when", results, bool(days),
                 query={"origin": args.origin, "destination": args.destination,
                        "from": args.date_from, "to": args.date_to,
                        "aggregation": args.aggregation,
                        "round_trip": args.round_trip,
                        "non_stop": args.non_stop},
                 meta={"origin_name": getattr(search, "origin_name", ""),
                       "destination_name": getattr(search, "destination_name", ""),
                       "currency": getattr(search, "currency", ""),
                       "returned": len(results),
                       "truncated": len(results) < len(ordered),
                       "sandbox": client.sandbox,
                       "prices_are_mocked": client.sandbox})


# ----------------------------------------------------------------- parser


def _add_global(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="structured output; exit codes are unchanged")
    parser.add_argument("--api-key", help=argparse.SUPPRESS)
    parser.add_argument("--key-file", help="file holding the API key ('-' for stdin)")
    parser.add_argument("--host", help=argparse.SUPPRESS)
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore cached place lookups")


def _add_car_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", type=_csv, metavar="A,B",
                        help="car groups: small, medium, large, suv, van, "
                             "pickupTruck, luxury, convertible, commercial")
    parser.add_argument("--min-passengers", type=int, metavar="N")
    parser.add_argument("--min-bags", type=int, metavar="N")
    parser.add_argument("--min-doors", type=int, metavar="N")
    parser.add_argument("--transmission", choices=("automatic", "manual"))
    parser.add_argument("--fuel", type=_csv, metavar="A,B")
    parser.add_argument("--unlimited-mileage", action="store_true")
    parser.add_argument("--free-cancellation", action="store_true")
    parser.add_argument("--cancel-window", type=float, metavar="HOURS",
                        help="free cancellation still open this many hours before pickup")
    parser.add_argument("--no-credit-card", action="store_true",
                        help="only offers that state no card is required")
    parser.add_argument("--exclude-opaque", action="store_true",
                        help="drop agencies that hide their name until booking")
    parser.add_argument("--exclude-p2p", action="store_true",
                        help="drop peer-to-peer listings")
    parser.add_argument("--agency", type=_csv, metavar="A,B")
    parser.add_argument("--max-price", type=float, metavar="N",
                        help="trip total at or below this")
    parser.add_argument("--sleepable", action="store_true",
                        help="SUV or van with room for four — car-camping sugar")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kayak",
        description="Search KAYAK's affiliate APIs for cars, hotels and fares. "
                    "Read-only: it never books, holds or pays for anything.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="validate and store an API key")
    p.add_argument("--api-key")
    p.add_argument("--key-file", help="file holding the key ('-' for stdin)")
    p.add_argument("--host", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("check", help="is the key usable, and which key is it")
    _add_global(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("places", help="resolve a name to a location id")
    p.add_argument("query")
    p.add_argument("--for", dest="for_", choices=("cars", "flights", "hotels"),
                   default="cars")
    _add_global(p)
    p.set_defaults(func=cmd_places)

    p = sub.add_parser("cars", help="rental cars for one pickup and date pair")
    p.add_argument("--pickup", required=True, help="IATA code or KAYAK city id")
    p.add_argument("--pickup-type", choices=("airport", "city"), default="airport")
    p.add_argument("--drop", help="drop-off location for a one-way rental")
    p.add_argument("--drop-type", choices=("airport", "city"), default="airport")
    p.add_argument("--from", dest="from_date", type=_date, required=True,
                   metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", type=_date, required=True,
                   metavar="YYYY-MM-DD")
    p.add_argument("--pickup-time", type=_clock, default=(10, 0), metavar="HH:MM")
    p.add_argument("--drop-time", type=_clock, default=(10, 0), metavar="HH:MM")
    p.add_argument("--until", choices=(STATUS_SECOND, STATUS_COMPLETE),
                   default=STATUS_COMPLETE,
                   help="stop once the major providers are in, or wait for the "
                        "complete result (default)")
    p.add_argument("--max-poll-seconds", type=float,
                   default=DEFAULT_MAX_POLL_SECONDS,
                   help="wall-clock ceiling; 25 suits claude.ai, 90 Claude Code")
    p.add_argument("--sort", choices=("price", "distance"), default="price")
    p.add_argument("--currency")
    p.add_argument("--per-day", action="store_true",
                   help="price per day instead of the trip total")
    p.add_argument("--limit", type=int, default=10,
                   help="most rows to return; trims --json as well as the table")
    p.add_argument("--full", action="store_true",
                   help="with --json, return the API's raw results[] rows and "
                        "put the agencies/providers/carLocations maps on meta, "
                        "instead of the trimmed projection")
    p.add_argument("--sandbox-ok", action="store_true",
                   help="show sandbox price columns. Passing this does NOT "
                        "make the numbers real — they remain mock data.")
    _add_car_filters(p)
    _add_global(p)
    p.set_defaults(func=cmd_cars)

    p = sub.add_parser("sweep", help="cheapest pickup day across a range")
    p.add_argument("--pickup", required=True)
    p.add_argument("--pickup-type", choices=("airport", "city"), default="airport")
    p.add_argument("--from", dest="from_date", type=_date, required=True)
    p.add_argument("--to", dest="to_date", type=_date, required=True)
    p.add_argument("--nights", type=int, required=True)
    p.add_argument("--max-requests", type=int,
                   default=DEFAULT_SWEEP_MAX_REQUESTS,
                   help="refuse the sweep up front if it could cost more than "
                        f"this (default {DEFAULT_SWEEP_MAX_REQUESTS}; a day "
                        "costs up to a dozen requests)")
    p.add_argument("--max-poll-seconds", type=float, default=DEFAULT_MAX_POLL_SECONDS)
    p.add_argument("--currency")
    p.add_argument("--per-day", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sandbox-ok", action="store_true")
    _add_car_filters(p)
    _add_global(p)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser(
        "hotels",
        help="hotels for a destination and date range",
        description="Hotels for a destination and date range. Rates are for "
                    "one room for two adults — there is no party-size flag "
                    "yet, so quote them as such and not as a per-person or "
                    "family price.",
    )
    p.add_argument("--destination",
                   help="hotel EntityKey, e.g. khotel:2589314 — the `key` "
                        "column of `places <name> --for hotels`, not a place id")
    # The old name. It said "place" and took a placeId, which the endpoint
    # rejects; kept as a hidden alias so an existing command line still runs.
    p.add_argument("--place", help=argparse.SUPPRESS)
    p.add_argument("--checkin", type=_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--checkout", type=_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--id", metavar="ENTITYKEY",
                   help="one specific hotel, by its own EntityKey; "
                        "--destination is then ignored")
    p.add_argument("--complete", action="store_true",
                   help="only accept a finished search; exit 5 with the "
                        "partial rows if it is still arriving. Applies to "
                        "--id lookups as well as destination searches")
    p.add_argument("--max-poll-seconds", type=float,
                   default=DEFAULT_MAX_POLL_SECONDS,
                   help="wall-clock ceiling; 25 suits claude.ai, 90 Claude Code")
    p.add_argument("--limit", type=int, default=10,
                   help="most rows to return; trims --json as well as the table")
    _add_global(p)
    p.set_defaults(func=cmd_hotels)

    p = sub.add_parser("when", help="cheapest date to fly a route")
    p.add_argument("--origin", required=True,
                   help="IATA code or numeric place id")
    p.add_argument("--destination", required=True,
                   help="IATA code or numeric place id")
    p.add_argument("--from", dest="date_from", type=_month, required=True,
                   metavar="YYYY-MM", help="first month of the window")
    p.add_argument("--to", dest="date_to", type=_month, required=True,
                   metavar="YYYY-MM", help="last month of the window")
    p.add_argument("--aggregation", choices=("day", "month"), default="day",
                   help="one row per departure date (default), or per month")
    p.add_argument("--round-trip", action="store_true",
                   help="price a return trip instead of a one-way")
    p.add_argument("--non-stop", action="store_true",
                   help="non-stop fares only")
    p.add_argument("--limit", type=int, default=15,
                   help="most rows to return; trims --json as well as the table")
    _add_global(p)
    p.set_defaults(func=cmd_when)

    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SearchTimeout as exc:
        # Reached only for commands that do not render partials themselves.
        return _fail(args, EXIT_PARTIAL, str(exc), args.command)
    except KayakError as exc:
        return _fail(args, _exit_for(exc), str(exc), args.command)
    except KeyboardInterrupt:
        return 130
