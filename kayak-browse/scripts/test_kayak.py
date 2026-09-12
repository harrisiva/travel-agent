#!/usr/bin/env python3
"""Self-check for kayak-browse.

    python3 test_kayak.py --offline    # logic only, no sockets
    python3 test_kayak.py              # adds live calls, if a key is present

Two labelled groups, as this repo's other skills do:

* ``[offline]`` — pure logic against recorded fixtures. No network, no key.
* ``[network]`` — live calls. These SKIP, rather than fail, when
  ``KAYAK_API_KEY`` is unset, because no key exists for this API yet. A real
  failure names the host, so "the tenant is down" stays distinguishable from
  "the skill is broken".

WHAT THIS SUITE CANNOT PROVE
----------------------------
Every fixture here is **synthetic** — built from the RAML specification, not
captured from a live API. That is a real weakness and it is worth stating
plainly: synthetic fixtures can only prove the parser copes with shapes we
already imagined. The bug worth catching is the one nobody imagined, which is
exactly what a recorded response would contain and this suite cannot.

So the degradations that matter are planted deliberately in
``cars_sparse.json`` — a missing price, a missing passenger count, an absent
policy, an agency code that is not in the agencies map, unknown enum values,
and a ``doors23``. Re-capture every fixture against a production key before
trusting the suite, and treat that as the first task once access exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kayak import filters as filter_mod                       # noqa: E402
from kayak.cache import Cache                                  # noqa: E402
from kayak.client import Client, STATUS_COMPLETE, STATUS_SECOND  # noqa: E402
from kayak.errors import AuthError, SearchTimeout, TransportError, UsageError  # noqa: E402
from kayak.format import (                                      # noqa: E402
    SANDBOX_ROW_PREFIX, envelope, offer_row, search_meta,
)
from kayak.http import Response                                # noqa: E402
from kayak.model import (                                       # noqa: E402
    CalendarSearch, Car, CarSearch, DOORS_RANGES, HotelSearch,
    Money, Offer, Place,
)

FIXTURES = HERE / "fixtures"
SENTINEL_KEY = "sk-SENTINEL-8f3a91b7c2d4e6f0-DO-NOT-LEAK"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str, str]] = []


def check(group: str, name: str, fn) -> None:
    """Run one check, recording pass/fail/skip rather than raising."""
    try:
        outcome = fn()
        if outcome is SKIP:
            _results.append((group, name, SKIP, "no KAYAK_API_KEY set"))
        else:
            _results.append((group, name, PASS, ""))
    except AssertionError as exc:
        _results.append((group, name, FAIL, str(exc) or "assertion failed"))
    except Exception as exc:                                    # noqa: BLE001
        _results.append((group, name, FAIL, f"{type(exc).__name__}: {exc}"))


def fixture(name: str) -> dict:
    """Load by path relative to this file — never the working directory.

    The launcher check runs from a foreign cwd, and a fixture resolved
    relatively would fail there for entirely the wrong reason.
    """
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeTransport:
    """Stands in for http.Transport. Replays queued responses; no sockets.

    Records every request so tests can assert on what was actually sent —
    which is how `pageSize: 500` and `priceMode: "total"` are verified.
    """

    def __init__(self, responses):
        # `responses` is either a queue drained in order, or a callable used
        # for every request. The callable form exists because a sweep runs its
        # searches on four threads: a queue cannot express "this answer belongs
        # to that date" when the draw order is nondeterministic.
        self.responder = responses if callable(responses) else None
        self.responses = [] if self.responder else list(responses)
        self.sent: list[dict] = []
        self.requests_made = 0

    def request(self, method, path, params=None, body=None, headers=None) -> Response:
        self.requests_made += 1
        self.sent.append({"method": method, "path": path,
                          "params": params or {}, "body": body})
        if self.responder is not None:
            return self.responder(method, path, params or {}, body)
        if not self.responses:
            raise AssertionError("FakeTransport ran out of queued responses")
        return self.responses.pop(0)


def ok(payload: dict) -> Response:
    return Response(status=200, body=payload, text="", headers={})


def err(status: int, payload: dict) -> Response:
    return Response(status=status, body=payload, text="", headers={})


class VirtualClock:
    """A clock a fake sleep advances, so waiting is instant but still counted.

    Without this the poll deadline and the second-phase settle rule — both
    defined in seconds — could never fire under a no-op sleep, and the loop
    would spin until it ran out of canned responses.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def client_for(responses: list[Response], **kwargs) -> Client:
    """A Client wired to a fake transport, a disabled cache and virtual time."""
    clock = VirtualClock()
    client = Client(api_key=SENTINEL_KEY, cache=Cache(enabled=False),
                    transport=FakeTransport(responses), sleep=clock.sleep,
                    clock=clock, **kwargs)
    client.clock = clock
    return client


D1, D2 = date(2026, 12, 20), date(2026, 12, 23)


# ----------------------------------------------------------------- offline
# 1. The doors trap.


def t_doors_ranges():
    expected = {"doors1": (1, 1), "doors2": (2, 2), "doors23": (2, 3),
                "doors24": (2, 4), "doors3": (3, 3), "doors4": (4, 4),
                "doors45": (4, 5), "doors5": (5, 5), "doors6": (6, 6)}
    assert DOORS_RANGES == expected, "the door range table drifted"
    for code, (low, high) in expected.items():
        car = Car(doors=code)
        assert car.doors_min == low and car.doors_max == high, code

    # The whole point: arithmetic decoding returns 23 and does not raise.
    naive = int("doors23".removeprefix("doors"))
    assert naive == 23, "premise check"
    assert Car(doors="doors23").doors_min == 2, (
        "doors23 decoded as 23 doors — a --min-doors filter would keep a "
        "two-door car and call it a match")

    unknown = Car(doors="doors9")
    assert unknown.doors_min is None and unknown.doors_max is None
    assert unknown.doors_display == "doors9", "unknown code should pass through"
    assert Car(doors="").doors_min is None


def t_min_doors_filter():
    """A doors23 car must not satisfy --min-doors 4."""
    search = CarSearch.parse(fixture("cars_sparse"))
    two_three = [o for o in search.offers if o.car.doors == "doors23"]
    assert two_three, "fixture no longer contains a doors23 car"
    predicate = filter_mod.min_doors(4)
    assert not any(predicate(o) for o in two_three)
    assert not predicate(Offer(result_id="x", car=Car(doors="doors9")))


# 2. None-safe sorting.


def t_none_safe_sort():
    priced = Money(amount=126.0, currency="CAD", mode="total", days=3)
    cheap = Money(amount=98.0, currency="CAD", mode="total", days=3)
    offers = [Offer(result_id="a", price=Money()),
              Offer(result_id="b", price=priced),
              Offer(result_id="c", price=Money()),
              Offer(result_id="d", price=cheap)]
    ordered = sorted(offers, key=Offer.sort_key)      # must not raise
    assert [o.result_id for o in ordered] == ["d", "b", "a", "c"], \
        "priceless offers must sort last"

    search = CarSearch.parse(fixture("cars_sparse"))
    best = search.cheapest()
    assert best is not None and best.price.amount is not None, \
        "an offer with no price was ranked as cheapest"


# 3. Denormalisation degrades.


def t_join_degrades():
    search = CarSearch.parse(fixture("cars_sparse"))
    ghosts = [o for o in search.offers if o.agency_code == "ghost_agency"]
    assert ghosts, "fixture no longer contains an unmapped agency code"
    ghost = ghosts[0]
    assert ghost.agency_type == "", "an unmapped agency must have no type"
    assert "ghost_agency" in ghost.agency_label, "the code should still show"

    # Absence of evidence is not evidence of opacity.
    assert filter_mod.exclude_agency_type("opaque")(ghost), \
        "an unmapped agency was dropped as if it were opaque"
    assert filter_mod.exclude_agency_type("p2p")(ghost)


def t_join_resolves():
    search = CarSearch.parse(fixture("cars_complete"))
    by_agency = {o.agency_code: o for o in search.offers}
    assert by_agency["hertz"].agency_name == "Hertz"
    assert by_agency["mystery"].agency_type == "opaque"
    assert by_agency["turo"].agency_type == "p2p"
    assert by_agency["hertz"].provider_name in ("PricelineCar", "KAYAK Direct")
    assert by_agency["hertz"].pickup is not None
    assert by_agency["hertz"].pickup.airport_code == "YYZ"


# 4. Every predicate, parameterised over the registry itself.


def t_registry_survives_sparse():
    """No filter may crash on a degraded row, whatever it is missing."""
    offers = CarSearch.parse(fixture("cars_sparse")).offers
    sample = {
        "--type": (["suv"],), "--min-passengers": (5,), "--min-bags": (2,),
        "--min-doors": (4,), "--transmission": ("automatic",),
        "--fuel": (["petrol"],), "--cancel-window": (24.0,),
        "--agency": (["hertz"],), "--max-price": (200.0,),
    }
    for flag, (factory, takes_arg) in filter_mod.REGISTRY.items():
        predicate = factory(*sample[flag]) if takes_arg else factory()
        for offer in offers:
            result = predicate(offer)
            assert isinstance(result, bool), f"{flag} returned {result!r}"


