#!/usr/bin/env python3
"""Self-check for the campsite-search skill — run this right after installing.

    python3 <skill>/scripts/test_availability.py              # full check
    python3 <skill>/scripts/test_availability.py --offline     # no network
    python3 <skill>/scripts/test_availability.py -v            # show details

Prints a PASS/FAIL line per check and exits non-zero if any failed, so it can
gate an install: `python3 .../test_availability.py || echo "skill is broken"`.

Two groups, honestly labelled:

  [offline]  pure logic — the availability model, natural sort, the provider
             table, the exit codes, that the launcher imports from a foreign
             working directory, and the CLI's exit codes on bad input, run
             against in-memory reference data. No sockets. Also collectable by pytest,
             since these are the only `test_*` functions in the file.

  [network]  live calls to the nine Camis5 tenants. These can fail for reasons
             that are not the skill's fault (a tenant down, no DNS). A failure
             here names the host so the difference is visible.

Reference data is cached on disk, so a second run costs far fewer requests:
measured ~70s on a cold cache, ~10s warm, ~0.3s with --offline.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

# This file has two homes: the skill bundle, where it sits in scripts/ beside
# campsites/, and the repo, where it sits in tests/ one level below it. Walk up
# until the package turns up so the same copy runs correctly from either.
_HERE = Path(__file__).resolve().parent
_PKG_BASE = next(
    (b for b in (_HERE, *_HERE.parents) if (b / "campsites" / "__init__.py").is_file()),
    _HERE,
)
if str(_PKG_BASE) not in sys.path:
    sys.path.insert(0, str(_PKG_BASE))

from campsites.camis import CamisClient, _natural  # noqa: E402
from campsites.cli import EXIT_NET, EXIT_NONE, EXIT_OK, EXIT_USAGE  # noqa: E402
from campsites.model import (  # noqa: E402
    BOOKING_ALIASES,
    Availability,
    Opening,
    ResourceCategory,
    ResourceType,
    Site,
    SiteInfo,
)
from campsites.providers import CAMIS_PROVIDERS, OTHER_PROVIDERS, resolve  # noqa: E402

# The bundle ships a campsites.py shim beside this file — the exact entry point
# the agent invokes, so prefer it. The repo has no shim and is driven through
# `python3 -m campsites`; either way the call below stays an absolute one made
# from a foreign cwd, which is what these checks are really asserting.
LAUNCHER = _HERE / "campsites.py"
_CLI_ARGV = [str(LAUNCHER)] if LAUNCHER.is_file() else ["-m", "campsites"]

A = Availability.AVAILABLE
U = Availability.UNAVAILABLE
N = Availability.NOT_OPERATING

# ---------------------------------------------------------------- fixtures

TODAY = date.today()

#: A window far enough out to be inside every tenant's booking horizon and
#: early enough to be inside the operating season, with shoulder-season midweek
#: nights that are almost never sold out. Relative to today so it never rots.
WIN_START = TODAY + timedelta(days=50)
WIN_END = WIN_START + timedelta(days=7)

#: One park per major tenant, spanning both Camis flavours (dedicated hosts and
#: goingtocamp), used to prove reference data loads end to end.
REFERENCE_PARKS = {
    "pc": "Banff - Two Jack Lakeside",
    "ontario": "Killarney Provincial Park",
    "bc": "Alice Lake Provincial Park",
    "novascotia": "Battery Provincial Park",
}

#: The exact command that used to raise
#: `TypeError: '<' not supported between instances of 'str' and 'int'`.
#: Rolled forward if those dates have passed — the bug is about the *names* in
#: the result set (Y3, C1 alongside 39), not about the calendar.
_KILL_START, _KILL_END = date(2026, 8, 22), date(2026, 8, 24)
if _KILL_START <= TODAY:
    _KILL_START, _KILL_END = TODAY + timedelta(days=7), TODAY + timedelta(days=9)

#: Parks that mix numeric and lettered site names in one result set — every one
#: of these took `search` and `find` down before `_natural` was fixed.
MIXED_NAME_PARKS = ["Y3", "C1", "A209", "RA 6", "W2", "39", "2", "10", "9"]

# ---------------------------------------------------------------- registry

_CHECKS: list[tuple[str, str, object]] = []


def offline(fn):
    _CHECKS.append(("offline", fn.__name__, fn))
    return fn


def network(fn):
    _CHECKS.append(("network", fn.__name__, fn))
    return fn


def run_cli(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Invoke the launcher by absolute path from a foreign working directory.

    Running from a temp dir rather than the skill directory is the point: if
    the package only resolves because `cwd` happens to contain it, this fails.
    """
    env = {**os.environ, "PYTHONPATH": str(_PKG_BASE)}
    return subprocess.run(
        [sys.executable, *_CLI_ARGV, *args],
        capture_output=True, text=True, timeout=timeout,
        cwd=tempfile.gettempdir(), env=env,
    )


