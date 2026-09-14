#!/usr/bin/env python3
"""Self-check for the cineplex-showtimes skill — run this right after installing.

    python3 <skill>/scripts/test_cineplex.py              # full check
    python3 <skill>/scripts/test_cineplex.py --offline     # no network
    python3 <skill>/scripts/test_cineplex.py -v            # show details

Prints a PASS/FAIL line per check and exits non-zero if any failed, so it can
gate an install: `python3 .../test_cineplex.py || echo "skill is broken"`.

Two groups, honestly labelled:

  [offline]  pure logic against real API responses saved in fixtures/ — the
             204 handling, experience matching, seat filtering and every exit
             code. No sockets: the transport is replaced with the fixtures.

  [network]  live calls to apis.cineplex.com. These can fail for reasons that
             are not the skill's fault (Cineplex down, no DNS, a rotated key
             that can't be scraped). A failure here names the host.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import cineplex_api as api  # noqa: E402
import cineplex_showtimes as cli  # noqa: E402

FIXTURES = _HERE / "fixtures"
HOST = "apis.cineplex.com"

# Real responses, trimmed to a few movies but otherwise verbatim:
#   showtimes_7130  Yonge-Dundas: VIP 19+, IMAX, Regular, UltraAVX+3D+Atmos, UltraAVX+D-BOX+Atmos
#   showtimes_7408  Vaughan: IMAX+70mm (The Odyssey), 3D, UltraAVX+ScreenX
#   seat_*_7130_446546  a 77-seat VIP auditorium with 8 open seats, all in AA and A
ST_7130 = json.loads((FIXTURES / "showtimes_7130.json").read_text())
ST_7408 = json.loads((FIXTURES / "showtimes_7408.json").read_text())
LAYOUT = json.loads((FIXTURES / "seat_layout_7130_446546.json").read_text())
AVAIL = json.loads((FIXTURES / "seat_availability_7130_446546.json").read_text())

# ---------------------------------------------------------------- registry

_CHECKS: list[tuple[str, str, object]] = []
_DETAILS: list[str] = []


def offline(fn):
    _CHECKS.append(("offline", fn.__name__, fn))
    return fn


def network(fn):
    _CHECKS.append(("network", fn.__name__, fn))
    return fn


def note(line: str) -> None:
    _DETAILS.append(line)


def _response(status: int, body=b"") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    r.url = f"https://{HOST}/fake"
    return r


class FakeTransport:
    """Stands in for ``api._get``: routes by URL to fixtures, so the real
    fetch/filter/CLI code runs end to end without a socket."""

    def __init__(self, showtimes=None, layout=LAYOUT, avail=AVAIL, error=None):
        self.showtimes, self.layout, self.avail, self.error = showtimes, layout, avail, error
        self.calls = []

    def __call__(self, url, params, session):
        self.calls.append((url, dict(params)))
        if self.error:
            raise self.error
        if url == api.SHOWTIMES_URL:
            return self.showtimes
        if url == api.THEATRES_URL:
            return {"otherTheatres": [{"theatreId": 7130, "theatreName": "Yonge-Dundas"},
                                      {"theatreId": 7408, "theatreName": "Vaughan"}]}
        if url.endswith("/seat-layout"):
            return self.layout
        if url.endswith("/seat-availability"):
            return self.avail
        raise AssertionError(f"unexpected URL {url}")


@contextlib.contextmanager
def fake(transport):
    real = api._get
    api._get = transport
    try:
        yield transport
    finally:
        api._get = real


def run_main(*argv) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


def both_modes(*argv) -> int:
    """Run with and without --json; the exit code must not change."""
    rc_text, _ = run_main(*argv)
    rc_json, out = run_main(*argv, "--json")
    assert rc_text == rc_json, f"{argv}: exit {rc_text} as text but {rc_json} under --json"
    return rc_json


def count_sessions(data, label) -> int:
    """Independent oracle: sessions whose raw labels contain ``label`` exactly."""
    return sum(len(e["sessions"]) for t in data for d in t["dates"]
               for m in d["movies"] for e in m["experiences"]
               if label in e["experienceTypes"])


# =========================================================== offline checks


@offline
def test_204_and_empty_body_are_no_data_not_errors():
    """B1: a date with no showtimes answers 204 with no body. It used to raise
    a JSON error that the CLI reported as "Network error"."""
    class Session:
        def __init__(self, resp):
            self.resp = resp

        def get(self, *a, **k):
            return self.resp

    real_key = api.get_subscription_key
    api.get_subscription_key = lambda s, force_refresh=False: "0" * 32
    try:
        assert api._get(api.SHOWTIMES_URL, {}, Session(_response(204))) is None
        assert api._get(api.SHOWTIMES_URL, {}, Session(_response(200, b"  "))) is None
        assert api._get(api.SHOWTIMES_URL, {}, Session(_response(200, ST_7130))) == ST_7130
    finally:
        api.get_subscription_key = real_key
    assert api.flatten_showtimes(None) == []


@offline
def test_empty_date_in_a_multi_date_query_does_not_sink_the_rest():
    dates = iter([None, ST_7408])  # data on the SECOND date: every date is fetched
    t = FakeTransport()
    t.showtimes = None

    def route(url, params, session):
        return next(dates) if url == api.SHOWTIMES_URL else t(url, params, session)

    with fake(route):
        rc, out = run_main("showtimes", "--location", "7408",
                           "--dates", "2026-09-11,2027-03-29", "--json")
    assert rc == cli.EXIT_OK, out
    assert len(json.loads(out)) == len(api.flatten_showtimes(ST_7408))


@offline
def test_experience_tokens_match_the_real_labels():
    """B2: `ultraavx`, `dolby atmos` and `imax` used to return nothing (the
    server wants undocumented lowercase codes), `70MM` was case-sensitive, and
    `"3d, vip"` dropped vip. Each spelling must find every session carrying
    the label, counted independently from the raw fixture."""
    def n(data, tokens):
        return len(api.flatten_showtimes(api.filter_showtimes_by_experience(data, tokens)))

    cases = [
        (ST_7130, "ultraavx", "UltraAVX"), (ST_7130, "UltraAVX", "UltraAVX"),
        (ST_7130, "avx", "UltraAVX"),
        (ST_7130, "dolby atmos", "Dolby Atmos"), (ST_7130, "DOLBY ATMOS", "Dolby Atmos"),
        (ST_7130, "atmos", "Dolby Atmos"),
        (ST_7130, "imax", "IMAX"), (ST_7130, "IMAX", "IMAX"),
        (ST_7130, "vip", "VIP 19+"), (ST_7130, "VIP 19+", "VIP 19+"),
        (ST_7130, "dbox", "D-BOX"), (ST_7130, "D-BOX", "D-BOX"),
        (ST_7408, "70MM", "70mm"), (ST_7408, "70mm", "70mm"),
        (ST_7408, "screenx", "ScreenX"), (ST_7408, "3D", "3D"),
        (ST_7130, "standard", "Regular"),
    ]
    for data, token, label in cases:
        want = count_sessions(data, label)
        assert want > 0, f"fixture has no {label} sessions"
        got = n(data, token)
        note(f"{token!r:15} -> {got} (want {want})")
        assert got == want, f"--experiences {token!r} found {got}, expected {want}"

    # A list is OR, and whitespace after the comma must not drop a token.
    union = n(ST_7130, "3d, vip")
    assert union == count_sessions(ST_7130, "3D") + count_sessions(ST_7130, "VIP 19+")
    assert n(ST_7130, " 3d ,vip ") == union
    # A filter must never *add* sessions, and no filter is the identity.
    assert n(ST_7130, None) == len(api.flatten_showtimes(ST_7130))
    assert n(ST_7130, "regular") == count_sessions(ST_7130, "Regular")

    # The library entry point filters client-side too, and never sends the
    # token to the server (whose filter silently drops `ultraavx`).
    t = FakeTransport(showtimes=ST_7130)
    with fake(t):
        got = api.fetch_showtimes(7130, "2026-09-11", experiences="ultraavx")
    assert "experiences" not in t.calls[0][1], t.calls
    assert len(api.flatten_showtimes(got)) == count_sessions(ST_7130, "UltraAVX")


@offline
def test_unknown_experience_is_refused_but_a_real_absent_one_is_not():
    """A typo must exit 2, not read as "no screenings" forever in a watch. But
    a real format that isn't playing (70mm at Yonge-Dundas) is a clean 0."""
    for bad in ("imx", "70", "dolby"):
        for data in (ST_7130, None):  # also on an empty (204) date
            try:
                api.filter_showtimes_by_experience(data, bad)
            except api.UsageError:
                pass
            else:
                raise AssertionError(f"--experiences {bad!r} was accepted")
    # Something given that normalises to nothing must not mean "no filter".
    for empty in ("19+", ",", " ", " , "):
        try:
            api.filter_showtimes_by_experience(ST_7130, empty)
        except api.UsageError:
            pass
        else:
            raise AssertionError(f"--experiences {empty!r} silently dropped the filter")
    with fake(FakeTransport(showtimes=ST_7130)):
        assert both_modes("showtimes", "--location", "7130", "--date", "2026-09-11",
                          "--experiences", "19+") == cli.EXIT_USAGE
        assert both_modes("theatres", "--film", "37617", "--experiences", ",") == cli.EXIT_USAGE
    assert api.filter_showtimes_by_experience(ST_7130, "70mm") == []
    assert api.filter_showtimes_by_experience(None, "70mm") is None
    for label in api.KNOWN_EXPERIENCES:
        api.filter_showtimes_by_experience(None, label)  # must not raise