def t_missing_field_excludes():
    offers = CarSearch.parse(fixture("cars_sparse")).offers
    no_seats = [o for o in offers if o.car.passengers is None]
    assert no_seats, "fixture no longer contains a car with no passenger count"
    assert not filter_mod.min_passengers(5)(no_seats[0]), \
        "a row with no passenger count must not satisfy --min-passengers"

    no_price = [o for o in offers if o.price.amount is None]
    assert no_price and not filter_mod.max_price(500.0)(no_price[0]), \
        "an unknown price must not be assumed within budget"

    # credit-card unknown is not the same as "not required"
    unknown_card = Offer(result_id="x", credit_card_required=None)
    assert not filter_mod.no_credit_card()(unknown_card)


# 5. Price mode is inseparable from the number.


def t_money_labels():
    per_day = Money(amount=42.0, currency="CAD", mode="perDayTotal", days=3)
    total = Money(amount=126.0, currency="CAD", mode="total", days=3)
    assert "/day" in per_day.label(), "a per-day price lost its unit"
    assert "/day" not in total.label()
    assert per_day.total() == 126.0, "per-day should multiply out by days"
    assert total.total() == 126.0
    assert Money(amount=42.0, mode="perDayTotal", days=None).total() is None
    assert Money().label() == "no price"


def t_request_carries_price_mode_and_page_size():
    """The "test validated the typo" failure, pinned so it cannot come back.

    This check used to read ``body["searchResultsParameters"]`` — the name the
    CLI happened to send, not the one `SearchRequest` declares in
    cars-search-api-iris_affiliate_cars_v1.raml, which is **`resultParameters`**
    (the *type* is called `SearchResultsParameters`; the property is not).
    Because the assertion was written from the code instead of the spec, the
    test and the bug agreed with each other and both passed, while the live API
    would have ignored the whole object and quietly served KAYAK's defaults:
    `perDayTotal` prices and a 50-row page.

    So this asserts the spelling from the RAML *and* asserts the old one is
    absent. Do not relax the second assertion — without it a client that sends
    both names, or drifts back to the old one, passes again.
    """
    client = client_for([ok(fixture("cars_complete"))])
    client.search_cars("YYZ", D1, D2)
    body = client.transport.sent[0]["body"]
    assert "searchResultsParameters" not in body, (
        "the request still carries `searchResultsParameters`; the RAML's "
        "SearchRequest declares `resultParameters`, and the API ignores "
        "anything else — silently")
    params = body["resultParameters"]
    assert params["priceMode"] == "total", \
        "the CLI must override KAYAK's perDayTotal default"
    assert params["pageSize"] == 500, (
        "pageSize must be 500 — filtering client-side out of a price-sorted "
        "50 returns a confident false 'none available'")
    # The rest of the start body, also checked against the RAML rather than
    # against the code: SearchRequest.searchStartParameters -> pickup/dropoff,
    # each a SearchStartLocationParameters with a `location` {type, value}.
    start = body["searchStartParameters"]
    assert set(start) == {"pickup", "dropoff"}, \
        f"SearchStartParameters declares pickup and dropoff only, got {sorted(start)}"
    assert start["pickup"]["location"] == {"type": "airport", "value": "YYZ"}
    assert start["pickup"]["date"] == D1.isoformat()
    assert start["dropoff"]["date"] == D2.isoformat()
    assert "location" not in start["dropoff"], \
        "a round trip must not pin a dropoff location the user did not give"


def t_price_mode_mismatch_raises():
    """A response priced differently than requested must refuse, not mislabel."""
    client = client_for([ok(fixture("cars_per_day"))])
    try:
        client.search_cars("YYZ", D1, D2)      # requests total, fixture is per-day
    except TransportError as exc:
        assert "priceMode" in str(exc)
        return
    raise AssertionError("a priceMode mismatch was silently accepted")


# 6. The exit-code contract.


def run_cli(argv: list[str], key: str | None = SENTINEL_KEY,
            responses: list[Response] | None = None,
            cache: Cache | None = None,
            transports: list | None = None) -> tuple[int, str, str]:
    """Invoke cli.main with a patched transport, capturing stdout/stderr.

    `cache` lets a test hand the same cache to two invocations, which is how
    the "warm cache must not silence `check`" check is written. `transports`
    collects each FakeTransport built, so a test can assert on what actually
    went out rather than only on what came back.
    """
    import io
    import contextlib
    from kayak import cli as cli_mod
    from kayak import client as client_mod

    real_client = client_mod.Client

    def fake_client(*args, **kwargs):
        clock = VirtualClock()
        transport = FakeTransport(
            responses if callable(responses) else list(responses or []))
        if transports is not None:
            transports.append(transport)
        kwargs["transport"] = transport
        kwargs["sleep"] = clock.sleep
        kwargs["clock"] = clock
        kwargs["cache"] = cache if cache is not None else Cache(enabled=False)
        kwargs.setdefault("api_key", key or SENTINEL_KEY)
        kwargs.pop("host", None)
        return real_client(*args[1:] if args else (), host="sandbox-en-us.kayakaffiliates.com",
                           **{k: v for k, v in kwargs.items() if k != "host"})

    out, errbuf = io.StringIO(), io.StringIO()
    original_env = os.environ.get("KAYAK_API_KEY")
    os.environ["KAYAK_API_KEY"] = key or ""
    client_mod.Client = fake_client
    cli_mod.Client = fake_client
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
            code = cli_mod.main(argv)
    finally:
        client_mod.Client = real_client
        cli_mod.Client = real_client
        if original_env is None:
            os.environ.pop("KAYAK_API_KEY", None)
        else:
            os.environ["KAYAK_API_KEY"] = original_env
    return code, out.getvalue(), errbuf.getvalue()


CARS_ARGS = ["cars", "--pickup", "YYZ", "--from", "2026-12-20",
             "--to", "2026-12-23", "--sandbox-ok"]


def t_exit_empty_is_one():
    code, _, _ = run_cli(CARS_ARGS, responses=[ok(fixture("cars_empty"))])
    assert code == 1, f"a completed empty search must exit 1, got {code}"


def t_exit_auth_is_four():
    code, _, stderr = run_cli(CARS_ARGS,
                              responses=[err(401, fixture("error_401"))])
    assert code == 4, f"a rejected key must exit 4, got {code}"
    assert "not 'no cars available'" in stderr.lower() or "NOT 'no cars" in stderr


def t_exit_partial_with_offers_is_five():
    """A partial search that found offers is 5, not 0 — cheapest is unproven."""
    # One response only: the deadline is hit before any poll can be issued.
    responses = [ok(fixture("cars_first_phase"))] * 30
    code, _, _ = run_cli(CARS_ARGS + ["--max-poll-seconds", "0.5"],
                         responses=responses)
    assert code == 5, f"partial-with-offers must exit 5, got {code}"


def t_exit_partial_empty_is_five_not_one():
    """The polling-safety property, in one assertion.

    A partial search that found nothing must NOT claim 1: code 1 asserts the
    search finished and the answer is genuinely no.
    """
    responses = [ok(fixture("cars_second_phase_empty"))] * 30
    code, _, _ = run_cli(CARS_ARGS + ["--max-poll-seconds", "0.5"],
                         responses=responses)
    assert code == 5, (
        f"partial-and-empty exited {code}; only a COMPLETED empty search may "
        f"exit 1, or a polling agent will stop waiting too early")


def t_exit_found_is_zero():
    code, _, _ = run_cli(CARS_ARGS, responses=[ok(fixture("cars_complete"))])
    assert code == 0, f"a completed search with matches must exit 0, got {code}"


def t_until_second_phase_is_zero():
    """Reaching the phase the caller asked for is success, not a timeout."""
    code, _, _ = run_cli(CARS_ARGS + ["--until", "second-phase"],
                         responses=[ok(fixture("cars_second_phase_with_offers"))])
    assert code == 0, f"--until second-phase reaching it must exit 0, got {code}"


# 7. Envelope shape.


def t_envelope_is_uniform():
    payloads = {}
    for argv, responses in (
        (CARS_ARGS + ["--json"], [ok(fixture("cars_complete"))]),
        (["places", "Toronto", "--json"], [ok(fixture("autocomplete_cars"))]),
        (["check", "--json"], [ok(fixture("autocomplete_cars"))]),
    ):
        code, out, _ = run_cli(argv, responses=responses)
        payloads[argv[0]] = json.loads(out)

    for command, data in payloads.items():
        assert isinstance(data["results"], list), \
            f"{command}: results must be a list, not {type(data['results'])}"
        assert data["count"] == len(data["results"]), f"{command}: count mismatch"
        for key in ("schema_version", "ok", "command", "exit_code",
                    "error", "query", "meta"):
            assert key in data, f"{command}: envelope missing {key}"
    assert len(payloads["check"]["results"]) == 1, \
        "check must return a one-element array, not a bare object"


def t_envelope_on_failure():
    code, out, _ = run_cli(CARS_ARGS + ["--json"],
                           responses=[err(401, fixture("error_401"))])
    data = json.loads(out)
    assert data["ok"] is False and data["exit_code"] == 4 == code
    assert data["error"] and isinstance(data["results"], list)


def t_json_does_not_change_exit_code():
    plain, _, _ = run_cli(CARS_ARGS, responses=[ok(fixture("cars_empty"))])
    as_json, _, _ = run_cli(CARS_ARGS + ["--json"],
                            responses=[ok(fixture("cars_empty"))])
    assert plain == as_json == 1, "--json changed the exit code"


# 8. Rejection accounting.