def run_json(*args: str, timeout: int = 180) -> tuple[subprocess.CompletedProcess, dict]:
    proc = run_cli(*args, "--json", timeout=timeout)
    try:
        return proc, json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"`{' '.join(args)} --json` did not emit JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout[:300]}\nstderr: {proc.stderr[:300]}"
        )


# =========================================================== offline checks
# Named test_* so `pytest test_availability.py` collects exactly this group.


@offline
def test_availability_model_ignores_the_departure_date():
    """The regression that hid 15.4% of real availability at Pinery.

    The API returns one entry per calendar date INCLUSIVE of the departure
    date, so a 2-night stay yields 3 entries and the third is checkout — a
    site booked by someone arriving as you leave is still bookable by you.
    `search` slices codes[:nights], so Site only ever sees the nights.
    """
    def site(*nights):
        return Site(resource_id=-1, name="285", map_name="Area A", nights=tuple(nights))

    assert site(A, A).available
    assert not site(A, U).available
    assert site(A, A).free_nights == 2

    partial = site(A, U, A)
    assert not partial.available and partial.partial
    assert partial.status is Availability.PARTIALLY_AVAILABLE

    # With no free nights, report the dominant blocker, not a coin flip.
    assert site(N, N, U).status is Availability.NOT_OPERATING
    assert site(U, U, N).status is Availability.UNAVAILABLE
    # An empty array must never read as "available".
    assert not site().available


@offline
def test_sweep_finds_every_run_of_free_nights():
    codes = [A, A, U, A, A, A, U]
    starts = [i for i in range(len(codes) - 1) if all(c.bookable for c in codes[i:i + 2])]
    assert starts == [0, 3, 4]
    # A stay covers a Fri/Sat night, or it does not. 2026-09-10 is a Thursday.
    assert Opening(-1, "1", "A", date(2026, 9, 10), 2).weekend
    assert not Opening(-1, "1", "A", date(2026, 9, 7), 2).weekend


@offline
def test_natural_sort_survives_mixed_numeric_and_lettered_sites():
    """Regression: `_natural` used to emit bare ints beside bare strings, so
    sorting a park that mixes "39" with "Y3" raised
    `TypeError: '<' not supported between instances of 'str' and 'int'`
    and took the whole search down. Killarney (Y3, C1), Sandbanks (A209),
    Pinery (RA 6) and Windy Lake (W2) all mix them."""
    sorted(MIXED_NAME_PARKS, key=_natural)          # must not raise
    sorted(["1", "A", "1A", "A1", "", "10-B"], key=_natural)

    # Every element must be a same-shaped tuple, or the mix only *happens* to
    # compare because the first components differ.
    for part in _natural("RA 6"):
        assert isinstance(part, tuple) and len(part) == 3
        assert isinstance(part[0], int) and isinstance(part[1], int)
        assert isinstance(part[2], str)

    # Still a natural sort, not a lexical one.
    assert sorted(["10", "2", "9"], key=_natural) == ["2", "9", "10"]
    assert sorted(["A10", "A2", "A9"], key=_natural) == ["A2", "A9", "A10"]
    # Numbers sort ahead of letters, so "39" precedes "Y3".
    assert sorted(["Y3", "39", "C1"], key=_natural) == ["39", "C1", "Y3"]