@offline
def test_theatres_filter_sends_only_verified_server_codes():
    """The theatres endpoint can only filter server-side, and its codes are
    lowercase and partly renamed (UltraAVX -> avx). Untranslatable formats
    must be refused, not sent and silently answered with []."""
    assert api.theatre_experience_codes("IMAX, 70MM") == "imax,70mm"
    assert api.theatre_experience_codes("UltraAVX") == "avx"
    assert api.theatre_experience_codes("VIP 19+,vip") == "vip"
    assert api.theatre_experience_codes(None) is None
    for bad in ("dolby atmos", "clubhouse", "imx"):
        try:
            api.theatre_experience_codes(bad)
        except api.UsageError:
            pass
        else:
            raise AssertionError(f"theatres accepted {bad!r}")
    # And fetch_theatres must actually go through the translation.
    t = FakeTransport()
    with fake(t):
        api.fetch_theatres(37617, experiences="UltraAVX, IMAX")
        assert t.calls[0][1]["experiences"] == "avx,imax", t.calls
        rc, _ = run_main("theatres", "--film", "37617", "--experiences", "dolby atmos")
        assert rc == cli.EXIT_USAGE and len(t.calls) == 1, "atmos was sent to the server"


@offline
def test_dates_parse_or_are_refused_locally():
    assert api.format_api_date("2026-09-11") == "9/11/2026"
    assert api.format_api_date("09/05/2026") == "9/5/2026"
    assert api.format_api_date(date(2026, 1, 2)) == "1/2/2026"
    for bad in ("garbage", "2026-13-01", "31/12/2026", ""):
        try:
            api.format_api_date(bad)
        except api.UsageError:
            pass
        else:
            raise AssertionError(f"date {bad!r} was accepted")