def t_filtered_out_partitions():
    offers = CarSearch.parse(fixture("cars_complete")).offers

    class Args:
        type = ["suv", "van"]
        min_passengers = 7
        unlimited_mileage = True
        min_bags = min_doors = transmission = fuel = None
        cancel_window = agency = max_price = None
        free_cancellation = no_credit_card = False
        exclude_opaque = exclude_p2p = sleepable = False

    built = filter_mod.build(Args())
    outcome = filter_mod.apply(offers, built)
    assert len(outcome.kept) + sum(outcome.rejected.values()) == outcome.parsed, \
        "filters dropped rows without reporting them"
    assert outcome.parsed == len(offers)
    assert any("--min-passengers 7" in flag for flag in outcome.rejected), \
        "the rejection report must name flags as the user typed them"


def t_sleepable_sugar():
    offers = CarSearch.parse(fixture("cars_complete")).offers
    kept = [o for o in offers if filter_mod.sleepable()(o)]
    assert kept, "no sleepable vehicle in a fixture containing an SUV and a van"
    assert all(set(o.car.groups) & {"suv", "van"} for o in kept)
    assert all(o.car.passengers >= 4 for o in kept)


def t_limit_trims_json_not_just_the_table():
    """--limit must shrink the JSON array too — that is the context-window lever.

    A limit that only shortened the human table would leave the thing it
    exists to solve (a 500-row response landing in an agent's context)
    entirely unsolved.
    """
    code, out, _ = run_cli(CARS_ARGS + ["--limit", "3", "--json"],
                           responses=[ok(fixture("cars_complete"))])
    data = json.loads(out)
    assert len(data["results"]) == 3, \
        f"--limit 3 returned {len(data['results'])} JSON rows"
    assert data["count"] == 3, "count must match the trimmed array"
    assert data["meta"]["kept"] == 9, \
        "meta.kept must still report how many actually matched"
    assert data["meta"]["truncated"] is True


def t_full_returns_raw_rows():
    """--full must actually differ from the projection, and keep the envelope."""
    plain, full = {}, {}
    for flags, into in ((["--json"], plain), (["--json", "--full"], full)):
        _, out, _ = run_cli(CARS_ARGS + flags,
                            responses=[ok(fixture("cars_complete"))])
        into.update(json.loads(out))

    assert isinstance(full["results"], list), "--full must keep results a list"
    assert full["count"] == len(full["results"])
    assert full["results"] != plain["results"], \
        "--full returned the trimmed projection — the flag does nothing"
    assert "bookingOptions" in full["results"][0], \
        "--full rows should be the API's own results[] entries"
    for key in ("agencies", "providers", "carLocations"):
        assert key in full["meta"]["maps"], f"--full must expose the {key} map"


def t_key_file_outranks_ambient_env():
    """An explicitly passed --key-file must beat a stale environment variable.

    Both are deliberate, but the flag was typed for THIS run while the export
    may be left over from another shell. Preferring the ambient value silently
    authenticates the user as somebody else.
    """
    import tempfile
    from kayak.auth import resolve_key

    path = Path(tempfile.mkdtemp()) / "key.txt"
    path.write_text("file-key-EXPLICIT\n")
    previous = os.environ.get("KAYAK_API_KEY")
    os.environ["KAYAK_API_KEY"] = "env-key-STALE"
    try:
        assert resolve_key(None, str(path), Cache(enabled=False)) == "file-key-EXPLICIT"
        assert resolve_key(None, None, Cache(enabled=False)) == "env-key-STALE"
        assert resolve_key("flag", str(path), Cache(enabled=False)) == "flag"
    finally:
        if previous is None:
            os.environ.pop("KAYAK_API_KEY", None)
        else:
            os.environ["KAYAK_API_KEY"] = previous


def t_sweep_ranks_days_by_price():
    """`sweep` must return the cheapest days, not the first days.

    The command exists to answer "which pickup day is cheapest", so appending
    in calendar order and trimming to --limit would hide a cheaper day later
    in the range — the same defect that let --limit hide the cheapest car.
    """
    dear = fixture("cars_complete")
    cheap = json.loads(json.dumps(dear))
    for result in cheap["results"]:
        for option in result.get("bookingOptions", []):
            if isinstance(option.get("price"), dict):
                option["price"]["price"] = 5.0
                option["price"]["displayPrice"] = "$5"

    CHEAP_DAY = "2026-12-22"          # the LAST day in the swept range

    def responder(method, path, params, body):
        """Price one specific pickup date cheaply, whatever order threads run."""
        start = (body or {}).get("searchStartParameters") or {}
        date_asked = ((start.get("pickup") or {}).get("date")
                      if isinstance(start.get("pickup"), dict) else None)
        return ok(cheap if date_asked == CHEAP_DAY else dear)

    code, out, _ = run_cli(
        ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", CHEAP_DAY,
         "--nights", "3", "--sandbox-ok", "--limit", "1", "--json"],
        responses=responder)
    data = json.loads(out)
    assert data["meta"]["ranked_by"] == "price", "sweep must say how it ordered days"
    assert data["results"], "expected at least one day"
    top = data["results"][0]
    assert top["date"] == CHEAP_DAY, (
        f"--limit 1 returned {top['date']} but {CHEAP_DAY} was cheaper; "
        f"sweep trimmed in calendar order instead of ranking by price")
    assert top["cheapest"]["price"]["total"] <= 20


# 9. No key material in output.


def t_key_never_leaks():
    code, out, errtext = run_cli(["check", "--json"],
                                 responses=[ok(fixture("autocomplete_cars"))])
    blob = out + errtext
    assert SENTINEL_KEY not in blob, "the raw API key was printed"
    # No meaningful run of the key may appear either.
    for size in (8, 6, 4):
        for i in range(len(SENTINEL_KEY) - size + 1):
            chunk = SENTINEL_KEY[i:i + size]
            if chunk.isalnum() and len(set(chunk)) > 2:
                assert chunk not in blob, f"key fragment {chunk!r} leaked"

    data = json.loads(out)
    fingerprint = data["results"][0]["key_fingerprint"]
    import hashlib
    assert fingerprint == hashlib.sha256(SENTINEL_KEY.encode()).hexdigest()[:8], \
        "the fingerprint must be derived, never a slice of the key"


# 10. Launcher from a foreign working directory.


def t_launcher_absolute_path():
    launcher = HERE / "kayak.py"
    env = dict(os.environ)
    env.pop("KAYAK_API_KEY", None)
    env["KAYAK_CACHE_DIR"] = "off"
    proc = subprocess.run([sys.executable, str(launcher), "check"],
                          cwd=tempfile.gettempdir(), env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 4, (
        f"a keyless check from a foreign cwd should exit 4, got "
        f"{proc.returncode}: {proc.stderr.strip()[:200]}")
    assert "key" in proc.stderr.lower()

    helped = subprocess.run([sys.executable, str(launcher), "--help"],
                            cwd=tempfile.gettempdir(), env=env,
                            capture_output=True, text=True, timeout=60)
    assert helped.returncode == 0 and "cars" in helped.stdout


# 11. Sandbox honesty.


def t_sandbox_suppresses_price_column():
    code, out, _ = run_cli(["cars", "--pickup", "YYZ", "--from", "2026-12-20",
                            "--to", "2026-12-23"],
                           responses=[ok(fixture("cars_complete"))])
    assert code == 0
    assert "SANDBOX" in out, "the sandbox banner is missing"
    assert "$126" not in out and "$98" not in out, \
        "a mock price was printed without --sandbox-ok"


def t_sandbox_ok_marks_every_row():
    code, out, _ = run_cli(CARS_ARGS + ["--json"],
                           responses=[ok(fixture("cars_complete"))])
    data = json.loads(out)
    assert data["meta"]["sandbox"] is True
    assert data["meta"]["prices_are_mocked"] is True
    assert data["results"], "expected offers"
    assert all(row["priceIsReal"] is False for row in data["results"]), \
        "every sandbox row must carry priceIsReal: false"


def t_sweep_refuses_in_sandbox():
    code, _, stderr = run_cli(["sweep", "--pickup", "YYZ", "--from", "2026-12-20",
                               "--to", "2026-12-22", "--nights", "3"],
                              responses=[ok(fixture("cars_complete"))] * 30)
    assert code == 2, f"a sandbox sweep ranks fiction; must exit 2, got {code}"
    assert "mock data" in stderr


def t_sweep_all_days_network_failed_exits_three():
    """B13: every day network-failing is not "nothing available".

    A completed empty search legitimately exits 1 (`t_exit_empty_is_one`).
    A sweep where every day's search raised a transport error never actually
    looked, so it must not read the same way — a polling agent watching a
    sold-out campground must not "wait" through an outage forever.
    """
    def responder(method, path, params, body):
        return err(500, {})

    code, _, stderr = run_cli(
        ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-21",
         "--nights", "3", "--sandbox-ok"],
        responses=responder)
    assert code == 3, f"every day network-failing must exit 3, got {code}"
    # `_fail` always prefixes "error: ", so asserting on that alone is
    # vacuous — only "network" actually pins the message content.
    assert "network" in stderr.lower()