@offline
def test_all_nine_providers_are_declared():
    expected = {
        "pc": "reservation.pc.gc.ca",
        "ontario": "reservations.ontarioparks.ca",
        "grca": "www.grcacamping.ca",
        "bc": "camping.bcparks.ca",
        "manitoba": "manitoba.goingtocamp.com",
        "novascotia": "novascotia.goingtocamp.com",
        "newfoundland": "www.nlcamping.ca",
        "yukon": "yukon.goingtocamp.com",
        # The booking system lives on reservations.*, not the www marketing
        # site — that is how this tenant was missed the first time round.
        "newbrunswick": "reservations.parcsnbparks.ca",
    }
    assert set(CAMIS_PROVIDERS) == set(expected), (
        f"provider table drifted: {sorted(set(CAMIS_PROVIDERS) ^ set(expected))}")
    for key, host in expected.items():
        assert CAMIS_PROVIDERS[key].host == host
        assert resolve(key) == host
    assert resolve("some.other.host") == "some.other.host"

    # Non-Camis tenants must fail loudly rather than being treated as a host.
    for key in OTHER_PROVIDERS:
        try:
            resolve(key)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} is not Camis5 but resolve() accepted it")


@offline
def test_stay_type_classification():
    def roofed(name, t=ResourceType.ONSITE):
        return ResourceCategory(-1, name, t).roofed

    for name in ("oTENTik", "Ôasis", "Yurt", "MicrOcube", "Teepee", "Cabin",
                 "Rustic Cabin", "Cottage", "Prospector Tent", "Equipped Camping",
                 "Soft-sided Shelter", "Backcountry Cabin", "Elfin Lakes Shelter"):
        assert roofed(name), f"{name} should be roofed"
    for name in ("Campsite", "Group Campsite", "Overflow", "Other",
                 "Backcountry Site", "Access Point", "Parking", "Ferry"):
        assert not roofed(name), f"{name} should not be roofed"
    # Regression: a plain substring test made "hut" match "s-hut-tle".
    assert not roofed("Shuttle", ResourceType.ACTIVITY)


@offline
def test_filters_match_case_insensitively():
    info = SiteInfo(resource_id=-1, name="285",
                    attributes={"Service Type": "Electric", "Privacy": "Good"})
    assert info.matches({"service type": "electric"})
    assert not info.matches({"Service Type": "Non-Electric"})
    assert not info.matches({"Pull-through": "Yes"})          # absent attribute
    multi = SiteInfo(resource_id=-1, name="1",
                     attributes={"Conditions": "Poor Drainage, Poison Ivy"})
    assert multi.matches({"Conditions": "Poison Ivy"})

    from campsites.camis import _type_match
    otentik = SiteInfo(resource_id=-1, name="oT4", category="oTENTik")
    assert _type_match(otentik, ["otentik"]) and _type_match(otentik, ["OTENTIK"])
    assert not _type_match(otentik, ["yurt"])
    assert not _type_match(None, ["otentik"])
    # A site with no category never matches a type filter.
    assert not _type_match(SiteInfo(resource_id=-1, name="1"), ["cabin"])


@offline
def test_booking_aliases_hold_names_never_tenant_ids():
    """Id 2 is "Group Campsite" on Parks Canada, "Roofed Accommodation" on
    Ontario and "Cabin" on BC, so a hardcoded number is wrong on two of three."""
    for alias, keywords in BOOKING_ALIASES.items():
        assert keywords, f"{alias} has no keywords"
        for k in keywords:
            assert isinstance(k, str) and not k.isdigit()
    for required in ("campsite", "roofed", "cabin", "group", "backcountry"):
        assert required in BOOKING_ALIASES


@offline
def test_exit_codes_are_the_documented_four():
    assert (EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_NET) == (0, 1, 2, 3), (
        "a watch loop treats only 1 as 'keep waiting' — these must not move")


@offline
def test_launcher_runs_from_a_foreign_working_directory():
    """The skill is invoked by absolute path from wherever the agent happens to
    be, so the package must resolve without the cwd helping."""
    proc = run_cli("--help", timeout=60)
    assert proc.returncode == 0, f"--help exited {proc.returncode}: {proc.stderr[:300]}"
    for cmd in ("search", "sweep", "find", "site", "stays", "providers"):
        assert cmd in proc.stdout, f"`{cmd}` missing from --help"

    # `providers` is served entirely from the static table, so it is the one
    # real command that proves the wiring without touching the network.
    proc, data = run_json("providers", timeout=60)
    assert proc.returncode == EXIT_OK
    assert len(data["camis"]) == 9, f"expected 9 tenants, got {len(data['camis'])}"