@offline
def test_seat_summary_and_filters_on_a_real_auditorium():
    s = api.summarize_seats(LAYOUT, AVAIL)
    assert (s["totalSeats"], s["available"]) == (77, 8), s
    assert s["byStatus"] == {"Occupied": 65, "Available": 8, "Broken": 4}
    assert not s["isSoldOut"]
    open_rows = {r["label"]: r["available"] for r in s["rows"] if r["available"]}
    assert open_rows == {"AA": 4, "A": 4}, open_rows

    # A seat missing from the availability map is "Unknown", never open.
    first = next(iter(AVAIL["seatAvailabilities"]))
    holey = {"seatAvailabilities": {k: v for k, v in AVAIL["seatAvailabilities"].items()
                                    if k != first}}
    hs = api.summarize_seats(LAYOUT, holey)
    assert hs["byStatus"].get("Unknown") == 1, hs["byStatus"]
    assert hs["available"] == s["available"] - (AVAIL["seatAvailabilities"][first] == "Available")

    # --all puts taken seats into `matched` (row G: 9 seats, none open).
    with fake(FakeTransport()):
        rc, out = run_main("seats", "--theatre", "7130", "--showtime", "446546",
                           "--rows", "G", "--all", "--json")
    m = json.loads(out)["matched"]
    assert rc == cli.EXIT_NONE and len(m) == 9 and all(x["status"] != "Available" for x in m), m

    assert api.filter_seats(LAYOUT, AVAIL, rows=["G", "H"]) == []
    g_all = api.filter_seats(LAYOUT, AVAIL, rows=["g"], available_only=False)
    assert len(g_all) == 9 and all(m["status"] != "Available" for m in g_all)
    aa = api.filter_seats(LAYOUT, AVAIL, rows=["AA", "A"])
    assert len(aa) == 8 and all(m["status"] == "Available" for m in aa)
    # --middle keeps the geometric centre third of each row: row H has 13.
    h_mid = api.filter_seats(LAYOUT, AVAIL, rows=["H"], middle=True, available_only=False)
    cols = sorted(x["column"] for x in api.filter_seats(LAYOUT, AVAIL, rows=["H"],
                                                         available_only=False))
    assert [m["column"] for m in h_mid] == cols[4:8], h_mid