def t_sweep_mixed_failure_says_which_days_were_not_checked():
    """Round-2 fix (#1): a mixed sweep must not silently report "no priced
    offers on any day" as if the whole range were confirmed empty.

    One day network-fails (500), one day genuinely completes empty. Exit 1
    is defensible here (something really was checked and found empty) but
    only if the output says out loud that the other day was never checked —
    otherwise an agent reading "no priced offers on any day in that range"
    reasonably concludes the whole range was searched.
    """
    FAILED_DAY = "2026-12-20"

    def responder(method, path, params, body):
        start = (body or {}).get("searchStartParameters") or {}
        date_asked = ((start.get("pickup") or {}).get("date")
                      if isinstance(start.get("pickup"), dict) else None)
        if date_asked == FAILED_DAY:
            return err(500, {})
        return ok(fixture("cars_empty"))

    args = ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-21",
            "--nights", "3", "--sandbox-ok"]

    code, out, _ = run_cli(args + ["--json"], responses=responder)
    data = json.loads(out)
    assert code == 1, f"one day empty + one day network-down must still exit 1, got {code}"
    assert data["meta"]["days_failed"] == 1, (
        f"meta.days_failed must count the network-failed day, got "
        f"{data['meta'].get('days_failed')}")

    _, plain, _ = run_cli(args, responses=responder)
    assert "1 of 2 days" in plain and "not checked" in plain, (
        "a mixed sweep must say out loud that some days were never checked, "
        "or exit 1 silently asserts the whole range is confirmed empty")


def t_sweep_all_timeouts_with_no_partial_exits_five_not_one():
    """Round-2 fix (#3): SearchTimeout's own `partial` defaults to None, and
    a day recorded that way must fall into the same "not checked" bucket as
    a network failure — not be counted toward a confirmed-empty sweep.

    Today's two SearchTimeout call sites always populate `partial`, so this
    exercises the defensive branch directly via a patched `sweep_cars`
    rather than via a fixture that cannot occur through the real client.
    A pure timeout (no network failure in the mix) exits 5, matching the
    single-search SearchTimeout contract (`cars` itself: partial -> exit 5) —
    not 3, which is reserved for when at least one day actually network-failed.
    """
    from kayak import client as client_mod

    def fake_sweep_cars(self, *a, **k):
        return [(date(2026, 12, 20), None,
                 SearchTimeout("timed out before returning anything", partial=None))]

    original = client_mod.Client.sweep_cars
    client_mod.Client.sweep_cars = fake_sweep_cars
    try:
        code, _, stderr = run_cli(
            ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-20",
             "--nights", "3", "--sandbox-ok"],
            responses=[])
    finally:
        client_mod.Client.sweep_cars = original
    assert code == 5, (
        f"an all-timeout-with-no-partial sweep must exit 5 (not checked, not "
        f"confirmed empty), got {code}")
    assert "timed out" in stderr.lower()


def t_sweep_usage_errors_are_not_network_failures():
    """A per-day UsageError (a bug in our own request, not an outage or a
    timeout) must not be counted as "not checked" — only TransportError and
    SearchTimeout earn that classification. An all-UsageError sweep still
    exits 1, distinguishing "our own bug" from "the tenant is down".

    Kills a mutant widening the `isinstance(error, (TransportError,
    SearchTimeout))` check to `isinstance(error, Exception)`.
    """
    from kayak import client as client_mod

    def fake_sweep_cars(self, *a, **k):
        return [(date(2026, 12, 20), None, UsageError("malformed request"))]

    original = client_mod.Client.sweep_cars
    client_mod.Client.sweep_cars = fake_sweep_cars
    try:
        code, _, _ = run_cli(
            ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-20",
             "--nights", "3", "--sandbox-ok"],
            responses=[])
    finally:
        client_mod.Client.sweep_cars = original
    assert code == 1, (
        f"a per-day UsageError must not be classified as 'not checked' "
        f"(network/timeout); expected the plain empty-sweep exit 1, got {code}")


def t_sweep_confirm_stage_failure_keeps_partial_not_unchecked():
    """A day with real stage-1 (second-phase) offers must not be reclassified
    as "not checked" just because the stage-2 confirm-to-complete re-poll
    failed — `sweep_cars` deliberately keeps the earlier partial in that case
    (client.py: `keep, _ = results[day]; results[day] = (keep, exc)`), and
    that day's `search` is therefore NOT None even though its `error` is a
    TransportError. Kills a mutant dropping the `search is None` half of the
    "not checked" test.
    """
    calls = {"n": 0}

    def responder(method, path, params, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return ok(fixture("cars_second_phase_with_offers"))
        return err(500, {})

    code, out, _ = run_cli(
        ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-20",
         "--nights", "3", "--sandbox-ok", "--json"],
        responses=responder)
    data = json.loads(out)
    assert code == 0, (
        f"a day with real stage-1 offers must not be treated as 'not "
        f"checked' just because the stage-2 confirm poll failed, got {code}")
    assert data["meta"]["days_failed"] == 0, (
        "a day with usable partial data from stage 1 must not count toward "
        "days_failed just because the confirm re-poll failed")


def t_sweep_genuinely_empty_still_exits_one():
    """The other half of B13: a real "nothing found" sweep must stay exit 1.

    Guards against overcorrecting B13 into treating every empty sweep as a
    network failure.
    """
    code, _, _ = run_cli(
        ["sweep", "--pickup", "YYZ", "--from", "2026-12-20", "--to", "2026-12-21",
         "--nights", "3", "--sandbox-ok"],
        responses=[ok(fixture("cars_empty"))] * 30)
    assert code == 1, f"a genuinely empty sweep must still exit 1, got {code}"


# 12. Poll-loop behaviour.


def t_poll_reaches_complete():
    responses = [ok(fixture("cars_first_phase")),
                 ok(fixture("cars_second_phase_with_offers")),
                 ok(fixture("cars_complete"))]
    client = client_for(responses)
    search = client.search_cars("YYZ", D1, D2)
    assert search.status == STATUS_COMPLETE
    assert client.transport.requests_made == 3
    poll_bodies = [s["body"] for s in client.transport.sent[1:]]
    assert all("searchId" in b for b in poll_bodies), "polls must send searchId"
    assert all(s["params"].get("cluster") == "c7"
               for s in client.transport.sent[1:]), "polls must pass cluster"


def t_poll_settles_early_in_second_phase():
    """Unchanged totalCount across two polls ends a search that asked for it.

    The rule is scoped to `--until second-phase`. A caller who asked for
    `complete` must keep polling: breaking early there would fail the phase
    check in `search_cars` and turn the happy path into exit 5.
    """
    second = fixture("cars_second_phase_with_offers")
    client = client_for([ok(second)] * 40, max_poll_seconds=600)
    try:
        client.search_cars("YYZ", D1, D2, until=STATUS_SECOND)
    except SearchTimeout:
        pass
    # totalCount never changes in this fixture, so the settle rule should end
    # the loop a few polls in rather than running to the 600s ceiling.
    assert client.transport.requests_made <= 6, (
        f"a settled second-phase search polled "
        f"{client.transport.requests_made} times; the unchanged-totalCount "
        f"rule did not fire")


def t_settle_rule_does_not_shortcut_a_complete_search():
    """`--until complete` polls to complete, or to the deadline. No third answer."""
    responses = ([ok(fixture("cars_second_phase_with_offers"))] * 6
                 + [ok(fixture("cars_complete"))])
    client = client_for(responses, max_poll_seconds=600)
    search = client.search_cars("YYZ", D1, D2)
    assert search.status == STATUS_COMPLETE, (
        "a search asked to reach complete stopped at second-phase; the settle "
        "rule must not apply to a caller who asked for certainty")
    assert client.transport.requests_made == 7


def t_budget_refuses_before_calling():
    client = client_for([], max_poll_seconds=25)
    try:
        client.sweep_cars("YYZ", date(2026, 12, 1), date(2026, 12, 28), 3,
                          max_requests=10)
    except Exception as exc:
        assert "max-requests" in str(exc), f"unexpected refusal: {exc}"
        assert client.transport.requests_made == 0, \
            "the budget refusal must happen before any request goes out"
        return
    raise AssertionError("an unaffordable sweep was not refused")


def t_usage_errors():
    client = client_for([])
    for bad in (lambda: client.search_cars("YYZ", D2, D1),
                lambda: client.places("", "cars"),
                lambda: client.places("Toronto", "spaceships")):
        try:
            bad()
        except UsageError:
            continue
        raise AssertionError("a usage error was not raised")


def t_places_parse():
    places = Place.parse_many(fixture("autocomplete_cars"))
    assert len(places) == 2
    assert places[0].iata == "YYZ"
    assert "Toronto" in places[0].label


# 13. Hotels — request shape, and the completion flag.
#
# Everything below this line was written from the RAML, not from the code:
# hotels-search-api-hapi_affiliate.raml for the two hotel checks, and
# flights-price-insights-api-iris_affiliate_flights_price_insights_v1.raml for
# the calendar ones. Until this section existed the suite had no hotel and no
# calendar coverage at all, and the field names in both requests were invented.


HOTEL_DEST = "kplace:58075"
CHECKIN, CHECKOUT = date(2027, 4, 14), date(2027, 4, 16)