@offline
def test_unknown_provider_is_a_usage_error_not_a_crash():
    proc = run_cli("parks", "nosuchprovider", timeout=60)
    assert proc.returncode == EXIT_USAGE, (
        f"unknown provider gave exit {proc.returncode}, want {EXIT_USAGE}")
    # An unsupported-but-known tenant must explain itself, not 404 mysteriously.
    proc = run_cli("parks", "quebec", timeout=60)
    assert proc.returncode == EXIT_USAGE
    assert "sepaq" in proc.stderr.lower() or "Camis5" in proc.stderr


def _offline_client() -> CamisClient:
    """A real CamisClient whose reference data is pre-seeded in memory, so
    lookups, validation and the CLI wiring run with no socket. Anything that
    would reach /api/availability must be stubbed by the caller."""
    def lv(**kw):
        return [{"cultureName": "en-CA", **kw}]

    client = CamisClient("grca", use_cache=False)
    client._mem.update({
        "parks": [
            {"resourceLocationId": -1, "localizedValues": lv(fullName="Pinery Provincial Park")},
            {"resourceLocationId": -2, "localizedValues": lv(fullName="Pinery Group Area")},
        ],
        "equipment": [{
            "equipmentCategoryId": -10, "localizedValues": lv(name="Camping"),
            "subEquipmentCategories": [
                {"subEquipmentCategoryId": -11, "localizedValues": lv(name="Single Tent")}],
        }],
        "bookingcategories": [{"bookingCategoryId": 0, "localizedValues": lv(name="Campsite")}],
        "maps:-1": [{"mapId": -5, "mapResources": [{"resourceId": 1}]}],
        "maps:-2": [{"mapId": -6, "mapResources": [{"resourceId": 2}]}],
    })
    return client


def _run_main(client: CamisClient, *argv: str) -> tuple[int, str, str]:
    """Run the CLI in-process against `client`; returns (exit, stdout, stderr)."""
    import contextlib
    import io

    from campsites import cli

    out, err = io.StringIO(), io.StringIO()
    saved = cli._client
    cli._client = lambda args: client
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        cli._client = saved
    return code, out.getvalue(), err.getvalue()


_D0 = (TODAY + timedelta(days=30)).isoformat()
_D3 = (TODAY + timedelta(days=33)).isoformat()
_D400 = (TODAY + timedelta(days=430)).isoformat()


@offline
def test_find_rejects_bad_input_up_front_not_as_nothing_available():
    """Regression: `find` skipped each park on a ValueError/LookupError, so a
    too-long span or a typo'd --equipment exited 1 and a watch polled forever.
    Input that is wrong for every park must exit 2 before any park is tried."""
    cases = {
        "span over 367 days": ["--start", _D0, "--end", _D400],
        "end before start": ["--start", _D3, "--end", _D0],
        "unknown equipment": ["--start", _D0, "--end", _D3, "--equipment", "zzz"],
        "unknown category": ["--start", _D0, "--end", _D3, "--booking-category", "zzz"],
    }
    for label, extra in cases.items():
        for as_json in (False, True):
            client = _offline_client()

            def never(*a, **k):
                raise AssertionError(f"{label}: sweep_many ran — not validated up front")
            client.sweep_many = never
            argv = ["find", "grca", "Pinery", *extra] + (["--json"] if as_json else [])
            code, out, _ = _run_main(client, *argv)
            assert code == EXIT_USAGE, f"{label} (json={as_json}) exited {code}, want 2"
            if as_json:
                data = json.loads(out)
                assert data["ok"] is False and data["exit_code"] == EXIT_USAGE


@offline
def test_check_span_boundaries():
    """Exactly MAX_SPAN_DAYS is the API's limit and must pass; one more day is
    the silent-empty-body case; a zero-night span is never a valid query."""
    from campsites.camis import SpanTooLongError, check_span
    from campsites.model import MAX_SPAN_DAYS

    d = date(2026, 1, 1)
    assert check_span(d, d + timedelta(days=MAX_SPAN_DAYS)) == MAX_SPAN_DAYS
    assert check_span(d, d + timedelta(days=1)) == 1
    try:
        check_span(d, d + timedelta(days=MAX_SPAN_DAYS + 1))
    except SpanTooLongError:
        pass
    else:
        raise AssertionError(f"{MAX_SPAN_DAYS + 1}-day span was accepted")
    for end in (d, d - timedelta(days=1)):
        try:
            check_span(d, end)
        except SpanTooLongError:
            raise AssertionError(f"end {end} misreported as too long")
        except ValueError:
            pass
        else:
            raise AssertionError(f"end {end} <= start {d} was accepted")