@offline
def test_exit_codes_follow_the_repo_convention():
    """B3: 0 found, 1 nothing, 2 usage/lookup, 3 network — identical under --json.
    Every error used to be 1 and an empty result 0, so a watch couldn't tell
    "sold out" from "outage"."""
    E = cli
    st = ["showtimes", "--location", "7130", "--date", "2026-09-11"]
    with fake(FakeTransport(showtimes=ST_7130)):
        assert both_modes(*st) == E.EXIT_OK
        assert both_modes(*st, "--experiences", "70mm") == E.EXIT_NONE
        assert both_modes(*st, "--experiences", "imx") == E.EXIT_USAGE
        assert both_modes("showtimes", "--location", "7130", "--date", "nope") == E.EXIT_USAGE
    with fake(FakeTransport(showtimes=None)):
        assert both_modes(*st) == E.EXIT_NONE               # 204: nothing on
        assert both_modes("showtimes", "--location", "999999",
                          "--date", "2026-09-11") == E.EXIT_USAGE  # 204: no such theatre

    seats = ["seats", "--theatre", "7130", "--showtime", "446546"]
    with fake(FakeTransport()):
        assert both_modes(*seats) == E.EXIT_OK
        assert both_modes(*seats, "--rows", "AA") == E.EXIT_OK
        assert both_modes(*seats, "--rows", "G,H") == E.EXIT_NONE
        assert both_modes(*seats, "--rows", "G,H", "--all") == E.EXIT_NONE
        assert both_modes(*seats, "--rows", "Z") == E.EXIT_USAGE
    sold = {"seatAvailabilities": {k: "Occupied" for k in AVAIL["seatAvailabilities"]}}
    with fake(FakeTransport(avail=sold)):
        assert both_modes(*seats) == E.EXIT_NONE
    with fake(FakeTransport(avail={"seatAvailabilities": {}, "isPostShowtime": True})):
        assert both_modes(*seats) == E.EXIT_USAGE

    def http(status):
        return requests.HTTPError(f"{status}", response=_response(status, b'"x"'))

    for err, want in ((http(404), E.EXIT_USAGE), (http(400), E.EXIT_USAGE),
                      (http(500), E.EXIT_NET), (http(401), E.EXIT_NET),
                      (requests.ConnectionError("dns"), E.EXIT_NET)):
        with fake(FakeTransport(error=err)):
            assert both_modes(*seats) == want, f"{err!r} should exit {want}"
            assert both_modes("movies") == want
    assert run_main()[0] == E.EXIT_USAGE


@offline
def test_stale_key_retry_survives_a_read_only_filesystem():
    """B5: on a 401 the cached key is deleted; on a read-only filesystem that
    unlink raised and took the retry down with it."""
    class ReadOnly:
        def exists(self):
            return False

        def unlink(self, missing_ok=False):
            raise OSError(30, "Read-only file system")

    class Session:
        def __init__(self):
            self.replies = [_response(401, b'"denied"'), _response(200, ST_7130)]

        def get(self, *a, **k):
            return self.replies.pop(0)

    real_cache, real_key = api.KEY_CACHE, api.get_subscription_key
    api.KEY_CACHE = ReadOnly()
    api.get_subscription_key = lambda s, force_refresh=False: "0" * 32
    try:
        assert api._get(api.SHOWTIMES_URL, {}, Session()) == ST_7130
    finally:
        api.KEY_CACHE, api.get_subscription_key = real_cache, real_key