def t_hotels_request_shape():
    """`/hotels` takes `destination`, an EntityKey — not a placeId, not a limit.

    The RAML's query parameters are `destination` (`EntityKey`, e.g.
    `kplace:58075`), `checkin`, `checkout`, `rooms`, `pageSize`. `placeId` and
    `limit` are not parameters of this endpoint at all: an unknown query
    parameter is ignored, so sending them means an unfiltered, unpaged search
    reported as if it had been narrowed.
    """
    client = client_for([ok(fixture("hotels_complete"))])
    client.hotels(HOTEL_DEST, CHECKIN, CHECKOUT)
    params = client.transport.sent[0]["params"]
    assert params.get("destination") == HOTEL_DEST, \
        f"hotels must send destination=<EntityKey>, got {params!r}"
    assert "placeId" not in params, \
        "`placeId` is not a parameter of /hotels — it is silently ignored"
    assert "limit" not in params, \
        "`limit` is not a parameter of /hotels; the RAML calls it pageSize"
    # Bug 1, in the hotels vertical. The server default is 25 rows, and every
    # narrowing this CLI does runs client-side, so 25 rows of a
    # popularity-sorted city is not a sample you can answer "nothing under
    # $200" from. An omitted field inherits the default in silence.
    assert "pageSize" in params, (
        "hotels must send pageSize explicitly; omitted, the server pages at 25 "
        "and a client-side filter over those 25 reports a false 'none found'")
    assert int(params["pageSize"]) == 250, (
        f"pageSize is {params['pageSize']}; the RAML's maximum is 250 and "
        f"there is no reason to ask for less")
    assert params.get("checkin") == CHECKIN.isoformat(), \
        "the RAML spells these `checkin`/`checkout`, all lower case"
    assert params.get("checkout") == CHECKOUT.isoformat()
    assert params.get("rooms"), \
        "`rooms` is required when searching with check-in and check-out dates"


def t_single_hotel_uses_the_hotel_parameter():
    """`/hotel` takes `hotel=<EntityKey>` — a different parameter, same rules.

    It is not the destination search with a filter: it addresses one property
    by its own key, returns exactly one hotel, and declares no `pageSize`.
    `rooms` is required of it whenever dates are supplied, and it has the same
    `onlyIfComplete` / `isComplete` completion semantics as the multi search.
    """
    client = client_for([ok(fixture("hotels_complete"))])
    client.hotels("", CHECKIN, CHECKOUT, hotel_id="khotel:2589314")
    sent = client.transport.sent[0]
    assert sent["path"].endswith("/hotel"), \
        f"a single-hotel lookup must hit /hotel, got {sent['path']}"
    params = sent["params"]
    assert params.get("hotel") == "khotel:2589314", \
        f"/hotel takes `hotel=<EntityKey>`, got {params!r}"
    assert "destination" not in params, \
        "/hotel does not take a destination; sending one is ignored in silence"
    assert "pageSize" not in params, \
        "/hotel returns one hotel and declares no pageSize"
    assert params.get("rooms"), \
        "`rooms` is required by /hotel whenever dates are supplied"


def t_hotels_only_if_complete_is_sent():
    """`onlyIfComplete` is what turns a partial answer into a 202 to retry."""
    client = client_for([ok(fixture("hotels_complete"))])
    client.hotels(HOTEL_DEST, CHECKIN, CHECKOUT, only_if_complete=True)
    params = client.transport.sent[0]["params"]
    assert params.get("onlyIfComplete") in (True, "true"), \
        f"only_if_complete=True must reach the wire, got {params!r}"


def t_hotels_partial_is_not_complete():
    """A half-finished hotel search must never read as finished."""
    complete = HotelSearch.parse(fixture("hotels_complete"))
    assert complete.complete is True and len(complete.hotels) == 4
    assert complete.total_count == 4

    partial = HotelSearch.parse(fixture("hotels_partial"))
    assert partial.complete is False, (
        "isComplete:false parsed as complete — the cheapest hotel in a "
        "half-finished search is not the cheapest hotel")
    assert partial.hotels, "a partial search still has rows worth showing"

    empty = HotelSearch.parse(fixture("hotels_empty"))
    assert empty.hotels == ()
    assert empty.complete is True, \
        "a finished empty search was reported as still running"

    # `isCompleted` appears once, in the spec's prose; every type definition
    # and every example body says `isComplete`. The parser tolerates the prose
    # spelling, and that tolerance is asserted here rather than baked into a
    # fixture on purpose: a fixture states what the server sends, so one
    # spelled `isCompleted` would pass against our own fallback and fail
    # against the real API — bug 2 again, one vertical over.
    assert HotelSearch.parse({"isCompleted": True, "results": []}).complete is True, \
        "the prose spelling `isCompleted` is no longer tolerated"
    assert HotelSearch.parse({"results": []}).complete is False, \
        "a response with no completion flag must never be assumed complete"


def t_hotels_cheapest_ignores_unpriced():
    """`lowestRate: null` is in the RAML type. It must not win on price."""
    search = HotelSearch.parse(fixture("hotels_complete"))
    unpriced = [h for h in search.hotels if h.lowest_rate is None]
    assert unpriced, "fixture no longer contains a hotel with a null lowestRate"
    best = search.cheapest()
    assert best is not None and best.lowest_rate == 188.4, \
        f"cheapest() returned {best!r}"


HOTEL_ARGS = ["hotels", "--destination", HOTEL_DEST,
              "--checkin", "2027-04-14", "--checkout", "2027-04-16"]


def t_hotels_cli_complete_flag_exits_five():
    """--complete asked for certainty and did not get it: that is exit 5."""
    code, _, _ = run_cli(HOTEL_ARGS + ["--complete"],
                         responses=[ok(fixture("hotels_partial"))] * 30)
    assert code == 5, (
        f"--complete over an incomplete search must exit 5, got {code}; "
        f"0 would assert a completeness the response denies")


def t_hotels_cli_partial_is_reported_not_hidden():
    """Without --complete the rows are still worth having — but labelled."""
    code, out, _ = run_cli(HOTEL_ARGS + ["--json"],
                           responses=[ok(fixture("hotels_partial"))])
    data = json.loads(out)
    assert code == 0, f"a partial hotel list with rows is still exit 0, got {code}"
    assert data["meta"]["partial"] is True, \
        "meta.partial must say the list was incomplete, or nothing downstream can"
    assert data["meta"].get("complete") is False
    assert len(data["results"]) == 2


def t_hotels_partial_and_empty_exits_five_not_one():
    """B14: a partial hotel search with zero rows so far must exit 5, not 1.

    The tool's own rule for cars (`t_exit_partial_empty_is_five_not_one`) is
    that a partial search which found nothing is 5, not 1 — 1 asserts the
    search finished and the answer is genuinely no. Hotels must be consistent:
    without --complete, `client.hotels()` never raises SearchTimeout, so this
    has to be caught after the fact from `meta.complete`.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--json"],
                           responses=[ok(fixture("hotels_partial_empty"))])
    assert code == 5, (
        f"a partial hotel search with nothing back yet must exit 5, got "
        f"{code}; only a COMPLETED empty search may exit 1")
    assert json.loads(out)["meta"]["complete_flag_present"] is True, (
        "hotels_partial_empty carries an explicit isComplete:false; the exit-5 "
        "gate must see the flag as present")


def t_hotels_absent_flag_and_empty_exits_one_not_five():
    """Round-2 regression guard: an absent isComplete flag must not read as
    positive evidence of an unfinished search on the plain (no --complete)
    path.

    `HotelSearch.parse` conservatively defaults a missing flag to
    complete=False (model.py's own documented rule), but the client's other
    documented rule — "an absent flag on a 200 is corroboration, not
    counter-evidence, once the HTTP status is the authoritative signal" —
    means the CLI must not escalate an absent flag to exit 5 the way it
    rightly does for an explicit `isComplete: false`
    (`t_hotels_partial_and_empty_exits_five_not_one`). Gated on
    `_has_complete_flag`.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--json"],
                           responses=[ok(fixture("hotels_no_flag_empty"))])
    data = json.loads(out)
    assert code == 1, (
        f"an empty search with no completion flag at all must exit 1, got "
        f"{code}; only an explicit isComplete:false earns exit 5")
    assert data["meta"]["complete_flag_present"] is False


def t_hotels_completed_and_empty_still_exits_one():
    """The other half of B14: a genuinely finished empty search stays exit 1.

    Guards against overcorrecting the B14 fix into treating every empty
    hotel result as partial.
    """
    code, _, _ = run_cli(HOTEL_ARGS, responses=[ok(fixture("hotels_empty"))])
    assert code == 1, f"a completed empty hotel search must exit 1, got {code}"