@offline
def test_cache_clear_json_is_an_object():
    """--json must parse on every command, cache-clear included."""
    from campsites import cache as cache_mod

    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get(cache_mod.ENV_CACHE_DIR)
        os.environ[cache_mod.ENV_CACHE_DIR] = tmp
        try:
            cache_mod.Cache().set("h", "parks", [1])
            code, out, _ = _run_main(_offline_client(), "cache-clear", "--json")
        finally:
            if saved is None:
                os.environ.pop(cache_mod.ENV_CACHE_DIR, None)
            else:
                os.environ[cache_mod.ENV_CACHE_DIR] = saved
    data = json.loads(out)
    assert code == EXIT_OK and data["ok"] is True and data["schema_version"] == 1
    assert data["cleared"] == 1 and data["dir"] == tmp, data


@offline
def test_sweep_many_reraises_span_too_long():
    """The span is the same for every park, so skipping it per park would
    silently turn a bad request into "nothing available"."""
    from campsites.camis import SpanTooLongError

    client = _offline_client()
    parks = client.find_parks("Pinery")
    try:
        client.sweep_many(parks, TODAY, TODAY + timedelta(days=400))
    except SpanTooLongError:
        pass
    else:
        raise AssertionError("sweep_many swallowed SpanTooLongError")


@offline
def test_find_with_every_park_skipped_is_not_nothing_available():
    """If every park errored, nothing was searched: exit 2, not 1. A network
    error from inside the loop must still surface as 3."""
    from campsites.http import CamisHTTPError

    for as_json in (False, True):
        client = _offline_client()
        client.sweep_many = lambda parks, *a, **k: (
            {}, {p.id: "No maps match" for p in parks})
        argv = ["find", "grca", "Pinery", "--start", _D0, "--end", _D3]
        code, out, err = _run_main(client, *argv, *(["--json"] if as_json else []))
        assert code == EXIT_USAGE, f"all-skipped exited {code} (json={as_json}), want 2"
        assert "skipped" in err

        def down(*a, **k):
            raise CamisHTTPError("/api/availability/map -> cannot resolve host")
        client.sweep_many = down
        code, _, _ = _run_main(client, *argv, *(["--json"] if as_json else []))
        assert code == EXIT_NET, f"network error exited {code}, want 3"

    # One park skipped among several that searched fine is still a real "none".
    client = _offline_client()
    client.sweep_many = lambda parks, *a, **k: ({}, {parks[0].id: "No maps match"})
    code, _, _ = _run_main(client, "find", "grca", "Pinery", "--start", _D0, "--end", _D3)
    assert code == EXIT_NONE, f"partially-skipped exited {code}, want 1"


@offline
def test_horizon_without_data_exits_the_same_in_text_and_json():
    """Regression: text mode raised KeyError on the `error` payload (surfacing
    as exit 3) while --json exited 1."""
    client = _offline_client()
    client.horizon = lambda park, **k: {
        "park": "Pinery Provincial Park", "probed_maps": ["A"],
        "error": "no availability data returned"}
    code, out, err = _run_main(client, "horizon", "grca", "Pinery Provincial Park")
    assert "KeyError" not in err, err
    assert code == EXIT_NONE, f"text exited {code}, want 1: {err[:200]}"
    assert "no availability data" in out
    code, out, _ = _run_main(client, "horizon", "grca", "Pinery Provincial Park", "--json")
    assert code == EXIT_NONE and "error" in json.loads(out)