@offline
def test_an_empty_seat_answer_is_an_api_error_not_sold_out():
    """An empty layout or availability map left every seat "Unknown", which
    read as sold out (exit 1) — a watch would wait forever on a broken answer."""
    seats = ["seats", "--theatre", "7130", "--showtime", "446546"]
    for kw in ({"avail": None}, {"avail": {"seatAvailabilities": {}}},
               {"avail": {"isSoldOut": False}},
               {"layout": None}, {"layout": {"standardSeats": {"rows": []}}}):
        with fake(FakeTransport(**kw)):
            assert both_modes(*seats) == cli.EXIT_NET, f"{kw} should exit 3"
            assert both_modes(*seats, "--rows", "G") in (cli.EXIT_NET, cli.EXIT_USAGE)


@offline
def test_unexpected_failures_exit_3_never_1():
    """Response-shape drift raised straight out of main(), which Python turns
    into exit 1 — "keep waiting" — in a watch loop."""
    for drift in ([{"theatre": "x", "dates": "not-a-list"}], {"unexpected": "object"}):
        with fake(FakeTransport(showtimes=drift)):
            rc, out = run_main("showtimes", "--location", "7130", "--date", "2026-09-11")
        assert rc == cli.EXIT_NET, f"shape drift {drift!r} exited {rc}: {out[:200]}"
    with fake(FakeTransport(layout={"standardSeats": "drifted"})):
        assert both_modes("seats", "--theatre", "7130", "--showtime", "446546") == cli.EXIT_NET


@offline
def test_an_unreadable_key_cache_falls_back_instead_of_crashing():
    class Unreadable:
        def exists(self):
            return True

        def read_text(self):
            raise PermissionError(13, "Permission denied")

    real_cache, real_scrape = api.KEY_CACHE, api._scrape_key
    api.KEY_CACHE, api._scrape_key = Unreadable(), lambda s: None
    try:
        assert api.get_subscription_key(None) == api.DEFAULT_KEY
    finally:
        api.KEY_CACHE, api._scrape_key = real_cache, real_scrape


@offline
def test_a_new_cineplex_label_works_on_days_it_is_playing():
    """The token check used to run before the fetch, against the built-in list
    only — so a label Cineplex adds later ("IMAX Laser") was refused with
    exit 2 even on a day it was playing, stopping a watch for good."""
    new = copy.deepcopy(ST_7408)
    for d in new[0]["dates"]:
        for m in d["movies"]:
            for e in m["experiences"]:
                e["experienceTypes"] = ["IMAX Laser" if t == "70mm" else t
                                        for t in e["experienceTypes"]]
    want = count_sessions(new, "IMAX Laser")
    assert want > 0
    st = ["showtimes", "--location", "7408", "--experiences", "imax laser"]
    with fake(FakeTransport(showtimes=new)):
        rc, out = run_main(*st, "--date", "2026-09-11", "--json")
    assert rc == cli.EXIT_OK, out
    assert len(json.loads(out)) == want

    # Across dates: seen on the second date is enough, even if the first is empty.
    days = iter([None, new])

    def route(url, params, session):
        return next(days) if url == api.SHOWTIMES_URL else FakeTransport()(url, params, session)

    with fake(route):
        rc, out = run_main(*st, "--dates", "2026-09-11,2026-09-12", "--json")
    assert rc == cli.EXIT_OK and len(json.loads(out)) == want, out

    # Unknown to both is still refused; known-but-not-playing is a clean 1.
    with fake(FakeTransport(showtimes=ST_7408)):
        assert both_modes(*st, "--date", "2026-09-11") == cli.EXIT_USAGE
        assert both_modes("showtimes", "--location", "7408", "--date", "2026-09-11",
                          "--experiences", "4dx") == cli.EXIT_NONE


@offline
def test_cli_runs_by_absolute_path_from_a_foreign_directory():
    proc = subprocess.run([sys.executable, str(_HERE / "cineplex_showtimes.py"), "--help"],
                          capture_output=True, text=True, cwd=tempfile.gettempdir(),
                          timeout=60)
    assert proc.returncode == 0, proc.stderr