def t_hotels_limit_zero_does_not_hide_that_hotels_were_found():
    """Defect 7: found-ness must be measured before `--limit` trims the list.

    `--limit 0` on a partial search with real (but untrimmed-away) hotels
    must not read as "nothing has come back yet" — that conflates "the
    caller asked to see zero rows" with "the search found zero hotels".
    `cars` (`bool(outcome.kept)`) and `when` (`bool(days)`) already check
    pre-trim; `hotels` must match, using `meta.matched`.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--limit", "0", "--json"],
                           responses=[ok(fixture("hotels_partial"))])
    data = json.loads(out)
    assert code == 0, (
        f"a partial search that matched hotels must not exit 5 just because "
        f"--limit 0 trimmed the visible list to nothing, got {code}")
    assert data["results"] == []
    assert data["meta"]["matched"] == 2


def t_hotels_deprecated_place_alias_still_works():
    """`--place` was the old name and is still accepted, silently.

    It is hidden from `--help` but must keep parsing: a recipe or a stored
    command written against the old flag should not start failing with a usage
    error, and `--destination` is what it feeds.
    """
    code, out, _ = run_cli(["hotels", "--place", HOTEL_DEST,
                            "--checkin", "2027-04-14",
                            "--checkout", "2027-04-16", "--json"],
                           responses=[ok(fixture("hotels_complete"))])
    data = json.loads(out)
    assert code == 0 and len(data["results"]) == 4


def t_hotels_without_a_destination_is_a_usage_error():
    code, _, stderr = run_cli(["hotels", "--checkin", "2027-04-14",
                               "--checkout", "2027-04-16"], responses=[])
    assert code == 2, f"a hotels search with no destination must exit 2, got {code}"
    assert "places" in stderr, \
        "the error should point at `places --for hotels`, not just refuse"


def t_hotels_limit_keeps_the_cheapest_not_the_first():
    """The `--limit` defect class again, one vertical over.

    `/hotels` returns rows "sorted based on our recommendations" — popularity,
    not price. hotels_complete puts its cheapest property ($188.40) third and a
    rate-less one last. Trim to two before ranking and the answer is the two
    most *recommended* hotels, with the cheapest silently cut, which is how the
    same bug read on cars: a plausible expensive row sitting at the top.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--limit", "2", "--json"],
                           responses=[ok(fixture("hotels_complete"))])
    data = json.loads(out)
    assert code == 0 and len(data["results"]) == 2
    rates = [row["lowest_rate"] for row in data["results"]]
    assert rates == [188.4, 402.5], (
        f"rows are {rates}; the fixture's cheapest is 188.4 and sits third, so "
        f"anything else means --limit trimmed before ranking")
    assert data["meta"]["ranked_by"] == "price", \
        "meta must say what the rows were ranked by; 'cheapest' is a claim"
    assert data["meta"]["truncated"] is True
    assert data["meta"]["matched"] == 4, \
        "meta.matched must still report how many came back"


def t_hotels_sandbox_suppresses_price_column():
    """B15: `hotels` must hide sandbox prices the same way `cars` does.

    Before this fix the "from" column printed the raw mock rate with only a
    banner above the table — no per-row marker, unlike `cars`.
    """
    code, out, _ = run_cli(HOTEL_ARGS, responses=[ok(fixture("hotels_complete"))])
    assert code == 0
    assert "SANDBOX" in out, "the sandbox banner is missing"
    assert "402.5" not in out and "188.4" not in out, \
        "a mock hotel rate was printed without --sandbox-ok"