@offline
def test_horizon_reaching_only_today_is_not_none_detected():
    """days_out == 0 is a real window edge, not a missing one."""
    client = _offline_client()
    client.horizon = lambda park, **k: {
        "park": "Pinery Provincial Park", "probed_maps": ["A"], "from": TODAY.isoformat(),
        "last_date_with_availability": None,
        "last_date_in_booking_window": TODAY.isoformat(), "days_out": 0,
        "booking_windows": []}
    code, out, _ = _run_main(client, "horizon", "grca", "Pinery Provincial Park")
    assert "none detected" not in out, out
    assert "0 days out" in out and code == EXIT_OK


@offline
def test_sweep_json_names_the_resolved_park_and_help_explains_end():
    """`park` must be the park actually searched, not whatever was typed —
    otherwise a caller can't tell which of several matches it got."""
    client = _offline_client()
    client.sweep = lambda park, *a, **k: []
    code, out, _ = _run_main(client, "sweep", "grca", "pinery provincial",
                             "--start", _D0, "--end", _D3, "--json")
    assert code == EXIT_NONE
    assert json.loads(out)["park"] == "Pinery Provincial Park", out[:300]

    # The text header must say the range is inclusive — the misreading
    # SKILL.md warns about is "availability up to --end".
    code, out, _ = _run_main(client, "sweep", "grca", "pinery provincial",
                             "--start", _D0, "--end", _D3)
    header = out.splitlines()[0]
    assert "inclusive" in header and "Pinery Provincial Park" in header, header

    # --end is exclusive (departure) for search, inclusive everywhere else.
    helps = {cmd: run_cli(cmd, "--help", timeout=60).stdout.replace("\n", " ")
             for cmd in ("search", "sweep", "find", "site")}
    assert "departure" in helps["search"] and "INCLUSIVE" not in helps["search"]
    for cmd in ("sweep", "find", "site"):
        assert "INCLUSIVE" in helps[cmd] and "departure" not in helps[cmd], cmd


# =========================================================== network checks


@network
def check_all_nine_providers_serve_parks():
    """Every declared tenant answers /api/resourceLocation with real parks."""
    failures, counts = [], {}
    for key, provider in CAMIS_PROVIDERS.items():
        try:
            parks = CamisClient(key).parks()
            assert parks, "returned zero parks"
            assert all(p.name and p.id for p in parks), "park with no name or id"
            counts[key] = len(parks)
        except Exception as e:
            failures.append(f"{key} ({provider.host}): {type(e).__name__}: {e}")
    detail(", ".join(f"{k}={v}" for k, v in counts.items()))
    assert not failures, "tenants failed:\n  " + "\n  ".join(failures)


@network
def check_reference_data_loads_per_tenant():
    """parks / stays / equipment / attrs for one park on each major tenant."""
    for key, park_name in REFERENCE_PARKS.items():
        client = CamisClient(key)
        park = client.find_park(park_name)
        assert park.name, f"{key}: park has no name"

        equip = client.equipment()
        assert equip, f"{key}: no equipment"
        assert any("tent" in e.name.lower() for e in equip), (
            f"{key}: no tent equipment — {[e.name for e in equip][:8]}")

        cats = client.booking_categories()
        assert cats, f"{key}: no booking categories"

        stays = client.resource_categories()
        assert stays, f"{key}: no resource categories"
        present = {k: v for k, v in client.stay_types(park.id).items() if v}
        assert present, f"{key}: {park.name} reports no bookable stay types"

        facets = client.facets(park.id)
        assert facets, f"{key}: {park.name} exposes no filterable attributes"

        detail(f"{key}: {park.name} — {len(equip)} equipment, {len(cats)} categories, "
               f"{len(stays)} stay types, {len(facets)} attributes, "
               f"{len(present)} types in park")


@network
def check_search_returns_sane_data():
    """A real availability query, checked against the shape the recipes promise."""
    proc, data = run_json(
        "search", "ontario", REFERENCE_PARKS["ontario"],
        "--start", WIN_START.isoformat(), "--end", WIN_END.isoformat(),
    )
    assert proc.returncode in (EXIT_OK, EXIT_NONE), (
        f"search exited {proc.returncode}: {proc.stderr[:300]}")
    assert data["ok"] is True and data["schema_version"] == 1

    assert data["start"] == WIN_START.isoformat()
    assert data["end"] == WIN_END.isoformat()
    assert data["nights"] == (WIN_END - WIN_START).days, (
        "nights must be end-minus-start — the departure night is not part of the stay")
    assert data["party_size"] == 2 and data["equipment"]
    assert data["requests"] >= 1
    assert sum(data["counts"].values()) >= len(data["available"])

    for site in data["available"]:
        assert site["site"] and site["area"], f"nameless site: {site}"
        assert isinstance(site["resource_id"], int)

    detail(f"{data['park']} {data['start']}..{data['end']}: "
           f"{len(data['available'])} available, {len(data['partial'])} partial, "
           f"{data['requests']} requests, counts={data['counts']}")
    assert (proc.returncode == EXIT_OK) == bool(data["available"]), (
        "exit 0 must mean the available list is non-empty, and exit 1 that it is empty")