# =========================================================== network checks


def live(*argv) -> tuple[int, object]:
    proc = subprocess.run([sys.executable, str(_HERE / "cineplex_showtimes.py"), *argv, "--json"],
                          capture_output=True, text=True, cwd=tempfile.gettempdir(),
                          timeout=120)
    if proc.returncode == cli.EXIT_NET:
        raise AssertionError(f"{HOST} unreachable or erroring: {proc.stderr.strip()[:300]}")
    try:
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        raise AssertionError(f"{' '.join(argv)} (from {HOST}) emitted non-JSON, "
                             f"exit {proc.returncode}: {proc.stdout[:200]}")


def _first_day_with_showtimes(location):
    for offset in range(1, 5):
        day = (date.today() + timedelta(days=offset)).isoformat()
        rc, recs = live("showtimes", "--location", str(location), "--date", day)
        if rc == cli.EXIT_OK:
            return day, recs
    raise AssertionError(f"{HOST}: theatre {location} had no showtimes for 4 days")


@network
def test_live_name_resolution():
    rc, recs = live("locations", "--name", "Vaughan")
    assert rc == 0 and any(r["theatreId"] == 7408 for r in recs), (
        f"{HOST}: Vaughan no longer resolves to 7408: {recs}")
    rc, recs = live("movies")
    assert rc == 0 and len(recs) > 10, f"{HOST}: film catalogue looks empty"


@network
def test_live_showtimes_and_experience_labels_have_not_drifted():
    """If Cineplex adds a format, `--experiences` would refuse it as unknown on
    days it isn't playing. Catch that here and add it to KNOWN_EXPERIENCES."""
    known = {api.normalize_experience(e) for e in api.KNOWN_EXPERIENCES}
    day, recs = _first_day_with_showtimes(7408)
    labels = {t for r in recs for t in r["experience"]}
    note(f"{day}: {len(recs)} sessions, labels {sorted(labels)}")
    new = [t for t in labels if api.normalize_experience(t) not in known]
    assert not new, f"{HOST} now sends {new}; add to KNOWN_EXPERIENCES in cineplex_api.py"
    assert all(r["sessionId"] for r in recs)


@network
def test_live_empty_date_is_exit_1_not_a_network_error():
    far = (date.today() + timedelta(days=400)).isoformat()
    rc, recs = live("showtimes", "--location", "7408", "--date", far)
    assert rc == cli.EXIT_NONE and recs == [], f"{HOST}: far-future date gave exit {rc}"


@network
def test_live_seat_map_for_a_real_session():
    day, recs = _first_day_with_showtimes(7408)
    rec = max(recs, key=lambda r: r["seatsRemaining"] or 0)
    rc, s = live("seats", "--theatre", "7408", "--showtime", str(rec["sessionId"]))
    note(f"{rec['movie']} {day} {rec['time']}: {s['available']}/{s['totalSeats']} open")
    assert rc in (0, 1) and s["totalSeats"] > 0, f"{HOST}: empty seat layout"
    assert rc == (0 if s["available"] else 1)
    rc, _ = live("seats", "--theatre", "7408", "--showtime", "1")
    assert rc == cli.EXIT_USAGE, f"{HOST}: unknown showtime gave exit {rc}, not 2"


# ---------------------------------------------------------------- runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-check for the cineplex-showtimes skill.")
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

    print(f"cineplex-showtimes self-check — {len(checks)} checks"
          + (" (offline only)" if args.offline else "") + "\n")

    failures: list[tuple[str, str]] = []
    started = time.time()
    for group, name, fn in checks:
        _DETAILS.clear()
        label = name.removeprefix("test_").replace("_", " ")
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

    print(f"\n{len(checks) - len(failures)}/{len(checks)} passed in {time.time() - started:.1f}s")
    if failures:
        print("\nFAILURES")
        for name, err in failures:
            print(f"\n  {name}")
            for line in err.splitlines():
                print(f"    {line}")
        print(f"\nFAIL — {len(failures)} check(s) failed")
        return 1
    print("PASS (offline) — re-run without --offline to check the live API"
          if args.offline else "PASS — the skill is working")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