def t_hotels_sandbox_ok_shows_marked_price():
    """--sandbox-ok must actually reveal the price, marked, not be a no-op.

    Kills two mutants: `show_price = not client.sandbox` (dropping the
    `or args.sandbox_ok` half, making the flag do nothing), and deleting the
    per-row `SANDBOX_ROW_PREFIX` on hotel rates.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--sandbox-ok"],
                           responses=[ok(fixture("hotels_complete"))])
    assert code == 0
    assert SANDBOX_ROW_PREFIX in out, (
        "--sandbox-ok must mark the price it reveals, not print a bare number")
    assert "188.4" in out, "the price should actually be visible with --sandbox-ok"


def t_hotels_sandbox_ok_marks_every_row():
    code, out, _ = run_cli(HOTEL_ARGS + ["--sandbox-ok", "--json"],
                           responses=[ok(fixture("hotels_complete"))])
    data = json.loads(out)
    assert data["results"], "expected hotel rows"
    assert all(row["priceIsReal"] is False for row in data["results"]), \
        "every sandbox hotel row must carry priceIsReal: false"


def t_hotels_without_rates_say_they_are_unranked():
    """With nothing to rank on, the order is the endpoint's, and it says so.

    Claiming `ranked_by: price` over rate-less rows would assert an ordering
    that does not exist — the failure this whole section is about, inverted.
    """
    code, out, _ = run_cli(HOTEL_ARGS + ["--json"],
                           responses=[ok(fixture("hotels_no_rates"))])
    data = json.loads(out)
    assert code == 0
    assert all(row["lowest_rate"] is None for row in data["results"]), \
        "premise check — no hotel in this fixture carries a rate"
    assert data["meta"]["ranked_by"] == "relevance", (
        f"ranked_by is {data['meta']['ranked_by']!r}; with no rates there is "
        f"nothing to rank on and the API's own order is what is being shown")

    # And the human output must not leave that implicit either.
    _, plain, _ = run_cli(HOTEL_ARGS, responses=[ok(fixture("hotels_no_rates"))])
    assert "rank" in plain.lower(), \
        "the table gave no hint that these rows are in the API's order"


# 14. Calendar — request shape, range limits, and the round-trip price.


def t_calendar_request_matches_spec():
    """CalendarRequest: PlaceRequest objects, and `dateFrom`/`dateTo` as YYYY-MM.

    `origin` and `destination` are `PlaceRequest` objects — `{"placeId": int}`
    or `{"iataCode": "JFK"}` — never bare strings, and there is no `month`
    field anywhere in the spec.
    """
    client = client_for([ok(fixture("calendar_days"))])
    client.calendar("JFK", "LIS", "2027-03", "2027-04")
    body = client.transport.sent[0]["body"]
    assert body["origin"] == {"iataCode": "JFK"}, \
        f"origin must be a PlaceRequest object, got {body.get('origin')!r}"
    assert body["destination"] == {"iataCode": "LIS"}
    assert body["dateFrom"] == "2027-03" and body["dateTo"] == "2027-04"
    assert "month" not in body, "`month` is not a field of CalendarRequest"

    # All-digit input is a KAYAK place id, and a place id is an integer.
    client = client_for([ok(fixture("calendar_days"))])
    client.calendar("175312", "175706", "2027-03", "2027-03")
    body = client.transport.sent[0]["body"]
    assert body["origin"] == {"placeId": 175312}, \
        f"a numeric place must go out as placeId, got {body.get('origin')!r}"
    assert body["destination"] == {"placeId": 175706}


def t_calendar_optional_flags_use_spec_names():
    client = client_for([ok(fixture("calendar_days"))])
    client.calendar("JFK", "LIS", "2027-03", "2027-03", aggregation="month",
                    round_trip=True, non_stop=True, currency="CAD",
                    exclude_predictions=True)
    body = client.transport.sent[0]["body"]
    assert body["aggregationType"] == "month", "the field is `aggregationType`"
    assert body["roundTrip"] is True
    assert body["noStops"] is True, "non-stop is `noStops` in CalendarRequest"
    assert body["currencyCode"] == "CAD", "the field is `currencyCode`"
    assert body["excludePredictions"] is True


def t_calendar_rejects_a_bad_month():
    """A month that is not YYYY-MM must be refused before it costs a request."""
    for bad in ("2027-3", "March", "2027-13", "2027-03-01", ""):
        client = client_for([])
        try:
            client.calendar("JFK", "LIS", bad, "2027-04")
        except UsageError:
            assert client.transport.requests_made == 0, \
                "the refusal must happen before the call goes out"
            continue
        raise AssertionError(f"{bad!r} was accepted as a YYYY-MM month")


def t_calendar_rejects_an_overlong_range():
    """The documented ceilings: 2 months for `day`, 12 for `month`.

    The RAML states them on `dateTo`, and nothing in the response distinguishes
    "no prices" from "range too wide" — so a range past the ceiling has to be
    refused here or it becomes a confident false "nothing available".
    """
    client = client_for([ok(fixture("calendar_days"))])
    client.calendar("JFK", "LIS", "2027-01", "2027-02")     # 2 months: allowed

    client = client_for([])
    try:
        client.calendar("JFK", "LIS", "2027-01", "2027-03")   # 3: one over
    except UsageError:
        assert client.transport.requests_made == 0
    else:
        raise AssertionError("a 3-month day-aggregation range was accepted")

    client = client_for([ok(fixture("calendar_days"))])
    client.calendar("JFK", "LIS", "2027-01", "2027-12", aggregation="month")

    client = client_for([])
    try:
        client.calendar("JFK", "LIS", "2027-01", "2028-01", aggregation="month")
    except UsageError:
        assert client.transport.requests_made == 0
    else:
        raise AssertionError("a 13-month month-aggregation range was accepted")

    # The span is an inclusive count of months, so a backwards window is its
    # own error rather than a negative one that slips under the ceiling.
    client = client_for([])
    try:
        client.calendar("JFK", "LIS", "2027-06", "2027-01")
    except UsageError:
        assert client.transport.requests_made == 0
    else:
        raise AssertionError("--to before --from was accepted")


def t_calendar_parse_labels_predictions():
    search = CalendarSearch.parse(fixture("calendar_days"))
    assert search.origin_name == "Boston Logan International Airport"
    assert search.destination_name == "Los Angeles International Airport"
    assert search.currency == "USD"

    by_key = {d.key: d for d in search.days}
    assert set(by_key) == {"2027-03-02", "2027-03-03", "2027-03-04", "2027-03-05"}
    assert by_key["2027-03-03"].predicted is True, \
        "a predicted row lost its label — the distinction is the whole value"
    assert by_key["2027-03-02"].predicted is False
    assert by_key["2027-03-02"].price == 214.0

    # A row the API priced at nothing must not be the cheapest one.
    assert by_key["2027-03-05"].price is None
    best = search.cheapest()
    assert best is not None and best.key == "2027-03-03" and best.price == 179.0


def t_calendar_round_trip_takes_cheapest_inbound():
    """For a round trip the price hangs off the inbound leg, and there are many.

    `CalendarFlightLegResponse.price` is documented as present only on the
    inbound leg for round trips, because it covers both legs — and
    `inboundLegs` is a list of possible return days. Taking the first one
    reports whichever return date the API happened to list first as though it
    were the cheapest.
    """
    row = [d for d in CalendarSearch.parse(fixture("calendar_days")).days
           if d.key == "2027-03-04"][0]
    assert row.price == 388.0, (
        f"round-trip price is {row.price}; the fixture's inbound legs are "
        f"512, 388, 441 in that order, so 512 means 'first' was mistaken for "
        f"'cheapest'")
    assert row.return_date == "2027-03-11", \
        "the return date must be the one the quoted price belongs to"
    assert row.depart == "2027-03-04"


def t_calendar_empty_parses():
    search = CalendarSearch.parse(fixture("calendar_empty"))
    assert search.days == ()
    assert search.cheapest() is None
    assert search.origin_name, \
        "an empty result still resolved the route; say which airports were priced"


def t_calendar_is_charged_to_the_price_insights_quota():
    """The quota family is `priceinsights`, the name `HOURLY_LIMITS` uses.

    A family string with no entry in that table is not a smaller guard, it is
    no guard at all: `_budget_hourly` finds no limit and never refuses. So the
    string is pinned here, against the table rather than against itself.
    """
    from kayak.cache import HOURLY_LIMITS
    assert "priceinsights" in HOURLY_LIMITS, \
        f"HOURLY_LIMITS has no priceinsights entry: {sorted(HOURLY_LIMITS)}"

    ledger = Cache(enabled=True)
    clock = VirtualClock()
    client = Client(api_key=SENTINEL_KEY, cache=ledger, clock=clock,
                    sleep=clock.sleep,
                    transport=FakeTransport([ok(fixture("calendar_days"))]))
    client.calendar("JFK", "LIS", "2027-03", "2027-03")
    assert ledger.recent_requests("priceinsights") == 1, (
        "the calendar call was not counted against the priceinsights quota; "
        "an unrecognised family name means no rate-limit guard at all")


WHEN_ARGS = ["when", "--origin", "JFK", "--destination", "LIS",
             "--from", "2027-03", "--to", "2027-03"]


def t_when_cli_takes_from_and_to():
    code, out, _ = run_cli(WHEN_ARGS + ["--json"],
                           responses=[ok(fixture("calendar_days"))])
    data = json.loads(out)
    assert code == 0, f"expected 0, got {code}"
    assert data["results"], "no rows from a calendar fixture with four days"
    assert data["query"].get("from") == "2027-03", \
        "`when` takes --from/--to as YYYY-MM; --month is gone"


def t_when_sandbox_suppresses_price_column():
    """B15: `when` must hide sandbox prices the same way `cars` does.

    The calendar endpoint is sandboxed too — a predicted-vs-quoted flag is not
    the same claim as "this number is real money" — so a raw mock fare must
    not print without --sandbox-ok, exactly as for `cars` and `hotels`.
    """
    code, out, _ = run_cli(WHEN_ARGS, responses=[ok(fixture("calendar_days"))])
    assert code == 0
    assert "SANDBOX" in out, "the sandbox banner is missing"
    assert "214" not in out and "179" not in out, \
        "a mock calendar price was printed without --sandbox-ok"


def t_when_sandbox_ok_shows_marked_price():
    """--sandbox-ok must actually reveal the price, marked, not be a no-op.

    Kills the same two mutant classes as the hotels version, one vertical
    over: a `show_price` that ignores the flag, and a deleted per-row
    `SANDBOX_ROW_PREFIX` on calendar rows.
    """
    code, out, _ = run_cli(WHEN_ARGS + ["--sandbox-ok"],
                           responses=[ok(fixture("calendar_days"))])
    assert code == 0
    assert SANDBOX_ROW_PREFIX in out, (
        "--sandbox-ok must mark the price it reveals, not print a bare number")
    assert "214" in out or "179" in out, \
        "the price should actually be visible with --sandbox-ok"


def t_when_sandbox_ok_marks_every_row():
    code, out, _ = run_cli(WHEN_ARGS + ["--sandbox-ok", "--json"],
                           responses=[ok(fixture("calendar_days"))])
    data = json.loads(out)
    assert data["results"], "expected calendar rows"
    assert all(row["priceIsReal"] is False for row in data["results"]), \
        "every sandbox calendar row must carry priceIsReal: false"


def t_when_cli_empty_exits_one():
    code, _, _ = run_cli(WHEN_ARGS, responses=[ok(fixture("calendar_empty"))])
    assert code == 1, f"a calendar with no rows must exit 1, got {code}"


# 15. `check` must actually reach the API.


def t_check_ignores_a_warm_place_cache():
    """`check` answers "is the key usable *now*" — a cache cannot answer that.

    `places` is cached for days, and `check` probes with a `places` call. If
    that call is served from cache, `check` reports a dead or expired key as
    healthy while making no request at all, which is the exact failure it
    exists to catch. `refresh=True` is what makes the call go out.
    """
    warm = Cache(enabled=True)      # memory-only: see _isolate_cache below
    if True:
        transports: list = []
        run_cli(["places", "JFK", "--json"], cache=warm, transports=transports,
                responses=[ok(fixture("autocomplete_cars"))])
        assert transports[-1].requests_made == 1, "the first lookup must call"

        # Same term, same vertical, same key: `places` is now a cache hit.
        run_cli(["places", "JFK", "--json"], cache=warm, transports=transports,
                responses=[])
        assert transports[-1].requests_made == 0, \
            "premise check — the second `places` should be served from cache"

        code, _, _ = run_cli(["check", "--json"], cache=warm,
                             transports=transports,
                             responses=[ok(fixture("autocomplete_cars"))])
        assert code == 0
        assert transports[-1].requests_made == 1, (
            "`check` was answered from the place cache; it must pass "
            "refresh=True so a rejected key is discovered rather than assumed")


# 16. Sorting, and the sandbox marker on --full rows.


def t_limit_keeps_the_cheapest_not_the_first():
    """`--limit 3` must trim the *ranked* list, not the API's arrival order.

    cars_complete puts its cheapest row ($98, an opaque agency) sixth. If the
    limit is applied before the sort, the three rows shown are simply the first
    three that arrived, and the summary calls the cheapest of *those* the
    cheapest car — a confident wrong answer that looks entirely plausible.
    """
    code, out, _ = run_cli(CARS_ARGS + ["--limit", "3", "--sort", "price", "--json"],
                           responses=[ok(fixture("cars_complete"))])
    data = json.loads(out)
    assert code == 0 and len(data["results"]) == 3
    prices = [row["price"]["total"] for row in data["results"]]
    assert prices == sorted(prices), f"rows are not in price order: {prices}"
    assert prices[0] == 98.0, (
        f"first row is {prices[0]}, but the cheapest offer in the fixture is "
        f"98.0 and sits sixth — --limit trimmed before sorting")
    assert data["meta"].get("ranked_by") == "price", \
        "meta must say what the rows were ranked by; 'cheapest' is a claim"


def t_full_rows_are_marked_mocked_in_sandbox():
    """--full must not be a hole in the sandbox honesty rule.

    The projection stamps `priceIsReal: false` on every sandbox row. `--full`
    returns the API's own rows, and a raw row that looks like production data
    is precisely the one an agent will quote.
    """
    _, out, _ = run_cli(CARS_ARGS + ["--json", "--full"],
                        responses=[ok(fixture("cars_complete"))])
    data = json.loads(out)
    assert data["meta"]["prices_are_mocked"] is True
    assert data["results"], "expected raw rows"
    assert all(row.get("priceIsReal") is False for row in data["results"]), \
        "a --full sandbox row carried no priceIsReal:false marker"


# ----------------------------------------------------------------- network


def _live_client() -> Client | None:
    key = os.environ.get("KAYAK_API_KEY")
    if not key:
        return None
    return Client(api_key=key, cache=Cache(enabled=False))


def t_live_autocomplete():
    client = _live_client()
    if client is None:
        return SKIP
    try:
        places = client.places("Toronto", "cars")
    except Exception as exc:                                    # noqa: BLE001
        raise AssertionError(f"sandbox-en-us.kayakaffiliates.com: {exc}")
    assert places, "autocomplete returned nothing for 'Toronto'"


def t_live_cars():
    client = _live_client()
    if client is None:
        return SKIP
    from datetime import timedelta
    start = date.today() + timedelta(days=30)
    try:
        search = client.search_cars("JFK", start, start + timedelta(days=3))
    except SearchTimeout:
        return                        # partial is a legitimate live outcome
    except Exception as exc:                                    # noqa: BLE001
        raise AssertionError(f"sandbox-en-us.kayakaffiliates.com: {exc}")
    assert search.status, "no status in a live response"


OFFLINE = [
    ("doors enum decodes as ranges, never arithmetic", t_doors_ranges),
    ("--min-doors rejects a doors23 car", t_min_doors_filter),
    ("priceless offers sort last, never cheapest", t_none_safe_sort),
    ("join degrades on an unmapped agency", t_join_degrades),
    ("join resolves agency/provider/location", t_join_resolves),
    ("every registered filter survives sparse rows", t_registry_survives_sparse),
    ("a missing field excludes rather than crashes", t_missing_field_excludes),
    ("Money keeps its unit attached", t_money_labels),
    ("request carries priceMode=total and pageSize=500",
     t_request_carries_price_mode_and_page_size),
    ("a priceMode mismatch refuses rather than mislabels",
     t_price_mode_mismatch_raises),
    ("completed empty search exits 1", t_exit_empty_is_one),
    ("rejected key exits 4", t_exit_auth_is_four),
    ("partial with offers exits 5", t_exit_partial_with_offers_is_five),
    ("partial and empty exits 5, never 1", t_exit_partial_empty_is_five_not_one),
    ("completed search with matches exits 0", t_exit_found_is_zero),
    ("--until second-phase reaching it exits 0", t_until_second_phase_is_zero),
    ("one envelope shape across commands", t_envelope_is_uniform),
    ("envelope intact on the failure path", t_envelope_on_failure),
    ("--json does not change the exit code", t_json_does_not_change_exit_code),
    ("filter rejections partition the input", t_filtered_out_partitions),
    ("--sleepable picks SUVs and vans with room", t_sleepable_sugar),
    ("--limit trims the JSON array too", t_limit_trims_json_not_just_the_table),
    ("--full returns raw rows plus the maps", t_full_returns_raw_rows),
    ("--key-file outranks a stale env var", t_key_file_outranks_ambient_env),
    ("sweep ranks days by price, not calendar order", t_sweep_ranks_days_by_price),
    ("no key material reaches output", t_key_never_leaks),
    ("launcher runs from a foreign cwd", t_launcher_absolute_path),
    ("sandbox hides the price column by default", t_sandbox_suppresses_price_column),
    ("sandbox marks every row priceIsReal:false", t_sandbox_ok_marks_every_row),
    ("sweep refuses to rank sandbox prices", t_sweep_refuses_in_sandbox),
    ("sweep: every day network-failing exits 3, not 1",
     t_sweep_all_days_network_failed_exits_three),
    ("sweep: a mixed failure says which days were not checked",
     t_sweep_mixed_failure_says_which_days_were_not_checked),
    ("sweep: all-timeout-with-no-partial exits 5, not 1",
     t_sweep_all_timeouts_with_no_partial_exits_five_not_one),
    ("sweep: per-day UsageErrors are not network failures",
     t_sweep_usage_errors_are_not_network_failures),
    ("sweep: a failed confirm-poll keeps the stage-1 partial, not unchecked",
     t_sweep_confirm_stage_failure_keeps_partial_not_unchecked),
    ("sweep: a genuinely empty sweep still exits 1",
     t_sweep_genuinely_empty_still_exits_one),
    ("poll loop reaches complete and sends cluster", t_poll_reaches_complete),
    ("second-phase settles early when it stops growing",
     t_poll_settles_early_in_second_phase),
    ("the settle rule never shortcuts an --until complete search",
     t_settle_rule_does_not_shortcut_a_complete_search),
    ("budget refuses before issuing a request", t_budget_refuses_before_calling),
    ("usage errors raise UsageError", t_usage_errors),
    ("autocomplete parses", t_places_parse),
    ("hotels sends destination, never placeId or limit",
     t_hotels_request_shape),
    ("hotels sends onlyIfComplete when asked", t_hotels_only_if_complete_is_sent),
    ("a single-hotel lookup sends hotel=, not destination=",
     t_single_hotel_uses_the_hotel_parameter),
    ("an incomplete hotel search parses as incomplete",
     t_hotels_partial_is_not_complete),
    ("an unpriced hotel is never the cheapest", t_hotels_cheapest_ignores_unpriced),
    ("hotels --complete over a partial search exits 5",
     t_hotels_cli_complete_flag_exits_five),
    ("a partial hotel list is labelled, not hidden",
     t_hotels_cli_partial_is_reported_not_hidden),
    ("hotels: a partial empty search exits 5, not 1",
     t_hotels_partial_and_empty_exits_five_not_one),
    ("hotels: an absent completion flag falls through to exit 1, not 5",
     t_hotels_absent_flag_and_empty_exits_one_not_five),
    ("hotels: a completed empty search still exits 1",
     t_hotels_completed_and_empty_still_exits_one),
    ("hotels: --limit 0 does not hide that hotels were found",
     t_hotels_limit_zero_does_not_hide_that_hotels_were_found),
    ("hotels hides the price column in sandbox mode by default",
     t_hotels_sandbox_suppresses_price_column),
    ("hotels --sandbox-ok marks every row priceIsReal:false",
     t_hotels_sandbox_ok_marks_every_row),
    ("hotels --sandbox-ok actually shows the marked price",
     t_hotels_sandbox_ok_shows_marked_price),
    ("the deprecated hotels --place alias still parses",
     t_hotels_deprecated_place_alias_still_works),
    ("hotels with no destination exits 2",
     t_hotels_without_a_destination_is_a_usage_error),
    ("hotels --limit keeps the cheapest, not the most recommended",
     t_hotels_limit_keeps_the_cheapest_not_the_first),
    ("rate-less hotels are reported as unranked",
     t_hotels_without_rates_say_they_are_unranked),
    ("calendar sends PlaceRequest objects and dateFrom/dateTo",
     t_calendar_request_matches_spec),
    ("calendar flags use the spec's field names",
     t_calendar_optional_flags_use_spec_names),
    ("calendar refuses a month that is not YYYY-MM",
     t_calendar_rejects_a_bad_month),
    ("calendar refuses a range past the documented maximum",
     t_calendar_rejects_an_overlong_range),
    ("calendar is charged to the priceinsights quota",
     t_calendar_is_charged_to_the_price_insights_quota),
    ("calendar labels predicted rows", t_calendar_parse_labels_predictions),
    ("a round-trip row takes the cheapest inbound leg",
     t_calendar_round_trip_takes_cheapest_inbound),
    ("an empty calendar still names the route", t_calendar_empty_parses),
    ("when takes --from/--to as YYYY-MM", t_when_cli_takes_from_and_to),
    ("when hides the price column in sandbox mode by default",
     t_when_sandbox_suppresses_price_column),
    ("when --sandbox-ok marks every row priceIsReal:false",
     t_when_sandbox_ok_marks_every_row),
    ("when --sandbox-ok actually shows the marked price",
     t_when_sandbox_ok_shows_marked_price),
    ("an empty calendar exits 1", t_when_cli_empty_exits_one),
    ("check calls the API even with a warm place cache",
     t_check_ignores_a_warm_place_cache),
    ("--limit keeps the cheapest, not the first three",
     t_limit_keeps_the_cheapest_not_the_first),
    ("--full sandbox rows are marked mocked",
     t_full_rows_are_marked_mocked_in_sandbox),
]

def t_live_hotels():
    client = _live_client()
    if client is None:
        return SKIP
    from datetime import timedelta
    start = date.today() + timedelta(days=30)
    try:
        search = client.hotels("kplace:58075", start, start + timedelta(days=2))
    except Exception as exc:                                    # noqa: BLE001
        raise AssertionError(f"sandbox-en-us.kayakaffiliates.com: {exc}")
    assert isinstance(search.complete, bool), \
        "a live hotel response carried no completion flag"


def t_live_calendar():
    client = _live_client()
    if client is None:
        return SKIP
    month = f"{date.today().year + 1:04d}-03"
    try:
        search = client.calendar("BOS", "LAX", month, month)
    except Exception as exc:                                    # noqa: BLE001
        raise AssertionError(f"sandbox-en-us.kayakaffiliates.com: {exc}")
    assert search.origin_name or search.days, \
        "a live calendar response resolved neither the route nor any day"


NETWORK = [
    ("live autocomplete", t_live_autocomplete),
    ("live car search", t_live_cars),
    ("live hotel search", t_live_hotels),
    ("live calendar", t_live_calendar),
]


def _isolate_cache() -> None:
    """Point every cache this process builds at memory, not the user's disk.

    `Cache.record_requests` writes the hourly-quota ledger whether or not the
    cache is enabled — the budget must keep counting even when caching is off.
    The offline group therefore charges its several hundred *fake* requests
    against the real 250/hour car quota, and after two or three runs the suite
    starts failing with BudgetError while a genuine search would be refused
    too. `KAYAK_CACHE_DIR=off` gives each Cache a per-instance in-memory
    ledger, so the checks stay hermetic and cost the user nothing.
    """
    os.environ["KAYAK_CACHE_DIR"] = "off"


def main() -> int:
    parser = argparse.ArgumentParser(description="kayak-browse self-check")
    parser.add_argument("--offline", action="store_true",
                        help="skip the live calls")
    args = parser.parse_args()
    _isolate_cache()

    print("kayak-browse self-check")
    print("NOTE: fixtures are SYNTHETIC — built from the RAML spec, not "
          "captured from a live API.")
    print("      They prove the parser handles shapes we imagined, not the "
          "ones we did not.\n")

    for name, fn in OFFLINE:
        check("offline", name, fn)
    if not args.offline:
        # Live calls spend the real quota, so they get the real ledger back.
        os.environ.pop("KAYAK_CACHE_DIR", None)
        for name, fn in NETWORK:
            check("network", name, fn)

    width = max(len(name) for _, name, _, _ in _results)
    failures = 0
    for group, name, status, detail in _results:
        line = f"[{group}] {name.ljust(width)}  {status}"
        if detail:
            line += f"  — {detail}"
        print(line)
        if status == FAIL:
            failures += 1

    passed = sum(1 for _, _, s, _ in _results if s == PASS)
    skipped = sum(1 for _, _, s, _ in _results if s == SKIP)
    print(f"\n{passed} passed, {failures} failed, {skipped} skipped")
    if skipped:
        print("skipped checks need KAYAK_API_KEY; no key exists for this API yet")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