@network
def check_sweep_check_in_dates_stay_inside_the_window():
    """The `--end` inclusivity trap.

    `search` treats `--end` as the DEPARTURE date, but `sweep` walks the whole
    span and will happily return a stay checking in on `--end - nights + 1`,
    whose check-out is one day PAST `--end`. A caller reading the rows
    literally must still be able to trust `check_in`, so every returned
    check-in has to land inside [start, end], and the overhang on check-out
    has to stay at exactly one day rather than silently growing.
    """
    nights = 2
    proc, data = run_json(
        "sweep", "ontario", REFERENCE_PARKS["ontario"],
        "--start", WIN_START.isoformat(), "--end", WIN_END.isoformat(),
        "--nights", str(nights),
    )
    assert proc.returncode in (EXIT_OK, EXIT_NONE), (
        f"sweep exited {proc.returncode}: {proc.stderr[:300]}")
    assert data["window"] == {"start": WIN_START.isoformat(), "end": WIN_END.isoformat()}
    assert data["nights"] == nights

    rows = data["dates"]
    if not rows:
        detail("sweep returned no openings — window bound unverified this run")
        assert proc.returncode == EXIT_NONE
        return

    for row in rows:
        check_in = date.fromisoformat(row["check_in"])
        check_out = date.fromisoformat(row["check_out"])
        assert WIN_START <= check_in <= WIN_END, (
            f"check_in {check_in} is outside the requested {WIN_START}..{WIN_END}")
        assert check_out == check_in + timedelta(days=nights), (
            f"check_out {check_out} does not match check_in + {nights} nights")
        assert check_out <= WIN_END + timedelta(days=1), (
            f"check_out {check_out} runs more than one day past --end {WIN_END}")
        assert check_in.strftime("%a") == row["weekday"]
        assert row["site_count"] >= 1 and row["sites"]

    last = max(date.fromisoformat(r["check_in"]) for r in rows)
    detail(f"{len(rows)} check-in dates, last {last} (window ends {WIN_END}); "
           f"check-outs run at most 1 day past --end")


@network
def check_killarney_mixed_site_names_no_longer_crash():
    """The exact reported repro, end to end.

    Killarney returns Y3 and C1 beside numeric sites, which used to raise
    TypeError inside the sort and surface as exit 3.
    """
    proc, data = run_json(
        "search", "ontario", "Killarney Provincial Park",
        "--start", _KILL_START.isoformat(), "--end", _KILL_END.isoformat(),
        "--party", "1",
    )
    assert "TypeError" not in proc.stderr, f"still crashing:\n{proc.stderr[:500]}"
    assert proc.returncode in (EXIT_OK, EXIT_NONE), (
        f"exited {proc.returncode} (want 0 or 1): {proc.stderr[:400]}")
    assert data["ok"] is True

    # The sort runs over every site in the park, so it is exercised even when
    # nothing is available — but if rows came back, verify the order too.
    listed = data["available"] or data["partial"]
    by_area: dict[str, list[str]] = {}
    for site in listed:
        by_area.setdefault(site["area"], []).append(site["site"])
    for area, names in by_area.items():
        assert names == sorted(names, key=_natural), (
            f"{area} is not in natural order: {names[:12]}")

    detail(f"{data['park']} {data['start']}..{data['end']} party 1: exit "
           f"{proc.returncode}, {len(data['available'])} available, "
           f"{len(data['partial'])} partial, {len(by_area)} areas ordered")


