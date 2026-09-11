#!/usr/bin/env python3
"""Self-check for the enterprise skill.

Two groups:

    [offline]  pure logic against a captured fixture. No sockets.
    [network]  live calls, which prove this environment can reach the API.

    python3 test_availability.py --offline   # fast, no network
    python3 test_availability.py             # everything

Exits non-zero on failure so it can gate an install.

What is tested is deliberately skewed towards bugs that fail *silently* - the
answer that looks plausible and is wrong. A crash is easy to notice; a rental
quoted in the wrong currency, or 59 sold-out classes reported as available, is
not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from decimal import Decimal
from pathlib import Path

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from enterprise import filters as vfilters  # noqa: E402
from enterprise.errors import (  # noqa: E402
    AgeRefused, RequestCeiling, RouteRefused, TransportError, UsageError,
)
from enterprise.model import (  # noqa: E402
    Location, QuoteRequest, Vehicle, VehicleStatus, check_dates, format_price,
    parse_age_policy, parse_amount, parse_hours, parse_quote, parse_when,
    sort_by_total,
)
from enterprise.rentals import Plan, date_windows  # noqa: E402

FIXTURE = Path(_HERE) / "fixtures" / "quote_yhz.json"
FRA_FIXTURE = Path(_HERE) / "fixtures" / "quote_fra_de.json"

_PASS, _FAIL = [], []


def check(group: str, name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASS.append(name)
        print(f"  [{group}] PASS  {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  [{group}] FAIL  {name}  {detail}")


def raises(exc, fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _branch(country: str = "CA", id_: str = "1019286") -> Location:
    return Location(
        id=id_, name="Halifax International Airport", kind="airport",
        location_type="BRANCH", airport_code="YHZ", city="Enfield",
        country=country, currency="CAD", latitude=44.88, longitude=-63.5,
    )


def _request(**kw) -> QuoteRequest:
    pickup = kw.pop("pickup", _branch())
    return QuoteRequest(
        pickup=pickup, dropoff=kw.pop("dropoff", pickup),
        pickup_time="2026-10-15T10:00", return_time="2026-10-18T10:00", **kw
    )



def _fanout_code(exc: Exception) -> int:
    """Run `sweep` with every quote raising `exc`, and return the exit code."""
    import contextlib
    import io

    import enterprise.locations as locations_mod
    import enterprise.rentals as rentals_mod
    branch = Location(
        id="1019286", name="Halifax International Airport", kind="airport",
        location_type="BRANCH", airport_code="YHZ", city="Enfield",
        country="CA", currency="CAD", latitude=44.88, longitude=-63.5,
    )
    original_resolve = locations_mod.LocationClient.resolve
    original_quote = rentals_mod.RentalClient.quote
    locations_mod.LocationClient.resolve = lambda self, query: branch

    def dead(self, request):
        raise exc

    rentals_mod.RentalClient.quote = dead
    try:
        from enterprise.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main([
                "sweep", "YHZ", "--start", "2026-10-01",
                "--end", "2026-10-08", "--nights", "3",
            ])
    finally:
        locations_mod.LocationClient.resolve = original_resolve
        rentals_mod.RentalClient.quote = original_quote




def _multi_age_quote(ages: list[str] | None = None) -> tuple[str, int]:
    """Quote several ages where the youngest is refused; return output+code."""
    import contextlib
    import io

    import enterprise.locations as locations_mod
    import enterprise.rentals as rentals_mod
    from enterprise.errors import AgeRefused

    branch = Location(
        id="1019286", name="Halifax International Airport", kind="airport",
        location_type="BRANCH", airport_code="YHZ", city="Enfield",
        country="CA", currency="CAD", latitude=44.88, longitude=-63.5,
    )
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    original_resolve = locations_mod.LocationClient.resolve
    original_quote = rentals_mod.RentalClient.quote
    locations_mod.LocationClient.resolve = lambda self, query: branch

    def quote(self, request):
        if request.age < 21:
            raise AgeRefused("renter is too young for this location")
        return parse_quote(raw, request)

    rentals_mod.RentalClient.quote = quote
    argv = ["quote", "YHZ", "--pickup-time", "2026-10-15",
            "--return-time", "2026-10-18"]
    for age in ages or ["25", "19"]:
        argv += ["--age", age]
    try:
        from enterprise.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return out.getvalue(), code
    finally:
        locations_mod.LocationClient.resolve = original_resolve
        rentals_mod.RentalClient.quote = original_quote




def _german_fleet() -> list:
    """Vehicles from a REAL Frankfurt de_DE response.

    This was a hand-written fixture, and the hand-written value was wrong: it
    asserted `Kleinbusse` was category code 500, so `--class van` passed
    offline while matching 0 of 9 vans live. Frankfurt actually returns FOUR
    categories - `Kleinbusse` is **400** and 500 is `Transporter` (cargo vans),
    a code Canada never shows. Captured data, not invented data, is the only
    thing that catches that.
    """
    raw = json.loads(FRA_FIXTURE.read_text(encoding="utf-8"))
    return list(parse_quote(raw, _request()).vehicles)



def _no_network_exit(argv: list[str]) -> tuple[int, int]:
    """Run a command with all network stubbed; return (exit code, calls made).

    A second element of 0 proves the refusal happened before any HTTP request -
    which is the whole point, given the host rate-limits without warning.
    """
    import contextlib
    import io

    import enterprise.locations as locations_mod
    import enterprise.rentals as rentals_mod

    calls = {"n": 0}
    originals = (
        locations_mod.LocationClient.search,
        locations_mod.LocationClient.by_id,
        rentals_mod.RentalClient.quote,
    )

    def counted(*_a, **_kw):
        calls["n"] += 1
        raise AssertionError("network was touched")

    locations_mod.LocationClient.search = counted
    locations_mod.LocationClient.by_id = counted
    rentals_mod.RentalClient.quote = counted
    try:
        from enterprise.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, calls["n"]
    finally:
        (locations_mod.LocationClient.search,
         locations_mod.LocationClient.by_id,
         rentals_mod.RentalClient.quote) = originals




def _compare_with_failure() -> tuple[list, int]:
    """Run `compare --limit 2` over three branches where one fails."""
    import contextlib
    import io

    import enterprise.locations as locations_mod
    import enterprise.rentals as rentals_mod
    from enterprise.errors import TransportError

    def branch(i: str, name: str) -> Location:
        return Location(
            id=i, name=name, kind="airport", location_type="BRANCH",
            airport_code=name[:3].upper(), city="x", country="CA",
            currency="CAD", latitude=0.0, longitude=0.0,
        )

    lookup = {"a": branch("1", "Alpha"), "b": branch("2", "Beta"),
              "c": branch("3", "Gamma")}
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    originals = (locations_mod.LocationClient.resolve,
                 rentals_mod.RentalClient.quote)
    locations_mod.LocationClient.resolve = lambda self, q: lookup[q]

    def quote(self, request):
        if request.pickup.id == "3":
            raise TransportError("host refused")
        return parse_quote(raw, request)

    rentals_mod.RentalClient.quote = quote
    try:
        from enterprise.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(["compare", "a", "b", "c", "--pickup-time",
                         "2026-11-17", "--return-time", "2026-11-20",
                         "--limit", "2", "--json"])
        return json.loads(out.getvalue()), code
    finally:
        (locations_mod.LocationClient.resolve,
         rentals_mod.RentalClient.quote) = originals




def _contract_cases() -> list:
    """Run each array-returning command so its payload has MIXED outcomes.

    The point is rows that differ: a priced row beside a failed one, a quote
    where one age is refused beside one that worked. A homogeneous payload
    cannot expose a key-set divergence, so the earlier version of this helper -
    single `--age 25`, and a `--max-price 1` that emptied every window - proved
    nothing while appearing to.
    """
    import contextlib
    import io

    import enterprise.locations as locations_mod
    import enterprise.rentals as rentals_mod
    from enterprise.errors import AgeRefused, TransportError

    def branch(i: str, name: str) -> Location:
        return Location(
            id=i, name=name, kind="airport", location_type="BRANCH",
            airport_code=name[:3].upper(), city="x", country="CA",
            currency="CAD", latitude=0.0, longitude=0.0,
        )

    lookup = {k: branch(str(n + 1), k.title())
              for n, k in enumerate(("alpha", "beta", "gamma"))}
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    originals = (locations_mod.LocationClient.resolve,
                 rentals_mod.RentalClient.quote)
    locations_mod.LocationClient.resolve = lambda self, q: lookup.get(q, lookup["alpha"])

    def quote(self, request):
        # Mixed outcomes: a refused age, a failing branch, and one date that
        # errors so a sweep gets priced AND failed rows in one payload.
        if request.age < 21:
            raise AgeRefused("renter is too young for this location")
        if request.pickup.id == "3" or request.pickup_time.startswith("2026-11-19"):
            raise TransportError("upstream 502")
        return parse_quote(raw, request)

    rentals_mod.RentalClient.quote = quote
    from enterprise.cli import main

    def run(argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            main(argv)
        return out.getvalue()

    dates = ["--pickup-time", "2026-11-17", "--return-time", "2026-11-20"]
    cases = []
    try:
        args = ["compare", "alpha", "beta", "gamma", *dates, "--limit", "1"]
        rows = json.loads(run(args + ["--json"]))
        body = [l for l in run(args).splitlines()
                if l[:3].isupper() and "CODE" not in l and l.strip()]
        cases.append(("compare, one branch failing", rows, len(body)))

        # Priced windows, a filtered-empty window and a failed window together.
        # --seats 9 leaves most windows empty (a `note` row) while 2026-11-19
        # still fails (an `error` row) - all three sweep row shapes at once.
        rows = json.loads(run([
            "sweep", "alpha", "--start", "2026-11-17", "--end", "2026-11-22",
            "--nights", "1", "--seats", "9", "--json",
        ]))
        cases.append(("sweep, empty and failed windows", rows, None))
        rows = json.loads(run([
            "sweep", "alpha", "--start", "2026-11-17", "--end", "2026-11-22",
            "--nights", "1", "--json",
        ]))
        cases.append(("sweep, priced and failed windows", rows, None))

        # One age refused, one priced - the rows must still match.
        rows = json.loads(run([
            "quote", "alpha", *dates, "--age", "25", "--age", "19", "--json",
        ]))
        cases.append(("quote, one age refused", rows, None))
    finally:
        (locations_mod.LocationClient.resolve,
         rentals_mod.RentalClient.quote) = originals
    return [c for c in cases if c[1]]



# --------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------


def offline() -> None:
    g = "offline"
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    quote = parse_quote(raw, _request())
    by_code = {v.code: v for v in quote.vehicles}

    # -- the currency trap -------------------------------------------------
    # The fixture was captured with view_currency_code=USD against a CAD
    # branch, so charged and converted genuinely disagree. That is the whole
    # point of it.
    priced = by_code["CFDR"]
    check(g, "charged price is the branch currency",
          priced.total.charged.currency == "CAD", str(priced.total.charged))
    check(g, "converted estimate is kept separate",
          priced.total.converted is not None
          and priced.total.converted.currency == "USD",
          str(priced.total.converted))
    check(g, "converted amount differs from charged",
          priced.total.converted.amount != priced.total.charged.amount)
    check(g, "format_price leads with the charged amount",
          format_price(priced.total).startswith(str(priced.total.charged)))
    check(g, "format_price labels the conversion an estimate",
          "est." in format_price(priced.total))
    check(g, "Price exposes no bare .amount",
          not hasattr(priced.total, "amount"))
    check(g, "Price is not float-coercible",
          not hasattr(priced.total, "__float__"))
    # A sort that read `converted` would order these differently only when the
    # exchange rate reorders them; assert the key itself instead.
    check(g, "sort key is the charged amount",
          priced.total.sort_key == priced.total.charged.amount)

    # -- sold out is not available ----------------------------------------
    sold = by_code["XCAR"]
    check(g, "SOLD_OUT parses as sold out", sold.status is VehicleStatus.SOLD_OUT)
    check(g, "SOLD_OUT has no price", sold.total is None)
    check(g, "SOLD_OUT is not bookable", not sold.bookable)
    check(g, "bookable count excludes sold out",
          len(quote.bookable) < len(quote.vehicles),
          f"{len(quote.bookable)} of {len(quote.vehicles)}")
    check(g, "RESTRICTED is not bookable", not by_code["ZZRS"].bookable)
    # Restricted classes belong to neither the bookable nor the sold-out
    # tally, so without their own line the footer arithmetic does not add up
    # and the reader cannot tell where the missing classes went.
    check(g, "restricted classes are accounted for separately",
          [v.code for v in quote.restricted] == ["ZZRS"])
    accounted = (
        len(quote.bookable)
        + sum(quote.sold_out_categories.values())
        + len(quote.restricted)
    )
    check(g, "bookable + sold out + restricted covers every class",
          accounted == len(quote.vehicles),
          f"{accounted} of {len(quote.vehicles)}")

    # -- facets that lie ---------------------------------------------------
    # UDAR carries a DRIVE filter_code with no filter_description. Left raw it
    # renders as "112" and, worse, an AWD class whose code is 59 would fail an
    # AWD filter because "4 wheel" is not in "59".
    code_only = by_code["UDAR"]
    check(g, "facet code resolves to a description",
          code_only.drive is not None and not code_only.drive.isdigit()
          and "wheel" in code_only.drive.lower(),
          repr(code_only.drive))
    check(g, "absent DRIVE facet is None, not a guess",
          by_code["ZZND"].drive is None, repr(by_code["ZZND"].drive))
    check(g, "absent DRIVE is not treated as AWD", not by_code["ZZND"].is_awd)
    check(g, "capped mileage is detected",
          by_code["ZZCM"].unlimited_mileage is False)

    # -- filters -----------------------------------------------------------
    def args(**kw):
        return argparse.Namespace(**{s.dest: kw.get(s.dest) for s in vfilters.SPECS})

    awd = vfilters.apply(quote.vehicles, vfilters.build(args(drive="awd")))
    check(g, "--drive awd matches only AWD",
          awd and all(v.is_awd for v in awd), f"{len(awd)} matched")
    check(g, "--drive awd excludes the absent-facet class",
          by_code["ZZND"] not in awd)

    seats = vfilters.apply(quote.vehicles, vfilters.build(args(seats=5)))
    check(g, "--seats is a >= threshold", all(v.seats >= 5 for v in seats))

    capped_out = vfilters.apply(
        quote.vehicles, vfilters.build(args(unlimited_mileage=True))
    )
    check(g, "--unlimited-mileage drops capped classes",
          by_code["ZZCM"] not in capped_out)

    cheap = vfilters.apply(
        quote.bookable, vfilters.build(args(max_price=Decimal("1")))
    )
    check(g, "--max-price compares against the charged amount", cheap == [])

    sleepable = vfilters.apply(quote.bookable, vfilters.build(args(sleepable=True)))
    check(g, "--sleepable requires AWD + unlimited mileage",
          all(v.is_awd and v.unlimited_mileage and v.seats >= 5 for v in sleepable),
          f"{[v.code for v in sleepable]}")

    # -- filters must survive a translated locale --------------------------
    # At a German branch the API returns `Mietwagen` for Cars, `Kleinbusse`
    # for Vans and `Vierrad- oder Allradantrieb` for all-wheel drive. Matching
    # on those descriptions made every filter return nothing while the
    # vehicles sat right there - a plausible, wrong answer. Facet CODES do not
    # move between locales, so predicates match on those.
    german = _german_fleet()

    def de_filter(**kw):
        """Filter the bookable German classes, as the CLI does."""
        bookable = [v for v in german if v.bookable]
        return [v.code for v in vfilters.apply(bookable, vfilters.build(args(**kw)))]

    check(g, "--class car matches a German 'Mietwagen'",
          de_filter(**{"class": "car"}) == ["PDAR"],
          str(de_filter(**{"class": "car"})))
    # The regression that started this: Kleinbusse is 400, not 500.
    check(g, "--class van matches a German 'Kleinbusse' (code 400)",
          "SVMR" in de_filter(**{"class": "van"}),
          str(de_filter(**{"class": "van"})))
    check(g, "--class minibus is specific to the people carrier",
          de_filter(**{"class": "minibus"}) == ["SVMR"])
    check(g, "--class suv still matches where the word is unchanged",
          set(de_filter(**{"class": "suv"})) == {"PFAR", "IFMR"},
          str(de_filter(**{"class": "suv"})))
    check(g, "--drive awd matches 'Vierrad- oder Allradantrieb'",
          de_filter(drive="awd") == ["PFAR"], str(de_filter(drive="awd")))
    check(g, "a literal \"null\" drive facet is not treated as a value",
          all(v.drive is None for v in german if v.code in ("SVMR", "PDAR")))
    check(g, "--transmission manual matches 'Schaltgetriebe'",
          set(de_filter(transmission="manual")) == {"SVMR", "IFMR"},
          str(de_filter(transmission="manual")))
    check(g, "--transmission automatic excludes the manuals",
          "IFMR" not in de_filter(transmission="automatic"))
    check(g, "--sleepable works at a non-English branch",
          de_filter(sleepable=True) == ["PFAR"], str(de_filter(sleepable=True)))

    ordered = sort_by_total(quote.bookable)
    check(g, "sorting is ascending by charged total",
          all(
              ordered[i].total.sort_key <= ordered[i + 1].total.sort_key
              for i in range(len(ordered) - 1)
          ))

    # -- refusals must not look like empty results -------------------------
    age_body = {
        "messages": [{"code": "PRICING_4463", "message": "age", "priority": "ERROR"}]
    }
    check(g, "age refusal raises rather than returning empty",
          raises(AgeRefused, lambda: parse_quote(age_body, _request(age=19))))

    no_cars = {
        "messages": [
            {"code": "PRICING_16007", "message": "no vehicles", "priority": "ERROR"}
        ]
    }
    same_country = parse_quote(no_cars, _request())
    check(g, "same-country sell-out is an empty quote, not an error",
          same_country.vehicles == () and same_country.bookable == [])

    us = _branch(country="US", id_="1018991")
    check(g, "cross-border one-way refusal is distinguished",
          raises(RouteRefused, lambda: parse_quote(no_cars, _request(dropoff=us))))

    # A numeric id whose detail lookup failed has country=None. Guessing a
    # country there silently disabled cross-border detection, so an unknown
    # country must be treated as "might be cross-border", not as domestic.
    unknown = Location(
        id="1018991", name="Location 1018991", kind="branch",
        location_type="BRANCH", airport_code=None, city=None, country=None,
        currency=None, latitude=None, longitude=None,
    )
    check(g, "unknown branch country is not assumed domestic",
          not _request(dropoff=unknown).countries_known)
    check(g, "one-way with an unknown country is treated as maybe-cross-border",
          _request(dropoff=unknown).maybe_cross_border)
    check(g, "unknown-country one-way refusal is not a silent sell-out",
          raises(RouteRefused, lambda: parse_quote(no_cars, _request(dropoff=unknown))))
    check(g, "a same-branch round trip is never maybe-cross-border",
          not _request().maybe_cross_border)

    horizon = {
        "messages": [{"code": "PRICING_220", "message": "too far", "priority": "ERROR"}]
    }
    check(g, "beyond-horizon dates raise a usage error",
          raises(UsageError, lambda: parse_quote(horizon, _request())))

    # -- the per-day figure must be derived, never read from the API -------
    # `rates[0]` can be a pre-tax daily base OR a weekly rate depending on
    # branch and duration; labelling it "per day" quoted a Kia Rio at $372/day
    # against a $510 six-day total.
    req6 = QuoteRequest(
        pickup=_branch(), dropoff=_branch(),
        pickup_time="2026-12-01T10:00", return_time="2026-12-07T10:00",
    )
    check(g, "rental_days counts billable days", req6.rental_days == 6,
          str(req6.rental_days))
    check(g, "a rental just over 24h counts as two days",
          QuoteRequest(pickup=_branch(), dropoff=_branch(),
                       pickup_time="2026-12-01T10:00",
                       return_time="2026-12-02T10:30").rental_days == 2)
    check(g, "an exact 24h rental stays one day",
          QuoteRequest(pickup=_branch(), dropoff=_branch(),
                       pickup_time="2026-12-01T10:00",
                       return_time="2026-12-02T10:00").rental_days == 1)
    check(g, "a part-day rental rounds up",
          QuoteRequest(pickup=_branch(), dropoff=_branch(),
                       pickup_time="2026-12-01T10:00",
                       return_time="2026-12-02T11:00").rental_days == 2)
    per_day = priced.per_day(6)
    check(g, "per_day is total divided by days",
          per_day is not None
          and per_day.amount == (priced.total.charged.amount / 6).quantize(
              Decimal("0.01")),
          str(per_day))
    check(g, "per_day keeps the charged currency",
          per_day.currency == priced.total.charged.currency)
    check(g, "per_day is not the API's own rate line",
          priced.daily is None or per_day.amount != priced.daily.charged.amount
          or priced.rate_period == "DAILY")
    check(g, "a non-daily API rate is flagged",
          Vehicle.parse({"code": "X", "charges": {"PAYLATER": {"rates": [
              {"unit_rate_type": "WEEKLY",
               "unit_amount_payment": {"amount": "371.65", "code": "CAD"}}]}}
          }).rate_note == "weekly")
    check(g, "a daily API rate needs no flag",
          Vehicle.parse({"code": "X", "charges": {"PAYLATER": {"rates": [
              {"unit_rate_type": "DAILY",
               "unit_amount_payment": {"amount": "50", "code": "CAD"}}]}}
          }).rate_note is None)

    # -- the API's rate lines are not a daily rate, and not the whole price --
    # Measured: the DAILY->WEEKLY switch happens between 4 and 5 nights, and a
    # 30-night rental prices as WEEKLY x4 + EXTRA_DAILY x2. So `rates[0]` is
    # neither a daily figure nor the total, which is exactly why the PER DAY
    # column is derived from the total instead.
    long_raw = json.loads(
        (Path(_HERE) / "fixtures" / "rates_long.json").read_text(encoding="utf-8")
    )
    long_v = {v.code: v for v in parse_quote(long_raw, _request()).vehicles}["IFMR"]
    check(g, "a 14-night rental is priced WEEKLY, not daily",
          long_v.rate_period == "WEEKLY", str(long_v.rate_period))
    check(g, "a weekly rate is flagged rather than shown as daily",
          long_v.rate_note == "weekly", str(long_v.rate_note))
    check(g, "the derived per-day divides the TOTAL, not the rate line",
          long_v.per_day(14).amount == (
              long_v.total.charged.amount / Decimal(14)).quantize(Decimal("0.01")),
          f"{long_v.per_day(14)} vs rate {long_v.daily.charged}")

    multi_raw = json.loads(
        (Path(_HERE) / "fixtures" / "rates_multiline.json").read_text(encoding="utf-8")
    )
    multi = {v.code: v for v in parse_quote(multi_raw, _request()).vehicles}["IFMR"]
    check(g, "multiple rate lines are counted", multi.rate_lines == 2,
          str(multi.rate_lines))
    check(g, "multi-line pricing names the period AND the structure",
          multi.rate_note == "weekly plus extras, across 2 rate lines",
          str(multi.rate_note))
    # The surprising case must not say LESS than the simple one: the earlier
    # wording dropped the period entirely for multi-line rentals.
    check(g, "the multi-line note still names the rate period",
          "weekly" in (multi.rate_note or ""), str(multi.rate_note))
    check(g, "rates[0] alone would understate the price",
          multi.daily.charged.amount < multi.total.charged.amount,
          f"{multi.daily.charged} vs {multi.total.charged}")

    # -- points are per-day and must never be divided into a trip total -----
    pointed = next((v for v in quote.vehicles if v.points_per_day), None)
    check(g, "points are carried as a per-day figure",
          pointed is not None and pointed.points_per_day > 0,
          str(pointed.points_per_day if pointed else None))
    check(g, "no cents-per-point is computed from an unknowable day count",
          not hasattr(pointed, "cents_per_point"))

    # -- money arguments must fail as USAGE errors, never as tracebacks -----
    # decimal.InvalidOperation inherits from ArithmeticError, not ValueError,
    # so argparse did not recognise it: a typo'd --below crashed and exited 1,
    # the one code meaning "keep waiting". A scheduled watch would poll forever
    # on a query that never ran.
    check(g, "a non-numeric amount is rejected",
          raises(ValueError, lambda: parse_amount("cheap")))
    check(g, "a currency-prefixed amount is rejected",
          raises(ValueError, lambda: parse_amount("$400")))
    check(g, "a negative amount is rejected",
          raises(ValueError, lambda: parse_amount("-5")))
    check(g, "a thousands separator is accepted",
          parse_amount("1,200") == Decimal("1200"))
    # Decimal("nan") parses cleanly and only explodes at comparison time,
    # surfacing as a network error rather than a bad argument.
    check(g, "NaN is rejected at parse time",
          raises(ValueError, lambda: parse_amount("nan")))
    check(g, "Infinity is rejected at parse time",
          raises(ValueError, lambda: parse_amount("Infinity")))
    check(g, "a plain amount is accepted", parse_amount("400.50") == Decimal("400.50"))

    # -- every command validates dates BEFORE touching the network ----------
    # sweep built its requests inline and so skipped validation entirely - the
    # one command that multiplies the cost of a bad date.
    check(g, "sweep refuses past dates without pricing anything",
          _no_network_exit(["sweep", "YHZ", "--start", "2019-01-01",
                            "--end", "2019-01-05", "--nights", "3"]) == (2, 0))
    check(g, "quote reports the date fault, not a branch lookup",
          _no_network_exit(["quote", "zzznotabranch", "--pickup-time",
                            "2025-01-10", "--return-time", "2025-01-05"]) == (2, 0))
    check(g, "branch rejects an unparseable --date locally",
          _no_network_exit(["branch", "YHZ", "--date", "next tuesday"]) == (2, 0))
    check(g, "compare validates dates before resolving branches",
          _no_network_exit(["compare", "YHZ", "YQM", "--pickup-time",
                            "2019-01-01", "--return-time", "2019-01-04"]) == (2, 0))

    # -- dates are validated locally, not by spending a pricing call --------
    from datetime import datetime as _dt
    now = _dt(2026, 6, 1, 12, 0)
    check(g, "reversed dates are rejected locally",
          raises(UsageError, lambda: check_dates(
              "2026-07-10T10:00", "2026-07-05T10:00", now=now)))
    check(g, "past pickup is rejected locally",
          raises(UsageError, lambda: check_dates(
              "2026-05-01T10:00", "2026-05-04T10:00", now=now)))
    check(g, "beyond the 395-day horizon is rejected locally",
          raises(UsageError, lambda: check_dates(
              "2027-08-01T10:00", "2027-08-04T10:00", now=now)))
    check(g, "a valid range passes",
          check_dates("2026-07-01T10:00", "2026-07-04T10:00", now=now) is None)
    try:
        check_dates("2026-05-10T10:00", "2026-05-05T10:00", now=now)
        both = ""
    except UsageError as exc:
        both = str(exc)
    check(g, "every date fault is reported at once, not just the first",
          "past" in both and "not after" in both, both)

    # -- request shaping ---------------------------------------------------
    check(g, "bare date defaults to a time", parse_when("2026-10-15") == "2026-10-15T10:00")
    check(g, "full timestamp is preserved",
          parse_when("2026-10-15T16:30") == "2026-10-15T16:30")
    check(g, "nonsense date is rejected", raises(UsageError, lambda: parse_when("soon")))

    body = _request().as_body()
    check(g, "filters are sent empty (server-side filtering is inert)",
          body["applied_vehicle_class_filters"] == [])
    check(g, "one-way flag is requested", body["check_if_oneway_allowed"] is True)
    check(g, "cross-border is detected",
          _request(dropoff=us).is_cross_border and _request().is_cross_border is False)

    # -- fan-out ceiling ---------------------------------------------------
    windows = date_windows("2026-10-01", "2026-10-08", 3)
    check(g, "date windows fit inside the range",
          windows[0] == ("2026-10-01", "2026-10-04")
          and windows[-1] == ("2026-10-05", "2026-10-08"),
          str(windows))
    check(g, "a rental that cannot fit is rejected",
          raises(UsageError, lambda: date_windows("2026-10-01", "2026-10-02", 5)))
    check(g, "reversed range is rejected",
          raises(UsageError, lambda: date_windows("2026-10-10", "2026-10-01", 2)))
    check(g, "ceiling refuses before sending",
          raises(RequestCeiling, lambda: Plan(50, "50").check(40)))
    check(g, "plan under the ceiling is allowed", Plan(10, "10").check(40) is None)

    # -- branch hours and age policy --------------------------------------
    # These parse a second, differently-shaped endpoint. Untested, a wrong
    # guess at the envelope makes the hours table silently vanish while the
    # command still exits 0.
    hours_raw = json.loads(
        (Path(_HERE) / "fixtures" / "hours_yhz.json").read_text(encoding="utf-8")
    )
    days = parse_hours(hours_raw)
    check(g, "hours parse into one row per date", len(days) == 5, f"{len(days)} rows")
    check(g, "counter hours are read", any("-" in d.counter for d in days))
    check(g, "after-hours drop-off is read separately",
          any(d.drop == "24 hours" for d in days))
    check(g, "a closed day is reported closed, not blank",
          any(d.counter == "closed" for d in days))
    check(g, "dates come back sorted", [d.date for d in days] == sorted(d.date for d in days))
    check(g, "a wrong envelope yields no rows rather than junk",
          parse_hours({"unexpected": {}}) == [] and parse_hours(None) == [])
    check(g, "an unknown age policy says so instead of asserting one",
          "not reported" in parse_age_policy(None))
    check(g, "a stated minimum age is used",
          "23" in parse_age_policy({"minimum_age": 23}))

    # -- a poisoned cache entry must not become "does not exist" ----------
    # A renter-age payload was found cached under a branch-detail key, and the
    # lookup turned that into a hard LocationNotFound for Halifax airport - the
    # very id the ambiguity error tells users to pass.
    from enterprise import cache as _cache

    def valid(value):
        return isinstance(value, dict) and value.get("ok") is True

    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"ok": True}

    ns = f"selfcheck{os.getpid()}"
    _cache.clear()
    first = _cache.get_or_fetch(ns, "k", fetch, validate=valid)
    second = _cache.get_or_fetch(ns, "k", fetch, validate=valid)
    check(g, "a valid value is cached and reused",
          first == second and calls["n"] == 1, f"{calls['n']} fetches")

    # Poison BOTH tiers the way the real bug did - a valid disk copy would
    # otherwise satisfy the lookup and mask whether memory was checked.
    _cache._MEMORY[f"{ns}/k"] = (time.time(), {"minimum_age": 21})
    poisoned_file = None
    if _cache._ROOT:
        poisoned_file = _cache._ROOT / ns / "k.json"
        if poisoned_file.exists():
            poisoned_file.write_text('{"minimum_age": 21}', encoding="utf-8")
    calls["n"] = 0
    healed = _cache.get_or_fetch(ns, "k", fetch, validate=valid)
    check(g, "a poisoned entry is refetched, never served",
          healed == {"ok": True} and calls["n"] == 1, f"{calls['n']} fetches")
    if poisoned_file is not None:
        check(g, "the poisoned file self-heals on disk",
              not poisoned_file.exists()
              or json.loads(poisoned_file.read_text(encoding="utf-8")) == {"ok": True},
              poisoned_file.read_text(encoding="utf-8")[:60])

    calls["n"] = 0
    bad = _cache.get_or_fetch(ns, "bad", lambda: {"error": "nope"}, validate=valid)
    again = _cache.get_or_fetch(ns, "bad", fetch, validate=valid)
    check(g, "an invalid response is never stored",
          bad == {"error": "nope"} and again == {"ok": True} and calls["n"] == 1,
          f"{calls['n']} fetches")

    check(g, "a raising validator counts as unusable, not a crash",
          _cache.get_or_fetch(
              ns, "boom", lambda: {"ok": True},
              validate=lambda v: (_ for _ in ()).throw(KeyError("x")),
          ) == {"ok": True})
    _cache.clear()

    # -- table alignment with double-width glyphs --------------------------
    # len() counts a CJK glyph as one column, skewing every column to its
    # right in a Tokyo branch listing.
    from enterprise.cli import _table, _width
    check(g, "a CJK glyph counts as two columns", _width("東京") == 4)
    check(g, "an ASCII string is unchanged", _width("Tokyo") == 5)
    aligned = _table(("A", "B"), [("東京", "x"), ("ab", "y")]).splitlines()

    def display_width(text: str) -> int:
        """Deliberately independent of the module under test.

        The earlier assertion measured with `_width` itself, so breaking
        `_width` broke the ruler as well as the table and the test stayed
        green. A test must not share its subject's bug.
        """
        wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in "WF")
        return len(text) + wide

    starts = {
        display_width(line[: line.rindex(line.split("  ")[-1])])
        for line in aligned
    }
    check(g, "the final column starts at the same display offset in every row",
          len(starts) == 1, f"offsets {sorted(starts)} in {aligned}")

    # -- OUTPUT CONTRACT ---------------------------------------------------
    # Every bug in the last three rounds was one of two shapes: a JSON row
    # missing fields its siblings have, or a table and its payload disagreeing.
    # Enumerate the commands rather than catching each instance by hand.
    for label, rows, table_count in _contract_cases():
        keys = [frozenset(r) for r in rows]
        check(g, f"{label}: every JSON row has the same keys",
              len(set(keys)) <= 1,
              f"differing keys: {sorted(set().union(*keys) - set.intersection(*[set(k) for k in keys])) if keys else ''}")
        if table_count is not None:
            check(g, f"{label}: table and JSON agree on row count",
                  table_count == len(rows), f"table={table_count} json={len(rows)}")

    # -- a constraint that excludes results must be named ------------------
    # `--below` is not in SPECS, so it missed the filter echo entirely: four
    # classes were excluded by a threshold the output never mentioned, on the
    # one command where "is it sold out or is my limit too low?" is the whole
    # question.
    ns = argparse.Namespace(**{s.dest: None for s in vfilters.SPECS})
    check(g, "an out-of-SPECS constraint still appears in the echo",
          vfilters.describe(ns, below=Decimal("500")) == "below=500",
          vfilters.describe(ns, below=Decimal("500")))
    check(g, "an unset extra constraint is not echoed",
          vfilters.describe(ns, below=None) == "")
    ns.seats = 5
    check(g, "extras and SPECS filters appear together",
          "below=500" in vfilters.describe(ns, below=Decimal("500"))
          and "seats=5" in vfilters.describe(ns, below=Decimal("500")))

    # -- truncation and filtering must never be silent ---------------------
    from enterprise.cli import _no_match_reason
    check(g, "an empty result from a filter is not called 'nothing bookable'",
          _no_match_reason(5) == "no match (5 bookable)", _no_match_reason(5))
    check(g, "a genuinely empty branch still says nothing bookable",
          _no_match_reason(0) == "nothing bookable")

    # A failed branch sorted last, so --limit cut it first and the JSON looked
    # like a clean, complete comparison at exit 0.
    rows, code = _compare_with_failure()
    check(g, "--limit never hides a failed branch from JSON",
          any(r["error"] for r in rows), f"{len(rows)} rows, no error field")
    check(g, "the failure count is carried in the payload",
          rows and rows[0]["branches_failed"] == 1,
          str(rows[0].get("branches_failed") if rows else None))

    # -- diagnostics must never land on stdout -----------------------------
    # `doctor` runs the transport verbose unconditionally, so one log line on
    # stdout made `doctor --json` unparseable - during exactly the incident
    # SKILL.md tells the agent to run it for.
    import contextlib as _ctx
    import io as _io

    import enterprise.http as _http

    _t = _http.Transport(verbose=True)
    _out, _err = _io.StringIO(), _io.StringIO()
    with _ctx.redirect_stdout(_out), _ctx.redirect_stderr(_err):
        _t._log("hello")
    check(g, "transport diagnostics go to stderr, not stdout",
          _out.getvalue() == "" and "hello" in _err.getvalue(),
          f"stdout={_out.getvalue()!r}")

    # -- transport detection ----------------------------------------------
    from enterprise.http import _Response
    check(g, "an HTML body is recognised as not-JSON",
          _Response(403, "<html>denied</html>").looks_like_html)
    check(g, "a JSON body is not mistaken for HTML",
          not _Response(200, '{"ok":true}').looks_like_html)

    # -- the transport ladder actually degrades ---------------------------
    # A socket-level failure must become a TransportError, or the fallback
    # rungs never run and the curl escape hatch is dead code. This is the bug
    # that made the ladder decorative: `requests` raises its own exception
    # types, which no `except TransportError` will ever catch.
    import requests as _requests
    import enterprise.http as _http

    def ladder(exc):
        transport = _http.Transport()
        seen: list[str] = []

        class Boom:
            def request(self, *a, **kw):
                raise exc

        transport._session = lambda rung: (seen.append(rung), Boom())[1]
        transport._via_curl = lambda *a, **kw: (_ for _ in ()).throw(
            _http.TransportError("curl unavailable")
        )
        original, _http.RETRIES = _http.RETRIES, 1
        try:
            transport.request_json("GET", "https://example.invalid/x", {})
            return seen, None
        except Exception as raised:  # noqa: BLE001 - the type is the assertion
            return seen, raised
        finally:
            _http.RETRIES = original

    for label, exc in (
        ("connection reset", _requests.exceptions.ConnectionError("reset")),
        ("stale CA bundle", _requests.exceptions.SSLError("verify failed")),
        ("timeout", _requests.exceptions.Timeout("timed out")),
    ):
        seen, raised = ladder(exc)
        check(g, f"{label} is translated, not leaked",
              isinstance(raised, _http.TransportError), f"{type(raised).__name__}")
        check(g, f"{label} falls through to later transports",
              len(set(seen)) > 1, f"tried {seen}")

    check(g, "a stale CA bundle names the fix",
          "truststore" in str(ladder(_requests.exceptions.SSLError("x"))[1]))

    # -- a dead network is never "nothing available" ----------------------
    # Exit 1 is the only code that means "keep waiting". A fan-out where every
    # request failed must not return it, or a polling caller waits forever on
    # an outage while reporting that there are no cars.
    # A refused age must not discard the ages that succeeded: "your
    # 19-year-old cannot rent, but you can, for $436" is the useful answer,
    # and it was being thrown away by an uncaught raise mid-loop.
    mixed, mixed_code = _multi_age_quote()
    check(g, "a refused age keeps the ages that worked", mixed_code == 0,
          f"exit {mixed_code}")
    check(g, "the successful age is still rendered", "renter age 25" in mixed)
    check(g, "the refused age is reported, not hidden", "refused" in mixed)
    check(g, "every age refused is still a hard refusal",
          _multi_age_quote(ages=["19"])[1] == 2)

    check(g, "total fan-out failure exits 3, not 1",
          _fanout_code(TransportError("connection reset")) == 3)
    # A refusal can never succeed on any date, so a sweep where every window
    # was refused must not return 1 either - that would poll forever.
    check(g, "fan-out where every window is refused exits 2, not 1",
          _fanout_code(UsageError("age refused")) == 2)


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


def network() -> None:
    g = "network"
    from enterprise.http import Transport
    from enterprise.locations import LocationClient
    from enterprise.rentals import RentalClient
    from datetime import datetime, timedelta

    transport = Transport()
    locations = LocationClient(transport, use_cache=False)

    host = "prd.location.enterprise.com"
    try:
        found = locations.search("Halifax")
        ok = any(c.airport_code == "YHZ" for c in found)
        check(g, f"{host} location search", ok, f"{len(found)} results")
    except Exception as exc:
        check(g, f"{host} location search", False, f"{type(exc).__name__}: {exc}")
        return

    check(g, "Exotic branches are flagged, not hidden",
          any(c.is_exotic for c in found))

    host = "prd-east.webapi.enterprise.ca"
    branch = next((c for c in found if c.airport_code == "YHZ" and not c.is_exotic), None)
    if branch is None:
        check(g, f"{host} live quote", False, "no YHZ branch to quote")
        return

    start = datetime.now() + timedelta(days=35)
    request = QuoteRequest(
        pickup=branch, dropoff=branch,
        pickup_time=start.strftime("%Y-%m-%dT10:00"),
        return_time=(start + timedelta(days=3)).strftime("%Y-%m-%dT10:00"),
    )
    try:
        quote = RentalClient(transport).quote(request)
    except Exception as exc:
        check(g, f"{host} live quote", False, f"{type(exc).__name__}: {exc}")
        return

    check(g, f"{host} live quote", bool(quote.vehicles),
          f"{len(quote.bookable)} of {len(quote.vehicles)} bookable")
    check(g, "live quote carries real prices",
          all(v.total.charged.amount > 0 for v in quote.bookable))
    check(g, "live quote names a currency",
          all(v.total.charged.currency for v in quote.bookable))
    check(g, f"transport in use: {transport.transport_used}", True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip live calls")
    args = parser.parse_args()

    if not FIXTURE.exists():
        print(f"missing fixture: {FIXTURE}")
        return 1

    print("enterprise self-check\n")
    offline()
    if not args.offline:
        print()
        network()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    for failure in _FAIL:
        print(f"  FAILED: {failure}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