@network
def check_exit_code_contract():
    """0 found / 1 nothing / 2 usage / 3 network — asserted one at a time.

    A watch loop retries on 1 and only on 1, so a lookup typo or a dead host
    must never masquerade as "nothing available yet".
    """
    park = REFERENCE_PARKS["ontario"]

    # 0 — something was found. `providers` always finds its own static table.
    proc = run_cli("providers", timeout=60)
    assert proc.returncode == EXIT_OK, f"providers exited {proc.returncode}"

    # 1 — the query worked, nothing matched. A party of 99 fits no campsite,
    # so every site comes back INVALID rather than erroring.
    proc, data = run_json(
        "search", "ontario", park,
        "--start", WIN_START.isoformat(), "--end", WIN_END.isoformat(),
        "--party", "99",
    )
    assert proc.returncode == EXIT_NONE, (
        f"party-of-99 exited {proc.returncode}, want {EXIT_NONE}: {proc.stderr[:300]}")
    assert data["ok"] is True and not data["available"], (
        "exit 1 must still be a successful query with an empty result")

    # 2 — usage / lookup error. A park that does not exist.
    proc, data = run_json("search", "ontario", "Nonexistent Park Zzzqx",
                          "--start", WIN_START.isoformat(), "--end", WIN_END.isoformat())
    assert proc.returncode == EXIT_USAGE, (
        f"bad park name exited {proc.returncode}, want {EXIT_USAGE}")
    assert data["ok"] is False and data["exit_code"] == EXIT_USAGE and data["error"]

    # 2 — an over-long span is a usage error too: the API returns an empty body
    # rather than an error, so silently reporting "nothing available" would lie.
    proc = run_cli("sweep", "ontario", park, "--start", WIN_START.isoformat(),
                   "--end", (WIN_START + timedelta(days=400)).isoformat())
    assert proc.returncode == EXIT_USAGE, (
        f"400-day span exited {proc.returncode}, want {EXIT_USAGE}")

    # 3 — network / API error. A host that cannot resolve.
    proc, data = run_json("parks", "camping.invalid-host-zzz.example", timeout=120)
    assert proc.returncode == EXIT_NET, (
        f"unreachable host exited {proc.returncode}, want {EXIT_NET}")
    assert data["ok"] is False and data["exit_code"] == EXIT_NET

    detail("0 found / 1 nothing / 2 usage (bad park, over-long span) / 3 unreachable host")


# ---------------------------------------------------------------- runner

_DETAILS: list[str] = []


def detail(line: str) -> None:
    """Record a line shown under the check name when -v is passed."""
    _DETAILS.append(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Self-check for the campsite-search skill.")
    parser.add_argument("--offline", action="store_true",
                        help="skip every check that touches the network")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show what each check actually saw")
    parser.add_argument("-k", metavar="SUBSTR", help="only run checks matching SUBSTR")
    args = parser.parse_args(argv)

    checks = [c for c in _CHECKS if not (args.offline and c[0] == "network")]
    if args.k:
        checks = [c for c in checks if args.k in c[1]]
    if not checks:
        print("no checks selected", file=sys.stderr)
        return 2

    print(f"campsite-search self-check — {len(checks)} checks"
          + (" (offline only)" if args.offline else "") + "\n")

    failures: list[tuple[str, str]] = []
    started = time.time()
    for group, name, fn in checks:
        _DETAILS.clear()
        label = name.removeprefix("test_").removeprefix("check_").replace("_", " ")
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, "".join(
                traceback.format_exception_only(type(e), e)).strip()))
            print(f"  FAIL  [{group}] {label}  ({time.time() - t0:.1f}s)")
            for line in _DETAILS:
                print(f"          {line}")
        else:
            print(f"  PASS  [{group}] {label}  ({time.time() - t0:.1f}s)")
            if args.verbose:
                for line in _DETAILS:
                    print(f"          {line}")

    elapsed = time.time() - started
    print(f"\n{len(checks) - len(failures)}/{len(checks)} passed in {elapsed:.1f}s")
    if failures:
        print("\nFAILURES")
        for name, err in failures:
            print(f"\n  {name}")
            for line in err.splitlines():
                print(f"    {line}")
        print(f"\nFAIL — {len(failures)} check(s) failed")
        return 1
    if args.offline:
        print("PASS (offline) — re-run without --offline to check the live tenants")
    else:
        print("PASS — the skill is working")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
