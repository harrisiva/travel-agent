#!/usr/bin/env python3
"""Self-check for the gmaps skill.

Two groups. ``--offline`` runs only the first:

    [offline]  pure logic against real captured responses — no sockets
    [network]  live calls; a failure names the host, so "the tenant is down"
               is distinguishable from "the skill is broken"

The bugs worth catching here are not crashes. They are the answers that look
plausible and are wrong: a shifted positional index, a place with no published
hours reported as closed, a walking route that is really a drive, a parser
blowing up and exiting 1 so a watch loop waits forever.

    python3 test_gmaps.py [--offline]
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from gmaps import cli, fanout, hours as hours_mod, http as http_mod, pb, query as query_mod
from gmaps import render, routing
from gmaps.errors import (EMPTY, FOUND, GmapsError, NETWORK, NetworkError,
                          ParseError as ContractParseError, USAGE, UsageError)
from gmaps.fanout import check_budget, fan_out
from gmaps.fields import PLACE_FIELDS, dig, extract, project
from gmaps.hours import (_clock, _day_records, _find_place, describe,
                         fetch_week, is_open_at, parse_days, parse_when,
                         week_index)
from gmaps.http import new_session
from gmaps import model as model_mod
from gmaps.model import CLOSED, OPEN, UNKNOWN, haversine_km, place_from_blob
from gmaps.parse import ParseError, decode, result_blobs, search_center
from gmaps.places import geocode, search
from gmaps.query import QueryResult, QuerySpec, _filter_open_at, _sort
from gmaps.routing import MODES, routes

FIXTURES = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fixtures")

_failures: list[str] = []
_ran = 0


def check(group: str, name: str, fn) -> None:
    global _ran
    _ran += 1
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a self-check reports, it does not raise
        _failures.append(f"[{group}] {name}: {type(exc).__name__}: {exc}")
        print(f"  FAIL [{group}] {name}: {exc}")
    else:
        print(f"  ok   [{group}] {name}")


def fixture(filename: str) -> str:
    with open(os.path.join(FIXTURES, filename), encoding="utf-8") as fh:
        return fh.read()


def assert_(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def raises(exc_type, fn, what: str):
    """Run `fn`, requiring `exc_type`. Returns the exception for inspection."""
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{what}: expected {exc_type.__name__}, got "
            f"{type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"{what}: expected {exc_type.__name__}, nothing raised")


class FakeSession:
    """A Session stand-in: same `get_text` surface, no sockets.

    `reply` is called with the url and returns a body, or raises to simulate a
    transport failure for one item.
    """

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[str] = []

    def get_text(self, url, params=None, raw_suffix="") -> str:
        full = url + ("?" + raw_suffix if raw_suffix else "")
        self.calls.append(full)
        return self._reply(full)


@contextlib.contextmanager
def patched(module, name, value):
    """Swap one module attribute for the duration of a test."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Drive cli.main and capture stdout — exit code plus what a caller sees."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(argv)
    return code, buffer.getvalue()


def blank_week() -> list[dict]:
    return [{"day": d, "display": [], "spans": [], "closed": False}
            for d in hours_mod.DAY_NAMES]


# ---------------------------------------------------------------- [offline]

def t_decode_multichunk():
    """The envelope can split one payload across several `{"c":..,"d":..}` chunks.

    A single `json.loads` of the whole body succeeds on the common one-chunk
    response and raises `Extra data` the moment Google splits it — the classic
    bug that passes every test until someone searches a busy city. So the
    split-chunk fixture is the one that matters here: it is the real
    single-result payload cut in half mid-token, so no chunk parses alone and
    only concatenation gives the right answer.
    """
    split = fixture("search_multichunk_split.txt")
    assert_(split.count('{"c"') > 1, "the multichunk fixture is no longer split")
    raises(ValueError, lambda: json.loads(split),
           "a naive json.loads of a split body")  # what decode must not do
    assert_(decode(split) == decode(fixture("search_single_toronto.txt")),
            "a split payload decoded to something other than the whole one")

    data = decode(fixture("search_page1_toronto.txt"))
    assert_(isinstance(data, list) and len(data) > 1,
            "decoded payload is not a populated list")
    assert_(len(result_blobs(decode(split))) == 1,
            "the reassembled payload lost its result")


def t_decode_rejects_garbage():
    for bad in ("", "not json at all", '{"c":0}', '{"c":0,"d":"no prefix"}'):
        raises(ParseError, lambda bad=bad: decode(bad), f"decode({bad!r})")


def t_parse_error_is_part_of_the_error_contract():
    """`decode` must raise the ParseError from `errors.py`, not a lookalike.

    `errors.ParseError` is a `GmapsError` carrying `exit_code`. Anything that
    raises a *different* class of the same name escapes both `fan_out`'s
    `except GmapsError` (which degrades one item cleanly) and `cli.main`'s
    typed branch, and is reported to the user as an "unexpected" failure. The
    exit code happens to survive via the catch-all; the contract does not.
    """
    exc = raises(Exception, lambda: decode("garbage"), "decode('garbage')")
    assert_(isinstance(exc, ContractParseError),
            f"decode raised {type(exc).__module__}.{type(exc).__name__}, "
            "not gmaps.errors.ParseError")
    assert_(isinstance(exc, GmapsError), "ParseError is not a GmapsError")
    assert_(exc.exit_code == NETWORK, "ParseError does not carry exit code 3")


def t_slot_offset_varies():
    """Results start at slot 1 in a paged search and slot 0 in a single lookup.

    This is the regression that motivated scanning every slot: indexing a fixed
    position drops a result on one shape and reads the header on the other.
    """
    many = result_blobs(decode(fixture("search_page1_toronto.txt")))
    one = result_blobs(decode(fixture("search_single_toronto.txt")))
    assert_(len(many) == 20, f"expected 20 blobs on a full page, got {len(many)}")
    assert_(len(one) == 1, f"expected 1 blob on a single lookup, got {len(one)}")
    # Both shapes must yield the same place for the same listing — proof the
    # scan, not an offset, is what found it.
    assert_(place_from_blob(one[0])["name"] == "Black+Blue Toronto",
            "the single-result fixture did not parse to its known place")


#: Minimum number of the 26 captured place blobs each field must populate.
#: A shifted index reads a neighbouring slot, which almost never validates, so
#: coverage collapsing to ~0 is the signature this catches. Thresholds are the
#: measured truth minus a little slack — not aspirations.
FIELD_COVERAGE = {
    "name": 26, "lat": 26, "lng": 26, "address": 26, "ftid": 26,
    "place_id": 26, "rating": 26, "categories": 26, "timezone": 26,
    "website": 22, "phone": 24, "status_detail": 24, "hours_today": 24,
    "editorial": 8, "neighbourhood": 26, "city_region": 26,
}

#: Values each validator must reject. These are the *plausible* wrong values —
#: mostly a neighbouring field's real content — because that is what a shifted
#: index actually delivers. A rating of 4.7 is plausible; "Old Toronto" is not.
FIELD_REJECTS = {
    "name": (None, 42, "", "   ", []),
    "lat": (None, "43.6481", 120.0, [43.6]),
    "lng": (None, "-79.38", 200.0, {}),
    "address": (None, 5, ""),
    "ftid": (None, "ChIJ243-C381K4gRHMKWCGU86iM", "0x882b357f0bfe8ddb"),
    "place_id": (None, "0x882b357f0bfe8ddb:0x23ea3c650896c21c", 7),
    "rating": (None, "Old Toronto", 7.5, -1),
    "categories": (None, "Restaurant", ["Restaurant", 3]),
    "timezone": (None, "Toronto", 4.7),
    "website": (None, 1, ""),
    "phone": (None, [], ""),
    "status_detail": (None, {}, ""),
    "hours_today": (None, 0, ""),
    "editorial": (None, 3.3, ""),
    "neighbourhood": (None, ["Old Toronto"], ""),
    "city_region": (None, 12, ""),
}


def t_place_fields_table():
    """One test for the whole index map, against every captured payload.

    Per-field tests rot: someone adds a field to `PLACE_FIELDS` and no test
    covers it. This walks the table itself, so a new field is covered the
    moment it is declared — and an unlisted field fails loudly rather than
    silently going unchecked.
    """
    blobs = []
    for name in ("search_page1_toronto.txt", "search_single_toronto.txt",
                 "search_tokyo_missing_hours.txt"):
        blobs.extend(result_blobs(decode(fixture(name))))
    assert_(len(blobs) == 26, f"fixtures yielded {len(blobs)} blobs, expected 26")

    declared = {f.name for f in PLACE_FIELDS}
    assert_(declared == set(FIELD_COVERAGE) == set(FIELD_REJECTS),
            "PLACE_FIELDS and this test's tables disagree: "
            f"{declared ^ set(FIELD_COVERAGE) ^ set(FIELD_REJECTS)}")

    for field in PLACE_FIELDS:
        hits = sum(1 for b in blobs if field.valid(dig(b, field.path)))
        floor = 26 if field.required else FIELD_COVERAGE[field.name]
        assert_(hits >= floor,
                f"{field.name} at {field.path} validated on {hits}/26 blobs, "
                f"expected at least {floor} — the index has probably shifted")


def t_field_validators_reject_wrong_types():
    """Validation is what turns a shifted index into *no* answer, not a wrong
    one — so each validator has to actually refuse a neighbour's value."""
    by_name = {f.name: f for f in PLACE_FIELDS}
    for name, bad_values in FIELD_REJECTS.items():
        for bad in bad_values:
            assert_(not by_name[name].valid(bad),
                    f"{name} validator accepted {bad!r}")


def t_extract_requires_required_fields():
    """A blob missing a required field is not a place, however much else fits."""
    blob = result_blobs(decode(fixture("search_single_toronto.txt")))[0]
    assert_(extract(blob) is not None, "known-good blob failed extraction")
    for field in PLACE_FIELDS:
        if not field.required:
            continue
        broken = list(blob)
        broken[field.path[0]] = None
        assert_(extract(broken) is None,
                f"a blob with no {field.name} still extracted as a place")


def t_bad_blob_rejected():
    """A shifted index must yield nothing, not a plausible wrong record."""
    for bad in ([], ["x"] * 12, [None] * 300):
        assert_(place_from_blob(bad) is None,
                f"garbage blob {bad[:2]} produced a place")
    shifted = [None] * 300
    shifted[11] = "Somewhere"
    shifted[9] = [None, None, 999.0, 999.0]  # impossible coordinates
    assert_(place_from_blob(shifted) is None,
            "out-of-range coordinates were accepted")


def t_status_is_three_states():
    """open / closed / unknown, and absence resolves to unknown.

    The failure to guard against is not a crash but a silent demotion: a place
    Google says nothing about being reported "closed" sends someone away from
    an open restaurant.
    """
    def blob_with(raw):
        blob = [None] * 300
        blob[11] = "Somewhere"
        blob[9] = [None, None, 43.65, -79.38]
        node = [None] * 9
        node[8] = [raw]
        blob[203] = [None, node]
        return blob

    cases = {
        "Open": OPEN, "Open ⋅ Closes 12 a.m.": OPEN,
        "Closed": CLOSED, "Closed ⋅ Opens 11 AM": CLOSED,
        "Temporarily closed": CLOSED, "Permanently closed": CLOSED,
    }
    for raw, want in cases.items():
        got = place_from_blob(blob_with(raw))["status"]
        assert_(got == want, f"{raw!r} read as {got!r}, expected {want!r}")

    for absent in (None, [], 0):
        got = place_from_blob(blob_with(absent))["status"]
        assert_(got == UNKNOWN,
                f"a place with status {absent!r} read as {got!r}, not unknown")
    assert_(len({OPEN, CLOSED, UNKNOWN}) == 3, "the three states are not distinct")


def t_missing_hours_is_unknown_not_closed():
    """The silent-failure case. Verified in Tokyo: some places publish no hours.

    Calling those "closed" is a false negative, so they must resolve to
    'unknown' and be counted, never quietly folded into the closed pile.
    """
    blobs = result_blobs(decode(fixture("search_tokyo_missing_hours.txt")))
    places = [p for p in (place_from_blob(b) for b in blobs) if p]
    assert_(places, "no places parsed from the Tokyo fixture")
    assert_(all(p["timezone"] == "Asia/Tokyo" for p in places),
            "Tokyo fixture did not resolve to Asia/Tokyo — status is not place-local")
    unknown = [p for p in places if p["status"] == UNKNOWN]
    assert_(unknown, "the Tokyo fixture is supposed to contain a place with no hours")
    assert_(all(p["status"] != CLOSED for p in unknown),
            "unknown hours reported as closed")


def t_open_now_filter_counts_unknowns():
    """--open-now keeps only OPEN, and counts UNKNOWN rather than hiding it.

    This used to test `places.filter_open`, which nothing in the package calls:
    `query.run` does the filtering inline, so the test passed while the real
    path went uncovered. A place with no published hours must not be quietly
    dropped — `hours_unknown` is what lets a caller say "4 open, plus 3 whose
    hours aren't listed" instead of implying those three are shut.
    """
    def rows():
        return [{"name": "o", "status": OPEN, "business_status": "operating",
                 "lat": 43.6, "lng": -79.4, "rating": 4.0},
                {"name": "c", "status": CLOSED, "business_status": "operating",
                 "lat": 43.6, "lng": -79.4, "rating": 4.0},
                {"name": "u", "status": UNKNOWN, "business_status": "operating",
                 "lat": 43.6, "lng": -79.4, "rating": 4.0}]

    with patched(query_mod, "search", lambda *a, **k: rows()), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")):
        strict = query_mod.run(None, QuerySpec(near="43.6,-79.4", open_now=True))
        loose = query_mod.run(None, QuerySpec(near="43.6,-79.4", open_now=False))

    assert_([p["name"] for p in strict.places] == ["o"],
            f"--open-now kept {[p['name'] for p in strict.places]}, expected ['o']")
    assert_(strict.hours_unknown == 1,
            f"hours_unknown was {strict.hours_unknown}, expected 1 — an unknown "
            "was dropped without being counted")
    assert_(len(loose.places) == 3, "the unfiltered call dropped rows")
    assert_(loose.hours_unknown == 1, "unknowns went uncounted when unfiltered")


def t_search_center():
    c = search_center(decode(fixture("search_page1_toronto.txt")))
    assert_(c is not None, "resolved centre not found in a real search response")
    assert_(43.5 < c[0] < 43.8 and -79.6 < c[1] < -79.2,
            f"resolved centre is not Toronto: {c}")


def t_pb_shape():
    """The fixed tokens are load-bearing: omit 6e2 or 20e3 and Google answers
    with an empty result list rather than an error."""
    s = pb.search_pb(43.6532, -79.3832, 10000, 20, 20)
    for token in ("!2d-79.3832", "!3d43.6532", "!7i20", "!8i20", "!6e2", "!20e3"):
        assert_(token in s, f"pb is missing the required token {token}")


def t_haversine():
    d = haversine_km((43.6532, -79.3832), (45.5017, -73.5673))  # Toronto -> Montreal
    assert_(500 < d < 520, f"Toronto->Montreal came out as {d:.1f} km, expected ~505")


def _embed_days(filename):
    """Parse a captured maps/embed response the way fetch_week does."""
    body = fixture(filename)
    marker = body.find("initEmbed(")
    assert_(marker != -1, f"{filename} has no initEmbed payload")
    data, _ = json.JSONDecoder().raw_decode(body, body.index("[", marker))
    place = _find_place(data)
    assert_(place is not None, f"{filename}: place record not found")
    records = _day_records(place)
    assert_(records is not None, f"{filename}: no seven-day block")
    return parse_days(records)


def t_embed_full_week():
    """The whole point: seven days from Google, matching known ground truth.

    Richmond Station runs split service — a day with TWO spans. A parser that
    keeps only the first interval loses dinner and reads as closed at 7pm.
    """
    days = _embed_days("embed_richmond_station.html")
    assert_(len(days) == 7, f"expected 7 days, got {len(days)}")
    assert_(days[0]["day"] == "Monday", f"not Monday-first: {days[0]['day']}")
    assert_(days[0]["spans"] == [(690, 870), (990, 1350)],
            f"Monday spans wrong: {days[0]['spans']}")
    assert_(days[5]["spans"] == [(990, 1350)], f"Saturday wrong: {days[5]['spans']}")
    assert_(is_open_at(days, 0, 13 * 60) is True, "Monday 1pm should be open")
    assert_(is_open_at(days, 0, 15 * 60) is False,
            "Monday 3pm falls in the afternoon gap and should be closed")
    lines = describe(days)
    assert_(len(lines) == 7 and lines[0].startswith("Monday: "),
            f"describe() is not one Monday-first line per day: {lines[:1]}")


def t_embed_closed_days():
    """A closed day carries the string 'Closed' and NO interval pair.

    Alo also gives the ragged-clock case in real data: '5 p.m.-12 a.m.' encodes
    as [[17], []], whose empty end means midnight (1440), not 0.
    """
    days = _embed_days("embed_alo_closed_days.html")
    closed = [d for d in days if d["closed"]]
    assert_(len(closed) == 2, f"Alo should have 2 closed days, got {len(closed)}")
    assert_(all(not d["spans"] for d in closed), "a closed day produced spans")
    assert_(days[1]["spans"] == [(1020, 1440)],
            f"Tuesday 5pm-midnight parsed as {days[1]['spans']}")
    assert_(is_open_at(days, 1, 23 * 60) is True, "Tuesday 11pm should be open")
    assert_(is_open_at(days, 0, 20 * 60) is False, "Monday is closed all day")


def t_embed_24h_and_midnight():
    """Katz's is open 24 hours on one day; midnight ends must not read as zero."""
    days = _embed_days("embed_katzs_24h.html")
    allday = [d for d in days if (0, 1440) in d["spans"]]
    assert_(allday, "no 24-hour day found in the Katz's fixture")
    assert_(is_open_at(days, 5, 3 * 60) is True, "3am on a 24-hour day is open")
    for d in days:
        for start, end in d["spans"]:
            assert_(end > 0, f"{d['day']}: end of {end} — empty list read as zero")


def t_clock_ragged_pairs():
    """Google omits obvious components, and the omission means midnight.

    Reading an empty end as 0 turns '5pm-midnight' into a negative span, which
    then reads as closed — a false negative that looks entirely plausible.
    """
    assert_(_clock([], end=False) == 0, "empty start should be midnight (0)")
    assert_(_clock([], end=True) == 1440, "empty end should be midnight (1440)")
    assert_(_clock([17], end=False) == 1020, "[17] should be 17:00")
    assert_(_clock([23, 30], end=True) == 1410, "[23,30] should be 23:30")
    assert_(_clock(["x"], end=True) is None, "non-int should be rejected")
    assert_(_clock([99], end=True) is None, "out-of-range hour should be rejected")
    assert_(_clock("17:00", end=True) is None, "a string clock should be rejected")


def t_week_index_crosses_midnight():
    """A span ending at or before its start spills onto the next day."""
    days = blank_week()
    days[4]["spans"] = [(1020, 120)]  # Friday 17:00 -> 02:00
    week = week_index(days)
    assert_((1020, 1440) in week[4], f"Friday evening missing: {week[4]}")
    assert_((0, 120) in week[5], f"Saturday 1am tail missing: {week[5]}")
    assert_(is_open_at(days, 5, 60) is True, "1am Saturday should be open")
    assert_(is_open_at(days, 5, 600) is False, "10am Saturday should be closed")
    assert_(is_open_at(days, 4, 1030) is True, "Friday 17:10 should be open")

    # Sunday night must wrap to Monday morning, not fall off the end of the week.
    sunday = blank_week()
    sunday[6]["spans"] = [(1320, 180)]  # Sunday 22:00 -> 03:00
    assert_((0, 180) in week_index(sunday)[0], "Sunday's tail did not wrap to Monday")


def t_open_at_unknown_is_none():
    """Three states again: None (no published hours) is not False (shut)."""
    assert_(is_open_at([], 0, 600) is None, "no days should give None, not False")
    closed = blank_week()
    assert_(is_open_at(closed, 0, 600) is False,
            "a place with a published, empty week is closed, not unknown")
    assert_(describe([]) is None, "describe([]) should be None, not an empty week")


def t_parse_when_accepted_forms():
    """Every form documented on --open-at, in the place's own local time."""
    cases = {
        "Fri 20:00": (4, 1200),
        "Friday 8pm": (4, 1200),
        "friday 8 p.m.": (4, 1200),
        "Mon 9am": (0, 540),
        "Sun 12am": (6, 0),
        "Sat 12pm": (5, 720),
        "2026-10-04 21:00": (6, 1260),   # a Sunday
        "2026-10-02T09:30": (4, 570),    # a Friday, ISO with T
    }
    for text, want in cases.items():
        got = parse_when(text)
        assert_(got == want, f"parse_when({text!r}) = {got}, expected {want}")


def t_parse_when_rejects_garbage():
    """A misread time is worse than a refusal: it answers about another day."""
    # Note the deliberate leniency being tested around: the weekday is matched
    # on its first two letters, so "Weds"/"Thurs" work. Only words whose first
    # two letters are not a weekday's are refused.
    for bad in ("", "   ", "tomorrow evening", "Fri 25:00",
                "20:00", "Fri 8:99", "next week", "8pm Friday"):
        raises(UsageError, lambda bad=bad: parse_when(bad), f"parse_when({bad!r})")


def t_fanout_isolates_a_failing_worker():
    """One item's exception degrades that item, never the run.

    Written by hand twice, this caught only NetworkError, while
    ThreadPoolExecutor re-raises everything else — so one shifted index in one
    result destroyed a search that had already succeeded.
    """
    items = [{"n": i} for i in range(6)]

    def work(item):
        if item["n"] == 3:
            raise TypeError("'<' not supported between 'int' and 'str'")
        if item["n"] == 4:
            raise NetworkError("host is down")
        item["done"] = True

    done = fan_out(items, work, budget=10, workers=4)
    assert_(done == 6, f"fan_out processed {done} items, expected 6")
    survivors = [i["n"] for i in items if i.get("done")]
    assert_(survivors == [0, 1, 2, 5],
            f"a failing worker took others down: survivors {survivors}")
    assert_("done" not in items[3] and "done" not in items[4],
            "a failed item was marked done")


def t_fanout_budget_truncates_and_marks():
    """Beyond the budget, items are handed to on_skipped — never left silently
    unannotated, which downstream cannot tell from 'no data published'."""
    items = [{"n": i} for i in range(5)]
    skipped = []
    done = fan_out(items, lambda i: i.__setitem__("done", True),
                   budget=2, workers=4, on_skipped=skipped.append)
    assert_(done == 2, f"fan_out ran {done} items against a budget of 2")
    assert_([i["n"] for i in items if i.get("done")] == [0, 1],
            "budget truncation took the wrong items")
    assert_([i["n"] for i in skipped] == [2, 3, 4],
            f"on_skipped saw {[i['n'] for i in skipped]}, expected [2, 3, 4]")

    # A zero budget must still mark everything, rather than doing nothing at all.
    marked = []
    assert_(fan_out(items, lambda i: None, budget=0, on_skipped=marked.append) == 0,
            "a zero budget still did work")
    assert_(len(marked) == 5, "a zero budget left items unmarked")


def t_check_budget_refuses_up_front():
    """An oversized plan is refused before the first request, as a usage error —
    not discovered 300 requests in."""
    check_budget(200, 200, "this")  # at the ceiling is fine
    exc = raises(UsageError, lambda: check_budget(201, 200, "a city-wide sweep"),
                 "check_budget over ceiling")
    assert_(exc.exit_code == USAGE, "an oversized plan is not a usage error")
    assert_("201" in str(exc) and "200" in str(exc),
            f"the refusal does not say how big the plan was: {exc}")


def t_hours_annotate_no_hours_is_unknown():
    """A place Google publishes nothing for keeps hours_week None.

    None must reach the caller as *unknown*. Anything that turns it into an
    empty week would render as "Closed" seven times over.
    """
    richmond = "0x89d4cb3323f69325:0xc9f504d1e5cc85a0"
    body = fixture("embed_richmond_station.html")

    def reply(url):
        if "0x89d4cb3323f69325" in url:
            return body
        return "<html>no embed payload here</html>"

    places = [{"ftid": richmond}, {"ftid": "0xdead:0xbeef"}, {"ftid": ""}]
    hours_mod.annotate(FakeSession(reply), places, budget=25)
    assert_(places[0]["hours_week_days"] and len(places[0]["hours_week_days"]) == 7,
            "the known-good place did not get seven days")
    assert_(places[0]["hours_week_source"] == "google-maps-embed",
            "the source of a fetched week is not recorded")
    for place in places[1:]:
        assert_(place["hours_week_days"] is None,
                "a place with no published hours got a week anyway")
        assert_(place.get("hours_week") is None, "unknown hours rendered as a week")
        assert_(place.get("closed") is not True, "unknown hours became closed")


def t_query_open_at_keeps_unknown_out_of_both_piles():
    """--open-at keeps only places *known* open; unknown is excluded, not shut."""
    week = blank_week()
    week[4]["spans"] = [(1020, 1440)]  # Friday 17:00 -> midnight
    rows = [{"name": "known open", "hours_week_days": week},
            {"name": "known shut", "hours_week_days": blank_week()},
            {"name": "no hours", "hours_week_days": None}]
    kept = _filter_open_at(rows, "Fri 20:00")
    assert_([p["name"] for p in kept] == ["known open"],
            f"--open-at kept {[p['name'] for p in kept]}")
    assert_(rows[1]["open_at"] is False, "a place with a published week is not False")
    assert_(rows[2]["open_at"] is None,
            "a place with no published hours was recorded as closed, not unknown")


def t_sort_puts_unmeasured_last():
    """Sorting must never compare None with a number.

    This is the local shape of the comparator bug that took campsite-search
    down: mixing types in a sort key passes every test until real data mixes
    them. Places that could not be routed sort last, they do not raise.
    """
    rows = [{"travel_minutes": None}, {"travel_minutes": 12.0},
            {"travel_minutes": None}, {"travel_minutes": 3.5}]
    order = [p["travel_minutes"] for p in _sort(rows, "travel")]
    assert_(order == [3.5, 12.0, None, None], f"travel sort gave {order}")

    mixed = [{"rating": None}, {"rating": 4.7}, {"rating": 3.1}]
    assert_([p["rating"] for p in _sort(mixed, "rating")] == [4.7, 3.1, None],
            "rating sort mishandled a place with no rating")
    assert_([p["straight_km"] for p in _sort(
        [{"straight_km": 2.0}, {"straight_km": None}], "distance")][0] is None,
        "distance sort mishandled a missing distance")


def _directions_body(echoed_mode: int, *, traffic: bool = True) -> str:
    """A minimal `/maps/preview/directions` reply: `)]}'` + plain JSON.

    Shaped from the live payload: route[0] is the head
    `[mode, via, [metres, text], [seconds, text]]` and the traffic block hangs
    off route[1][0][0][10].
    """
    head = [echoed_mode, "Gardiner Expy", [31000, "31 km"], [1920, "32 min"]]
    block = [[2400, "40 min"], None, 2, [1900, "31 min"],
             [2100, 2700, "35–45 min"]]
    inner = [None] * 10 + [block] if traffic else [None] * 10
    return ")]}'\n" + json.dumps([[None, [[head, [[inner]]]]]])


def t_routing_mode_echo_guard():
    """A wrong-mode route must be dropped, not reported.

    Google ignores an unrecognised mode block instead of erroring — it answers
    HTTP 200 with a *driving* route. The echo at route[0][0] is the only thing
    standing between "43 min walk" and a 13 min drive presented as one, so a
    disagreeing echo has to remove the route entirely.
    """
    origin, dest = (43.6453, -79.3807), (43.6777, -79.6306)

    for mode, code in MODES.items():
        honest = FakeSession(lambda url, c=code: _directions_body(c))
        found = routes(honest, origin, dest, mode)
        assert_(len(found) == 1, f"{mode}: an honestly echoed route was dropped")
        assert_(found[0]["mode"] == mode, f"{mode}: route came back as another mode")

    # The hazard itself: asked to walk, Google echoes drive. Dropping the route
    # is necessary but not sufficient — an empty list is what "no route exists"
    # looks like, and a caller turns that into exit 1, "keep waiting". A mode
    # that was not honoured is a failure to report, so it raises.
    lying = FakeSession(lambda url: _directions_body(MODES["drive"]))
    for asked in ("walk", "transit"):
        exc = raises(NetworkError, lambda a=asked: routes(lying, origin, dest, a),
                     f"a drive route returned for a {asked} request")
        assert_("drive" in str(exc) and asked in str(exc),
                f"the mismatch does not name both modes: {exc}")

    raises(UsageError, lambda: routes(lying, origin, dest, "teleport"),
           "routes with an unknown mode")


def t_routing_traffic_preferred_over_free_flow():
    """When Google supplies a live-traffic duration it wins — and `traffic_aware`
    records which number was used, so nothing describes a free-flow estimate as
    if it accounted for traffic."""
    session = FakeSession(lambda url: _directions_body(MODES["drive"]))
    found = routes(session, (43.6453, -79.3807), (43.6777, -79.6306), "drive")
    best = found[0]
    assert_(best["km"] == 31.0, f"distance read as {best['km']}")
    assert_(best["free_flow_minutes"] == 32.0,
            f"free-flow read as {best['free_flow_minutes']}")
    assert_(best["traffic"]["minutes"] == 40.0,
            f"traffic duration read as {best['traffic']}")

    places = [{"lat": 43.6777, "lng": -79.6306}]
    routing.annotate(session, (43.6453, -79.3807), places, "drive")
    assert_(places[0]["travel_minutes"] == 40.0,
            "annotate preferred the free-flow number over live traffic")
    assert_(places[0]["traffic_aware"] is True, "traffic_aware was not set")

    plain = FakeSession(lambda url: _directions_body(MODES["walk"], traffic=False))
    walked = [{"lat": 43.6777, "lng": -79.6306}]
    routing.annotate(plain, (43.6453, -79.3807), walked, "walk")
    assert_(walked[0]["travel_minutes"] == 32.0,
            "a route with no traffic block did not fall back to free-flow")
    assert_(walked[0]["traffic_aware"] is False,
            "a free-flow number was labelled traffic-aware")


def _star_body(legs: list[tuple[int, int]], echoed_mode: int = 0) -> str:
    """A star-chain reply: one head per leg, in `O,D0,O,D1,O…` order.

    Carries the mode echo at `data[0][1][0][0][0]` because every real payload
    does. The guard fails CLOSED on a missing echo, so a fixture without one
    is not a simpler fixture — it is an unrealistic one that tests a path
    Google never produces.
    """
    heads = [[echoed_mode, "Some Rd", [m, f"{m / 1000:.1f} km"],
              [s, f"{s // 60} min"]] for m, s in legs]
    chained = [[echoed_mode, None, [0, "0 km"], [0, "0 min"]],
               [[h] for h in heads]]
    return ")]}'\n" + json.dumps([[None, [chained]]])


def t_star_legs_take_the_outbound_leg():
    """The star chain is `O,D0,O,D1,O,D2`, so only EVEN legs are what was asked.

    Reading leg *i* instead of leg *2i* returns a real distance for the wrong
    pair — a plausible number attached to the wrong place, which no amount of
    sanity-checking downstream will catch. The return legs below carry absurd
    values so a misalignment cannot pass.
    """
    # Metres chosen to EXCEED the straight-line distance for each pair below:
    # a road route shorter than the crow flies is impossible, and star_legs
    # now rejects such a leg as a misalignment.
    outbound = [(9000, 300), (12000, 600), (18000, 900)]
    back = (999000, 99999)
    legs = []
    for i, out in enumerate(outbound):
        legs.append(out)
        if i < len(outbound) - 1:
            legs.append(back)  # D_i -> O, never reported

    session = FakeSession(lambda url: _star_body(legs))
    got = routing.star_legs(session, (43.65, -79.38),
                            [(43.6, -79.3), (43.7, -79.4), (43.8, -79.5)])
    assert_([leg["km"] for leg in got] == [9.0, 12.0, 18.0],
            f"star legs came back misaligned: {[l['km'] for l in got]}")
    assert_(len(session.calls) == 1,
            f"star_legs made {len(session.calls)} requests, expected 1")
    assert_(routing.star_legs(session, (43.65, -79.38), []) == [],
            "an empty destination list still issued a request")


def t_star_pass_never_claims_traffic_awareness():
    """The cheap pass has no traffic block, and must not pretend otherwise.

    A free-flow number presented as traffic-aware is the silent wrong answer
    here: it reads as "12 minutes right now" when it means "12 minutes on an
    empty road". Places the second pass does not re-price keep the honest flag.
    """
    session = FakeSession(lambda url: _star_body([(9000, 300), (999000, 99999),
                                                  (20000, 1200)]))
    places = [{"lat": 43.6, "lng": -79.3}, {"lat": 43.8, "lng": -79.5}]
    routing.annotate(session, (43.65, -79.38), places, "drive", traffic_top_k=0)
    assert_([p["travel_minutes"] for p in places] == [5.0, 20.0],
            f"star pass priced places as {[p.get('travel_minutes') for p in places]}")
    assert_(all(p["traffic_aware"] is False for p in places),
            "a free-flow star figure was labelled traffic-aware")
    assert_(len(session.calls) == 1,
            f"the star pass alone cost {len(session.calls)} requests")


def t_annotate_reprices_only_the_top_of_the_list():
    """Second pass: the few places a human will be shown get real traffic.

    Everything else keeps the star pass's free-flow number — so the count of
    two-point calls is the budget claim being made here, and `traffic_aware`
    has to split the two groups honestly.
    """
    # Road metres must exceed the straight line for each pair; star_legs now
    # rejects a leg shorter than the crow flies as a misalignment.
    star = _star_body([(9000, 300), (999000, 99999), (20000, 1200),
                       (999000, 99999), (30000, 2400)])

    def reply(url):
        # The star request chains five waypoints; a two-point re-price has two.
        return star if url.count("!3m2!3d") > 2 else _directions_body(MODES["drive"])

    session = FakeSession(reply)
    places = [{"lat": 43.6, "lng": -79.3}, {"lat": 43.8, "lng": -79.5},
              {"lat": 43.9, "lng": -79.6}]
    routing.annotate(session, (43.65, -79.38), places, "drive", traffic_top_k=1)
    assert_(len(session.calls) == 2,
            f"expected 1 star + 1 re-price, got {len(session.calls)} requests")
    aware = [p["traffic_aware"] for p in places]
    assert_(aware == [True, False, False],
            f"the wrong places were re-priced for traffic: {aware}")
    assert_(places[0]["travel_minutes"] == 40.0,
            "the re-priced place did not take its traffic duration")
    assert_(places[1]["travel_minutes"] == 20.0,
            "a place that was not re-priced lost its free-flow figure")


def t_routing_annotate_records_a_failure():
    """A destination that cannot be routed is marked, not silently left blank."""
    def boom(url):
        raise NetworkError("could not reach www.google.com: timed out")

    places = [{"lat": 43.6, "lng": -79.3}]
    routing.annotate(FakeSession(boom), (43.64, -79.38), places, "drive")
    assert_(places[0].get("travel_minutes") is None,
            "a failed route produced a travel time")
    assert_("www.google.com" in places[0].get("travel_error", ""),
            f"the failure does not name the host: {places[0].get('travel_error')}")


def _stub_result(places):
    spec = QuerySpec(near="43.65,-79.38")
    return QueryResult(origin={"query": spec.near, "name": "here",
                               "lat": 43.65, "lng": -79.38},
                       spec=spec, places=places)


SEARCH_ARGV = ["search", "--near", "43.65,-79.38", "--query", "restaurants"]


def t_cli_exit_codes():
    """0 found / 1 nothing matched / 2 usage / 3 network — the polling contract."""
    place = {"name": "Somewhere", "status": UNKNOWN, "rating": None,
             "straight_km": 1.0}
    with patched(cli, "run", lambda s, spec: _stub_result([place])):
        code, _ = run_cli(SEARCH_ARGV)
    assert_(code == FOUND, f"a result gave exit {code}, expected 0")

    with patched(cli, "run", lambda s, spec: _stub_result([])):
        code, _ = run_cli(SEARCH_ARGV)
    assert_(code == EMPTY, f"an empty result gave exit {code}, expected 1")

    # A usage error on a real path: no location to resolve, no socket opened.
    code, _ = run_cli(["geocode", ""])
    assert_(code == USAGE, f"an empty location gave exit {code}, expected 2")

    def unreachable(session, spec):
        raise NetworkError("could not reach www.google.com: timed out")

    with patched(cli, "run", unreachable):
        code, _ = run_cli(SEARCH_ARGV)
    assert_(code == NETWORK, f"a network failure gave exit {code}, expected 3")

    # argparse's own refusal must land on the same usage code. main() catches
    # argparse's SystemExit and RETURNS the code rather than letting it escape,
    # so that a --json caller still gets an error object on stdout instead of a
    # silent empty document; the shell-level exit code is unchanged.
    code, out = run_cli(["search"])
    assert_(code == USAGE, f"argparse path exited {code}, expected 2")
    code, out = run_cli(["--json", "search", "--bogus-flag"])
    assert_(code == USAGE, f"unknown flag exited {code}, expected 2")
    assert_(out.strip().startswith("{"),
            "a --json usage error wrote nothing to stdout; the caller cannot "
            "tell a bad flag from 'nothing matched'")


def t_cli_unexpected_exception_is_three_never_one():
    """The one that makes a watch loop hang forever.

    Exit 1 means "the query worked and nothing matched" — keep waiting. A
    shifted index, a shape change, anything unforeseen must NOT borrow that
    code; it has to surface as a failure.
    """
    for boom in (TypeError("'<' not supported between 'int' and 'str'"),
                 IndexError("list index out of range"),
                 KeyError("lat"),
                 ValueError("bad payload"),
                 ZeroDivisionError("division by zero")):
        def explode(session, spec, boom=boom):
            raise boom

        with patched(cli, "run", explode):
            code, _ = run_cli(SEARCH_ARGV)
        assert_(code == NETWORK,
                f"an unhandled {type(boom).__name__} exited {code}; "
                "anything but 3 (and never 1) is a hanging watch loop")


def t_cli_travel_network_failure_is_not_empty():
    """`travel` must not report a network outage as exit 1.

    Every per-destination failure is caught inside the fan-out and recorded as
    `travel_error`, so the command sees "no destination has a travel time" and
    returns EMPTY. To a watch loop that reads "the query worked, nothing
    matched — keep waiting", and to a person it reads "no route exists", when
    what actually happened is that www.google.com was unreachable.
    """
    def unreachable(url):
        raise NetworkError("could not reach www.google.com: timed out")

    session = FakeSession(unreachable)
    with patched(cli, "new_session", lambda *a, **k: session):
        code, out = run_cli(["travel", "--from", "43.6453,-79.3807",
                             "--to", "43.6777,-79.6306", "--json"])
    assert_("travel_error" in out,
            "the failure was not recorded on the destination at all")
    assert_(code != EMPTY,
            "a total network failure exited 1 (EMPTY) — a watch loop would "
            "wait forever and a user would be told no route exists")
    assert_(code == NETWORK, f"a network failure exited {code}, expected 3")


def t_cli_json_error_goes_to_stdout():
    """Under --json a bare stderr line leaves stdout empty, and a caller cannot
    tell "nothing matched" from "it broke" without parsing prose."""
    def unreachable(session, spec):
        raise NetworkError("could not reach www.google.com: timed out")

    for argv in (SEARCH_ARGV + ["--json"], ["--json"] + SEARCH_ARGV):
        with patched(cli, "run", unreachable):
            code, out = run_cli(argv)
        assert_(code == NETWORK, f"{argv}: exit {code}, expected 3")
        payload = json.loads(out)  # raises if stdout was empty or prose
        assert_(payload["ok"] is False, f"{argv}: error payload is not ok:false")
        assert_(payload["exit_code"] == NETWORK,
                f"{argv}: payload exit_code {payload['exit_code']}")
        assert_("www.google.com" in payload["error"],
                f"{argv}: the error text does not name the host")


def t_cli_json_success_shape():
    """--json output is an object with a results array — the shape recipes chain
    on. A bare array or a renamed key breaks every downstream caller."""
    place = {"name": "Somewhere", "status": OPEN, "rating": 4.5,
             "straight_km": 1.0}
    with patched(cli, "run", lambda s, spec: _stub_result([place])):
        code, out = run_cli(SEARCH_ARGV + ["--json"])
    payload = json.loads(out)
    assert_(code == FOUND, f"exit {code} on a successful json search")
    assert_(isinstance(payload, dict), "--json search returned an array, not an object")
    for key in ("from", "query", "count", "results", "hours_unknown"):
        assert_(key in payload, f"--json output is missing {key!r}")
    assert_(payload["count"] == len(payload["results"]) == 1,
            "count and results disagree")



# --- regressions: every one of these shipped once ----------------------------

def t_open_at_supersedes_open_now():
    """`--open-at` must not be intersected with the open-NOW filter.

    `nearby` filters to currently-open places by default, so "what is open
    Monday 9am", asked at 10pm on a Tuesday, answered "nothing" — and answered
    it as exit 1, which tells a watch loop to keep waiting — while eight
    breakfast places really were open Monday 9am. Two questions about two
    different times must not be ANDed together.
    """
    week = blank_week()
    week[0]["spans"] = [(480, 720)]  # Monday 08:00-12:00
    row = {"name": "shut now, open Monday", "status": CLOSED,
           "business_status": "operating", "lat": 43.6, "lng": -79.3,
           "hours_week_days": week, "ftid": "0x1:0x2", "rating": None}

    spec = QuerySpec(near="43.65,-79.38", open_now=True, open_at="Mon 09:00",
                     want_hours=True)
    with patched(query_mod, "search", lambda *a, **k: [dict(row)]), \
            patched(query_mod.hours_mod, "annotate", lambda *a, **k: 0):
        result = query_mod.run(FakeSession(lambda url: ""), spec)
    assert_([p["name"] for p in result.places] == ["shut now, open Monday"],
            "--open-at was ANDed with open-now: a place open at the asked-for "
            "time was dropped for being shut right now")
    assert_(cli._payload(result)["open_now"] is False,
            "the payload claims an open-now filter that was not applied")


def t_hours_failure_is_never_no_hours():
    """A failed weekly-hours lookup is UNKNOWN, never "publishes none".

    Three places leaked it: the rendered line said "Google publishes none for
    this place", the footnote counted it under "have no published weekly
    schedule", and the counter that fed them lumped it in with places Google
    genuinely has nothing for. All three state a confident fact about a place
    out of a network failure.
    """
    def boom(url):
        raise NetworkError("could not reach www.google.com: blocked")

    places = [{"ftid": "0x1:0x2", "name": "Somewhere"}]
    hours_mod.annotate(FakeSession(boom), places, budget=5)
    assert_("www.google.com" in (places[0].get("hours_error") or ""),
            "a failed hours lookup did not record the host")

    rendered = io.StringIO()
    with contextlib.redirect_stdout(rendered):
        render.place({"name": "Somewhere", "status": UNKNOWN,
                      "hours_error": places[0]["hours_error"]})
    text = rendered.getvalue()
    assert_("publishes none" not in text,
            f"a failed lookup rendered as 'publishes none':\n{text}")
    assert_("FAILED" in text, f"a failed lookup did not say so:\n{text}")

    # The pipeline counts the two separately, and the footnotes print both.
    spec = QuerySpec(near="43.65,-79.38", want_hours=True)
    with patched(query_mod, "search",
                 lambda *a, **k: [{"name": "no hours at all", "status": UNKNOWN,
                                   "business_status": "operating", "rating": None,
                                   "lat": 43.6, "lng": -79.3, "ftid": ""},
                                  {"name": "lookup failed", "status": UNKNOWN,
                                   "business_status": "operating", "rating": None,
                                   "lat": 43.7, "lng": -79.4, "ftid": "0x1:0x2"}]):
        result = query_mod.run(FakeSession(boom), spec)
    assert_(result.week_unknown == 1 and result.hours_failed == 1,
            f"week_unknown={result.week_unknown} hours_failed="
            f"{result.hours_failed}: a failed lookup was counted as a place "
            "with no published hours")

    notes = io.StringIO()
    with contextlib.redirect_stdout(notes):
        render.places(result.places, result)
    footnotes = notes.getvalue()
    assert_("no published weekly schedule" in footnotes and "FAILED" in footnotes,
            f"the footnotes conflate failures with absent hours:\n{footnotes}")


def t_hours_command_reports_a_failure_as_three():
    """`hours` must not exit 0 when the schedule it was asked for never arrived."""
    def reply(url):
        if "embed" in url:
            raise NetworkError("could not reach www.google.com: blocked")
        return fixture("search_single_toronto.txt")

    with patched(cli, "new_session", lambda *a, **k: FakeSession(reply)):
        code, _ = run_cli(["hours", "Black+Blue Toronto", "--near", "43.65,-79.38"])
    assert_(code == NETWORK,
            f"a failed hours lookup exited {code}; 0 would record 'no hours "
            "published' and 1 would tell a watch loop to keep waiting")


def t_hours_discloses_the_place_it_actually_matched():
    """Google answers a name query with its best match, which may be another
    place entirely — "Zzqqx Nonexistent Bistro" came back as "Muse Bistro +
    Bar", with a full schedule and exit 0. The answer is still worth returning;
    passing it off as the place that was named is not."""
    def reply(url):
        if "embed" in url:
            return fixture("embed_richmond_station.html")
        return fixture("search_single_toronto.txt")

    with patched(cli, "new_session", lambda *a, **k: FakeSession(reply)):
        code, out = run_cli(["hours", "Zzqqx Nonexistent Bistro",
                             "--near", "43.65,-79.38"])
    assert_(code == FOUND, f"a matched place exited {code}")
    assert_("nearest match" in out,
            f"the answer does not disclose that it is a nearest match:\n{out}")

    with patched(cli, "new_session", lambda *a, **k: FakeSession(reply)):
        _, out = run_cli(["hours", "Black+Blue Toronto", "--near", "43.65,-79.38"])
    assert_("nearest match" not in out,
            f"an exact match was flagged as approximate:\n{out}")


def t_zero_ceiling_is_a_usage_error_not_an_empty_result():
    """A ceiling of zero issues no requests, so the empty list that follows is
    not "nothing matched". It came back as exit 1 from a flag that meant the
    search never ran — the one code a polling caller must be able to trust."""
    for flag, value in (("--max-requests", "0"), ("--max-requests", "-3"),
                        ("--max-place-requests", "0"), ("--concurrency", "0")):
        code, _ = run_cli(["search", "--near", "43.65,-79.38", flag, value])
        assert_(code == USAGE,
                f"{flag} {value} exited {code}, expected 2 (usage)")


def t_non_list_slots_are_a_shape_change_not_an_empty_result():
    """Every slot Google sends is a list — a no-match search still returns one
    19-element list whose [14] is null (verified live). Slots that are strings
    or nulls mean the payload moved, and returning [] for them reported a
    schema change as "nothing matched"."""
    for shape in ('[[null,[null,null]]]', '[["hdr",["a","b"]]]'):
        raises(ParseError, lambda s=shape: result_blobs(json.loads(s)),
               f"slots {shape} were read as an empty result")
    empty = json.loads("[[null,[[" + ",".join(["null"] * 19) + "]]]]")
    assert_(result_blobs(empty) == [],
            "a genuinely empty result was reported as a shape change")


def t_html_block_page_survives_a_brace():
    """A block page's inline script holds braces that are not JSON. Bailing out
    on the first one reported "malformed chunk at byte 35" and sent a
    maintainer hunting a parser regression that did not exist."""
    html = ('<!doctype html><html><script>var a={bad json here</script>'
            '<body>unusual traffic</body></html>')
    exc = raises(ParseError, lambda: decode(html), "an HTML block page")
    assert_("CAPTCHA" in str(exc) or "rate-limit" in str(exc),
            f"a block page was misreported as a parser fault: {exc}")
    # A genuinely truncated chunk still says where it broke.
    exc = raises(ParseError, lambda: decode('{"c":1,"d":")]}\''),
                 "a truncated chunk")
    assert_("byte" in str(exc), f"a truncated chunk lost its detail: {exc}")


def _star_body_with_mode(legs, echoed_mode=0):
    """A star reply carrying the mode Google echoes at data[0][1][0][0][0]."""
    heads = [[echoed_mode, "Some Rd", [m, f"{m / 1000:.1f} km"],
              [s, f"{s // 60} min"]] for m, s in legs]
    chained = [[echoed_mode, None, [0, "0 km"], [0, "0 min"]],
               [[h] for h in heads]]
    return ")]}'\n" + json.dumps([[None, [chained]]])


def t_star_legs_check_the_echoed_mode():
    """The cheap pass prices EVERY place, and had no mode guard at all.

    An unrecognised mode token is ignored rather than rejected, so a walk
    request can come back as a driving chain with HTTP 200 — and every place
    would then carry a 12-minute drive labelled "12 min walk". `routes()` has
    always checked the echo; this pass did not.
    """
    session = FakeSession(lambda url: _star_body_with_mode([(9000, 300)], 0))
    raises(NetworkError,
           lambda: routing.star_legs(session, (43.65, -79.38), [(43.6, -79.3)],
                                     "walk"),
           "a driving chain returned for a walk request")
    walk = FakeSession(lambda url: _star_body_with_mode([(9000, 1200)], 2))
    got = routing.star_legs(walk, (43.65, -79.38), [(43.6, -79.3)], "walk")
    assert_(got[0]["free_flow_minutes"] == 20.0,
            f"a correctly-moded star leg was mangled: {got}")


def t_annotate_falls_back_when_the_star_pass_is_rejected():
    """A discarded star pass degrades to the per-place call, which verifies its
    own mode — slower and right, never fast and wrong."""
    def reply(url):
        if url.count("!3m2!3d") > 2:          # the star chain
            return _star_body_with_mode([(1000, 300), (2000, 600)], 0)
        return _directions_body(MODES["walk"])

    session = FakeSession(reply)
    places = [{"lat": 43.6, "lng": -79.3}, {"lat": 43.7, "lng": -79.4}]
    routing.annotate(session, (43.65, -79.38), places, "walk", budget=25)
    assert_(all(p["travel_mode"] == "walk" for p in places),
            "a place kept a mode the response did not confirm")
    assert_(all(p.get("travel_minutes") is not None for p in places),
            "the fallback left places unpriced")


def t_routing_never_exceeds_its_budget():
    """One ceiling, counted in REQUESTS — the star call included.

    Charging only the re-pricing let a documented ceiling of 8 issue 9 when the
    star pass failed and every place had to be priced individually.
    """
    def reply(url):
        if url.count("!3m2!3d") > 2:
            raise NetworkError("could not reach www.google.com: star down")
        return _directions_body(MODES["drive"])

    session = FakeSession(reply)
    places = [{"lat": 43.6 + i / 100, "lng": -79.3} for i in range(8)]
    routing.annotate(session, (43.65, -79.38), places, "drive", budget=8)
    assert_(len(session.calls) <= 8,
            f"a ceiling of 8 issued {len(session.calls)} requests")
    unreached = [p for p in places if p.get("travel_minutes") is None]
    assert_(all(p.get("travel_skipped") for p in unreached),
            "a place the budget could not reach was left as a bare null, which "
            "reads as 'no route exists'")


def t_travel_ceiling_covers_geocodes_and_routing():
    """`travel` spends in two phases against ONE ceiling.

    Free-text destinations are geocoded before any routing starts, and routing
    then costs a star call plus a traffic re-check for the nearest few.
    Charging only the geocodes let the command overspend its own ceiling.
    """
    code, _ = run_cli(["travel", "--from", "Toronto"]
                      + sum([["--to", f"place {i}"] for i in range(20)], []))
    assert_(code == USAGE,
            f"20 free-text destinations against a ceiling of 25 exited {code}; "
            "the plan is 21 geocodes plus 6 routing requests, and must be "
            "refused up front")

    # Coordinates cost no geocode, so the same count is affordable — and the
    # affordable plan must still run, not be refused by an over-eager ceiling.
    session = FakeSession(lambda url: _star_body_with_mode([(9000, 300)], 0))
    with patched(cli, "new_session", lambda *a, **k: session):
        code, _ = run_cli(["travel", "--from", "43.65,-79.38",
                           "--max-place-requests", "6", "--to", "43.6,-79.3"])
    assert_(code == FOUND,
            f"a plan of one coordinate destination exited {code}")


def t_concurrency_flag_reaches_the_pool():
    """`--concurrency` has to lower the worker count at CALL time.

    `workers: int = DEFAULT_WORKERS` binds the module attribute once, when the
    function is defined, so the flag mutated a value nothing ever read again: a
    caller told to turn the request rate down could not.
    """
    seen: list[int] = []
    real_pool = fanout.ThreadPoolExecutor

    class Recording(real_pool):
        def __init__(self, max_workers=None):
            seen.append(max_workers)
            super().__init__(max_workers=max_workers)

    original = fanout.DEFAULT_WORKERS
    try:
        fanout.DEFAULT_WORKERS = 2
        with patched(fanout, "ThreadPoolExecutor", Recording):
            fanout.fan_out([{"i": i} for i in range(6)], lambda it: None,
                           budget=6)
    finally:
        fanout.DEFAULT_WORKERS = original
    assert_(seen == [2], f"fan_out used {seen} workers, expected [2]")

    # And the flag has to reach the module in the first place.
    with patched(cli, "new_session", lambda *a, **k: FakeSession(lambda u: "")):
        run_cli(["search", "--near", "43.65,-79.38", "--concurrency", "3"])
    assert_(fanout.DEFAULT_WORKERS == 3,
            f"--concurrency 3 left DEFAULT_WORKERS at {fanout.DEFAULT_WORKERS}")
    fanout.DEFAULT_WORKERS = original


def t_traffic_is_checked_against_the_route_duration():
    """In-traffic can never beat free-flow — checked against the route too.

    The ferry route reported 13 min of road against a 34 min journey. The
    block's own free-flow figure caught it there, but nothing guarantees that
    figure is present; with it absent the route's duration is the only
    reference left.
    """
    head = [0, "Queens Quay", [4000, "4 km"], [2040, "34 min"]]
    block = [[780, "13 min"], None, 3, None, [700, 900, "12–15 min"]]
    route = [head, [[[None] * 10 + [block]]]]
    assert_(routing._traffic(route, head[3][0]) is None,
            "a 13-minute traffic figure was accepted on a 34-minute route")

    slower = [head, [[[None] * 10 + [[[2400, "40 min"], None, 3, None, None]]]]]
    assert_(routing._traffic(slower, head[3][0]) is not None,
            "a genuinely slower in-traffic figure was discarded")


def t_closes_soon_is_open_not_closed():
    """Google's own wording for "open, shutting shortly" starts with "Closes".

    It is live in the neighbouring status-detail field ([203][1][4][0]), so a
    prefix match on the status field is one schema shuffle away from calling
    every about-to-close restaurant shut — and the reverse for "Opens soon".
    """
    def blob_with(status):
        blob = [None] * 210
        blob[11] = "Somewhere"
        blob[9] = [None, None, 43.65, -79.38]
        blob[203] = [None, [None, None, None, None, [status], None, None, None,
                            [status]]]
        return blob

    for wording, expected in (("Open", OPEN), ("Closed", CLOSED),
                              ("Closes soon", OPEN), ("Closing soon", OPEN),
                              ("Opens soon", CLOSED),
                              ("Temporarily closed", CLOSED)):
        got = place_from_blob(blob_with(wording))["status"]
        assert_(got == expected,
                f"status {wording!r} read as {got!r}, expected {expected!r}")


def t_failures_are_visible_without_full():
    """`--full` is for identifiers, not for the reason an answer is missing.

    A consumer that cannot see "this lookup failed" reads a missing schedule as
    "no hours published" — the false negative the package exists to prevent.
    """
    place = {"name": "Somewhere", "status": UNKNOWN, "hours_week": None,
             "hours_error": "could not reach www.google.com",
             "travel_error": "could not reach www.google.com",
             "hours_skipped": True, "lat": 43.6, "lng": -79.3}
    trimmed = project(place, full=False)
    for key in ("hours_error", "travel_error", "hours_skipped"):
        assert_(key in trimmed, f"{key} is invisible without --full")
    assert_("lat" not in trimmed, "--full-only fields leaked into the summary")


def t_ascii_stdout_still_prints_the_answer():
    """A C-locale container gives an ASCII stdout, and every result page prints
    a star, an accent or an arrow. Every single search died there with
    `UnicodeEncodeError`, reported as exit 3 — the data was fine, only the
    encoder was not. The skill is meant to survive a hostile environment.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    original = sys.stdout
    sys.stdout = stream
    try:
        cli._force_utf8_output()
        render.places([{"name": "Caf\u00e9 Ol\u00e9", "status": OPEN,
                        "rating": 4.8, "straight_km": 1.0,
                        "transit": {"depart": "9:00", "arrive": "9:20",
                                    "timezone": "America/Toronto"}}], None)
        sys.stdout.flush()
    finally:
        sys.stdout = original
    text = raw.getvalue().decode("utf-8", "replace")
    assert_("4.8" in text and "Caf" in text,
            f"an ASCII stdout lost the answer: {text!r}")


def t_wrong_mode_everywhere_is_not_an_empty_result():
    """`travel` must not report a mode Google refused to honour as "no route".

    An empty route list is exit 1 — "the query worked, nothing matched" — and a
    watch loop keeps waiting on it. Routes discarded for coming back in the
    wrong mode are not an absence; they are a failure.
    """
    session = FakeSession(lambda url: _directions_body(MODES["drive"]))
    with patched(cli, "new_session", lambda *a, **k: session):
        code, out = run_cli(["travel", "--from", "43.65,-79.38",
                             "--to", "43.66,-79.39", "--mode", "walk",
                             "--json"])
    assert_(code == NETWORK,
            f"every route in the wrong mode exited {code}, expected 3")
    payload = json.loads(out)
    assert_(payload["results"][0].get("travel_minutes") is None,
            "a drive time was reported against a walk request")


def t_locale_flags_cannot_rewrite_the_request():
    """`--hl`/`--gl` are interpolated into the directions URL, because the pb
    that follows must keep its literal "!" delimiters and cannot go through
    urlencode. That put raw user input in a query string: a `--gl` of "ca#x"
    truncated the whole pb into a fragment and Google answered a completely
    different question with HTTP 200 rather than rejecting it."""
    url = routing._endpoint("en&authuser=9", "ca#x")
    assert_("#" not in url and url.count("&") == 2,
            f"a locale value escaped its parameter: {url}")
    assert_("hl=en%26authuser%3D9" in url and "gl=ca%23x" in url,
            f"locale values were not percent-encoded: {url}")


def t_open_at_matches_the_printed_schedule():
    """Every verdict below was read off the rendered week by hand.

    `--open-at` and the schedule a human is shown come from the same records
    but by different routes — one through `week_index`, one through Google's
    display strings — so they can disagree without anything raising. These are
    the three cases where they most easily do: a day the place is CLOSED, a
    span that runs to midnight, and a place open past midnight into a day whose
    own schedule says it opens later.
    """
    schedules = {name: _embed_days(name) for name in
                 ("embed_alo_closed_days.html", "embed_richmond_station.html",
                  "embed_katzs_24h.html")}

    cases = [
        # Alo: "Monday: Closed", "Tuesday: 5 p.m.-12 a.m.", "Sunday: Closed"
        ("embed_alo_closed_days.html", "Mon 20:00", False),
        ("embed_alo_closed_days.html", "Sun 20:00", False),
        ("embed_alo_closed_days.html", "Tue 16:59", False),
        ("embed_alo_closed_days.html", "Tue 17:00", True),
        ("embed_alo_closed_days.html", "Tue 23:59", True),
        # ...and midnight is the END of Tuesday, not the start of Wednesday's
        # trading: Wednesday's own span begins at 5 p.m.
        ("embed_alo_closed_days.html", "Wed 00:30", False),
        # Richmond Station: two sittings on weekdays, dinner only at weekends.
        ("embed_richmond_station.html", "Mon 13:00", True),
        ("embed_richmond_station.html", "Mon 15:30", False),   # between sittings
        ("embed_richmond_station.html", "Mon 19:00", True),
        ("embed_richmond_station.html", "Sat 13:00", False),   # no lunch sitting
        ("embed_richmond_station.html", "Sat 19:00", True),
        # Katz's: "Saturday: Open 24 hours", "Sunday: 12 a.m.-11 p.m."
        ("embed_katzs_24h.html", "Sat 03:00", True),
        ("embed_katzs_24h.html", "Sun 03:00", True),
        ("embed_katzs_24h.html", "Sun 23:30", False),
        ("embed_katzs_24h.html", "Mon 07:00", False),          # opens at 8 a.m.
        ("embed_katzs_24h.html", "Mon 08:00", True),
    ]
    for fixture_name, when, expected in cases:
        rows = [{"name": fixture_name, "hours_week_days": schedules[fixture_name]}]
        kept = _filter_open_at(rows, when)
        assert_(rows[0]["open_at"] is expected,
                f"{fixture_name} at {when}: verdict {rows[0]['open_at']}, the "
                f"printed schedule says {expected}\n  "
                + "\n  ".join(describe(schedules[fixture_name])))
        assert_(bool(kept) is expected,
                f"{fixture_name} at {when}: the verdict and the kept list "
                "disagree")


def t_hours_no_match_answers_on_stdout():
    """`hours` found nothing must say so where the caller is listening.

    It wrote only to stderr and returned 1, so under --json stdout was empty —
    the exact ambiguity `_fail` exists to remove. An empty stdout is
    indistinguishable from a crash, and every other command answers a non-zero
    outcome on stdout.
    """
    empty = ('{"c":1,"d":")]}\'\\n[[null,[[' + ",".join(["null"] * 19) + ']]]]"}')
    with patched(cli, "new_session", lambda *a, **k: FakeSession(lambda u: empty)):
        code, out = run_cli(["hours", "Nowhere", "--near", "43.65,-79.38",
                             "--json"])
    assert_(code == EMPTY, f"a genuine no-match exited {code}, expected 1")
    payload = json.loads(out)
    assert_(payload["exit_code"] == EMPTY and "Nowhere" in payload["error"],
            f"the no-match answer does not name what was not found: {payload}")


def t_skipped_places_are_counted_and_disclosed():
    """The per-place ceiling must be visible when it bites.

    A `truncated_at` field was declared, emitted in every payload and rendered
    as a footnote — and never assigned, so it read null forever and the
    footnote was unreachable. A signal that cannot fire is worse than none: a
    consumer trusts it. `skipped` replaces it and is counted from the places
    themselves, in BOTH enrichment stages.
    """
    def reply(url):
        if url.count("!3m2!3d") > 2:
            raise NetworkError("could not reach www.google.com: star down")
        return _directions_body(MODES["drive"])

    places = [{"lat": 43.6 + i / 100, "lng": -79.3} for i in range(4)]
    routing.annotate(FakeSession(reply), (43.65, -79.38), places, "drive",
                     budget=4)
    skipped = [p for p in places if p.get("travel_skipped")]
    assert_(skipped, "the budget bit, but no place was marked skipped")

    result = _stub_result(places)
    result.skipped = len(skipped)
    notes = io.StringIO()
    with contextlib.redirect_stdout(notes):
        render.places([{"name": "x", "status": OPEN}], result)
    assert_("not looked up" in notes.getvalue(),
            f"a truncated answer did not disclose it:\n{notes.getvalue()}")

    # And nothing in the payload may be a permanently-null promise.
    payload = cli._payload(_stub_result([]))
    assert_("truncated_at" not in payload,
            "the unreachable truncated_at field is still in the payload")


def t_directions_shape_change_is_not_an_empty_route():
    """routes() must RAISE on a missing route container, not return [].

    Asserted directly on routes() rather than through the CLI: the travel
    command reaches exit 3 by another path (star_legs converts its own failure
    first), so an end-to-end assertion passed even with this guard deleted.
    A test that cannot fail is worse than no test.
    """
    for payload, why in ((")]}'\n[[\"x\"]]", "container too short"),
                         (")]}'\n[]", "empty top level"),
                         (")]}'\n{\"a\": 1}", "object instead of array")):
        with patched(http_mod.Session, "get_text",
                     lambda self, *a, _p=payload, **k: _p):
            try:
                got = routes(http_mod.Session(), (43.6, -79.4), (43.7, -79.5))
            except (ParseError, NetworkError):
                continue
        raise AssertionError(
            f"{why}: routes() returned {got!r} instead of reporting a shape "
            "change — indistinguishable from 'no route exists', which exits 1")


def t_non_network_transport_failure_is_recorded():
    """A truncated body must not vanish.

    http.client.IncompleteRead subclasses HTTPException/ValueError but NOT
    OSError, so it escaped every net in http.get_text and then the worker's
    `except NetworkError`. fan_out swallowed it, the place gained no hours_*
    keys, and the renderer announced "Google publishes none for this place" —
    a confident fact manufactured out of a flaky connection.
    """
    import http.client as httplib

    def boom(session, ftid):
        raise httplib.IncompleteRead(b"", 500)

    place = {"name": "X", "ftid": "0x1:0x2", "lat": 43.6, "lng": -79.4,
             "status": "open"}
    with patched(hours_mod, "fetch_week", boom):
        hours_mod.annotate(None, [place])
    assert_(place.get("hours_error"),
            "a non-NetworkError transport failure left no trace on the place")
    assert_(place.get("hours_week") is None, "a failed lookup produced a week")


def t_describe_uses_spans_not_display_strings():
    """A day with real spans and no display string must not print 'Closed'.

    describe() inferred closure from an empty display list while is_open_at()
    read the spans, so one record answered open in JSON and closed in the human
    line.
    """
    days = [{"day": d, "display": [], "spans": [], "closed": False}
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday")]
    days[0]["spans"] = [(540, 1020)]
    line = hours_mod.describe(days)[0]
    assert_("Closed" not in line, f"day with real spans printed {line!r}")
    assert_(hours_mod.is_open_at(days, 0, 600) is True,
            "is_open_at disagrees with describe on the same record")


def t_travel_ceiling_scales_with_destinations():
    """40 destinations must be REFUSED, not silently truncated to 15.

    The cost estimate saturated at the traffic-recheck cap, so the budget check
    passed for any --to count and annotate() then truncated — rendering the
    remainder as "no route found", a false negative at exit 0.
    """
    argv = ["travel", "--from", "43.65,-79.38"]
    for i in range(40):
        argv += ["--to", f"43.6{i},-79.4"]
    code, _ = run_cli(argv)
    assert_(code == USAGE,
            f"40 destinations exited {code}, expected 2 (refused up front)")


def t_unknown_wording_is_unknown_not_closed():
    """A status string matching no known prefix must be UNKNOWN, not CLOSED.

    The package's headline false negative, and it was untested: every existing
    case fed an ABSENT value, which exits at the isinstance check, so the final
    `return UNKNOWN` never executed. A new Google wording or a localised one
    ("Ouvert", "Geschlossen", "Temporarily unavailable") would land there — and
    calling it closed sends someone away from an open restaurant.
    """
    for wording in ("Ouvert 24 heures", "Geschlossen", "Might be busy",
                    "Reopens later", ""):
        blob = [None] * 210
        blob[203] = [None, [None] * 9]
        blob[203][1][8] = [wording]
        assert_(model_mod._status(blob) == UNKNOWN,
                f"status {wording!r} resolved to {model_mod._status(blob)!r}, "
                "expected unknown")


def t_search_outage_is_not_an_empty_result():
    """cli._outcome's network branch was never executed by any test.

    The travel path had its own return and its own test; search/nearby went
    through _outcome, whose `if result.network_errors: return NETWORK` line was
    dead in the suite. A total outage during `search --with-hours` returned
    exit 1 — the watch-loop-hangs-forever failure its docstring describes.
    """
    result = QueryResult(origin={}, spec=QuerySpec(near="x"))
    result.places = []
    result.network_errors = 3
    assert_(cli._outcome(result) == NETWORK,
            "an outage that emptied the result list reported as 'nothing matched'")
    result.network_errors = 0
    assert_(cli._outcome(result) == EMPTY, "a genuine empty result is not exit 1")


def t_directions_pb_puts_latitude_before_longitude():
    """The #1 hazard in routing.py's own docstring, and it had no offline test.

    The directions pb is `3d`=LAT `4d`=LNG — the REVERSE of the search pb,
    where `2d` is longitude. Swap them and Google routes between two other
    places and returns a perfectly plausible number. Every offline routing test
    used a fake session that ignores URL geometry, so only the live test caught
    it, and only indirectly.
    """
    pb = routing._pb((43.6453, -79.3807), (43.6777, -79.6306), 0)
    assert_("!3d43.6453!4d-79.3807" in pb,
            f"origin waypoint has lat/lng swapped or reordered: {pb[:80]}")
    assert_("!3d43.6777!4d-79.6306" in pb,
            f"destination waypoint has lat/lng swapped or reordered: {pb[:80]}")
    star = routing._waypoint((43.6453, -79.3807))
    assert_(star == "!1m4!3m2!3d43.6453!4d-79.3807!6e2",
            f"star waypoint encoding changed: {star}")


def t_truncated_payload_is_a_shape_change():
    """The FIRST guard in result_blobs — a payload too short to index — was
    never taken; existing tests only reached the second."""
    for payload in ([], [[]], [{"a": 1}]):
        try:
            result_blobs(payload)
        except ParseError:
            continue
        raise AssertionError(f"{payload!r} returned an empty result instead of "
                             "reporting a shape change")


def t_open_at_closing_minute_is_closed():
    """The exact minute a place shuts must be CLOSED.

    16 hand-verified cases and none landed on a boundary, so `minutes < end`
    could become `<=` untouched — reporting a place open at the moment it
    closes.
    """
    days = [{"day": d, "display": [], "spans": [], "closed": False}
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday")]
    days[0]["spans"] = [(540, 1020)]          # 09:00-17:00
    assert_(hours_mod.is_open_at(days, 0, 1019) is True, "16:59 should be open")
    assert_(hours_mod.is_open_at(days, 0, 1020) is False,
            "17:00 is the closing minute and must not be open")
    assert_(hours_mod.is_open_at(days, 0, 540) is True, "09:00 opening is open")
    assert_(hours_mod.is_open_at(days, 0, 539) is False, "08:59 is not open")


def t_hours_budget_truncation_is_disclosed():
    """The hours half of the skipped-disclosure chain was untested.

    Only routing's travel_skipped had coverage, so hours could stop marking
    skips and nothing would notice — and a short answer then reads as "there
    is nothing more" rather than "we stopped asking".
    """
    places = [{"name": f"p{i}", "ftid": f"0x{i}:0x{i}", "lat": 43.6, "lng": -79.4}
              for i in range(5)]
    with patched(hours_mod, "fetch_week", lambda s, f: None):
        hours_mod.annotate(None, places, budget=2)
    skipped = [p for p in places if p.get("hours_skipped")]
    assert_(len(skipped) == 3,
            f"{len(skipped)} places marked skipped past a budget of 2, expected 3")
    assert_(not places[0].get("hours_skipped"),
            "a place inside the budget was marked skipped")


def t_permanently_closed_places_are_dropped():
    """The filter at query.run never executed under any test.

    A closed-down restaurant offered as "nearby" is worse than no answer.
    """
    rows = [{"name": "gone", "business_status": "closed", "status": "unknown",
             "lat": 43.6, "lng": -79.4, "rating": 4.9},
            {"name": "here", "business_status": "operating", "status": "open",
             "lat": 43.6, "lng": -79.4, "rating": 4.0}]
    with patched(query_mod, "search", lambda *a, **k: [dict(r) for r in rows]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")):
        kept = query_mod.run(None, QuerySpec(near="43.6,-79.4")).places
    names = [p["name"] for p in kept]
    assert_("gone" not in names, f"a permanently closed listing survived: {names}")
    assert_("here" in names, f"the operating place was dropped: {names}")


def t_parse_when_refuses_an_ambiguous_bare_hour():
    """'Fri 8' could be breakfast or dinner. The documented refusal had no test."""
    for text in ("Fri 8", "Friday 7", "Mon 11"):
        try:
            hours_mod.parse_when(text)
        except UsageError:
            continue
        raise AssertionError(f"parse_when({text!r}) guessed instead of refusing")
    assert_(hours_mod.parse_when("Fri 8pm") == (4, 1200), "8pm should still parse")
    assert_(hours_mod.parse_when("Fri 20:00") == (4, 1200), "20:00 should still parse")


def t_fetch_week_rejects_a_partial_week():
    """fetch_week's OWN guards, including the seven-day count.

    Every other offline hours test calls a helper that re-implements this
    parsing, so fetch_week's marker/decode/place/day-count checks were dead
    code in the suite. The day-count one matters most: a partial week would
    otherwise render as a confident schedule with days silently missing.
    """
    good = fixture("embed_richmond_station.html")
    with patched(http_mod.Session, "get_text", lambda self, *a, **k: good):
        days = hours_mod.fetch_week(http_mod.Session(), "0x1:0x2")
    assert_(days and len(days) == 7, "a known-good payload failed to parse")

    # A payload whose seven records collapse to fewer than seven distinct days
    # must be refused. Deleting records does NOT exercise this: _day_records
    # already requires exactly seven, so a short list is rejected one guard
    # earlier. A DUPLICATED day name is the case that reaches it — seven
    # records in, six days out — and without the count check that renders as a
    # confident schedule with a day silently missing.
    import json as _json
    marker = good.index("initEmbed(")
    data, _ = _json.JSONDecoder().raw_decode(good, good.index("[", marker))
    records = hours_mod._day_records(hours_mod._find_place(data))
    assert_(records is not None and len(records) == 7, "fixture lost its day block")
    records[6][0] = records[0][0]                 # duplicate a weekday
    partial = good[:marker] + "initEmbed(" + _json.dumps(data) + ");"
    with patched(http_mod.Session, "get_text", lambda self, *a, **k: partial):
        got = hours_mod.fetch_week(http_mod.Session(), "0x1:0x2")
    assert_(got is None,
            f"a payload collapsing to {got and len(got)} distinct days was "
            "accepted as a full week")

    for broken, why in ((" no initEmbed here ", "missing marker"),
                        ("initEmbed(not-json", "undecodable payload"),
                        ("initEmbed([1,2,3])", "no place record")):
        with patched(http_mod.Session, "get_text",
                     lambda self, *a, _b=broken, **k: _b):
            got = hours_mod.fetch_week(http_mod.Session(), "0x1:0x2")
        assert_(got is None, f"{why} produced {got!r} instead of None")


def t_limit_is_applied_after_the_free_filters():
    """Filters must see the whole page, not an arbitrary prefix of it.

    `search` used to return `out[:limit]`, so `--limit 8 --min-rating 4.5`
    filtered 8 candidates and reported 6 — while 14 qualifying places sat in
    the same response, already parsed and already paid for. Worse, nothing
    counted the loss, so "6" read as "only 6 exist".
    """
    page = [{"name": f"p{i}", "lat": 43.6, "lng": -79.4, "status": "open",
             "business_status": "operating", "rating": 5.0 if i >= 10 else 2.0}
            for i in range(20)]
    with patched(query_mod, "search", lambda *a, **k: [dict(p) for p in page]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")):
        res = query_mod.run(None, QuerySpec(near="43.6,-79.4", limit=8,
                                            min_rating=4.5))
    assert_(len(res.places) == 8,
            f"{len(res.places)} places returned; the 10 high-rated ones were "
            "behind a prefix cut applied before the filter")
    assert_(res.matched_before_limit == 10,
            f"matched_before_limit was {res.matched_before_limit}, expected 10 "
            "— without it a capped answer reads as an exhausted one")


def t_search_center_rejects_impossible_coordinates():
    """geocode prefers this value, so an unchecked pair poisons every request.

    A swapped pair is accepted by the pb, searches another hemisphere, and
    nothing downstream looks wrong.
    """
    def payload(lat, lng):
        loc = [None, [None, lng, lat]]           # loc[1] = [_, lng, lat]
        slot = [None] * 11
        slot[10] = [None, None, None, loc]       # slot[10][3] = loc
        return [[None, [slot]]]

    assert_(search_center(payload(43.65, -79.38)) == (43.65, -79.38),
            "a valid centre was rejected")
    for lat, lng, why in ((151.2, -33.9, "lat/lng swapped"),
                          (95.0, 10.0, "latitude past the pole"),
                          (10.0, 200.0, "longitude past the antimeridian")):
        assert_(search_center(payload(lat, lng)) is None,
                f"{why}: ({lat}, {lng}) was accepted as a search centre")


def t_mode_guard_fails_closed_on_a_missing_echo():
    """A non-int echo must REFUSE, not fall through to 'trust it'.

    A renumbering is exactly the event that would move this slot, and the old
    form degraded to accepting the answer — fail-open in the one place the
    invariant demands fail-closed.
    """
    for bogus in (None, "0", [], {}):
        heads = [[bogus, "Rd", [9000, "9.0 km"], [300, "5 min"]]]
        body = ")]}'\n" + json.dumps([[None, [[h] for h in heads]]])
        session = FakeSession(lambda url, _b=body: _b)
        # Discarded AND reported: an unverifiable mode is not "no route", it is
        # a refusal to guess, so it must not reach the caller as an empty list.
        raises(NetworkError,
               lambda: routes(session, (43.65, -79.38), (43.6, -79.3), "drive"),
               f"an echo of {bogus!r} was accepted as a verified mode")


def t_star_leg_shorter_than_the_crow_flies_is_rejected():
    """A dropped or inserted leg shifts every destination onto its neighbour.

    That is a plausible wrong value, never an exception. A road route cannot be
    shorter than the straight line between the same two points, so the
    haversine distance is a free consistency check on the mapping.
    """
    # 500 m of "road" between points ~8 km apart: impossible.
    session = FakeSession(lambda url: _star_body([(500, 300)]))
    got = routing.star_legs(session, (43.65, -79.38), [(43.6, -79.3)])
    assert_(got == [None],
            f"an impossibly short leg was accepted: {got} — a misaligned star "
            "chain would pass straight through")


def t_traffic_never_reports_a_faster_than_free_flow_pair():
    """The 2% rounding window must not let an inversion reach the payload.

    The mixed-source case was fixed earlier; the tolerance itself still let a
    29.5-minute in-traffic figure ship beside a 30.0-minute free-flow one.
    `--full` carries both, so the payload stated that congestion saved the
    driver thirty seconds.
    """
    block = [[1770, "29.5 min"], None, 2, [1800, "30 min"],
             [1700, 2000, "28-33 min"]]
    inner = [None] * 11
    inner[10] = block
    route = [[0, "Rd", [9000, "9 km"], [1800, "30 min"]], [[inner]]]
    got = routing._traffic(route)
    assert_(got is not None, "a within-tolerance block was discarded entirely")
    assert_(got["minutes"] >= got["free_flow_minutes"],
            f"traffic {got['minutes']} reported faster than free-flow "
            f"{got['free_flow_minutes']}")


def t_skipped_places_do_not_become_nothing_matched():
    """An empty list because we never looked is NOT exit 1.

    Exit 1 tells a watch loop to keep waiting and tells a person the search was
    exhaustive. Neither is true when a per-place ceiling stopped us short, so
    this must land on the usage code that names the flag which fixes it.
    """
    result = QueryResult(origin={}, spec=QuerySpec(near="x"))
    result.places = []
    result.skipped = 1
    assert_(cli._outcome(result) == USAGE,
            "places skipped for budget were reported as 'nothing matched'")
    result.skipped = 0
    assert_(cli._outcome(result) == EMPTY,
            "a genuinely empty result stopped being exit 1")


def t_bad_open_at_is_refused_before_spending_requests():
    """A typo must not cost a full round of enrichment calls first.

    parse_when was first reached inside _filter_open_at, after hours.annotate
    had already run — so `--open-at "Fri 8"` made up to 25 embed requests to
    Google and then exited 2.
    """
    calls = []

    def counting(self, url, *a, **k):
        calls.append(url)
        return fixture("search_page1_toronto.txt")

    with patched(http_mod.Session, "get_text", counting):
        code, _ = run_cli(["search", "--near", "43.6532,-79.3832",
                           "--open-at", "Fri 8", "--limit", "8"])
    assert_(code == USAGE, f"an ambiguous --open-at exited {code}, expected 2")
    assert_(len(calls) <= 1,
            f"{len(calls)} requests were issued before rejecting a bad flag")


def t_as_coordinates_round_trips_and_range_checks():
    """`--near 43.65,-79.38` must not become (-79.38, 43.65).

    A swap here searches the Antarctic ocean and NOTHING downstream re-checks
    it — every coordinate query in the package flows through this one function.
    Its sibling `search_center` has a range test; this did not.
    """
    assert_(query_mod.as_coordinates("43.65,-79.38") == (43.65, -79.38),
            "lat/lng came back swapped or altered")
    assert_(query_mod.as_coordinates("-33.87,151.21") == (-33.87, 151.21),
            "a southern-hemisphere pair was mangled")
    for bad in ("200,300", "91,0", "0,181", "-91,0", "abc,def", "43.65", ""):
        assert_(query_mod.as_coordinates(bad) is None,
                f"{bad!r} was accepted as a coordinate pair")


def t_geocode_refuses_an_unresolvable_name():
    """An unresolvable name must raise, not quietly become (0, 0).

    Returning a default would answer from the Gulf of Guinea at exit 0 — a
    confident result for a place that does not exist.
    """
    header = [None] * 11          # a slot with no place blob at [14]
    empty = json.dumps({"c": 0, "d": ")]}'\n" + json.dumps([[None, [header]]])})
    raises(UsageError,
           lambda: geocode(FakeSession(lambda url: empty), "qqzzxx nowhere"),
           "an unresolvable place name did not raise UsageError")


def t_star_chain_alternates_origin_and_destination():
    """The chain must be `O,D0,O,D1,O,D2` — every EVEN leg an outbound route.

    Drop the origin between stops and the chain becomes O,D0,D1,D2, so
    `legs[i*2]` hands dests[1] the D1->D2 leg: a real distance for the wrong
    pair. The haversine guard cannot catch it, because it compares against
    O->D1 and a neighbouring leg is usually plausible against that.
    """
    seen = []
    session = FakeSession(lambda url: seen.append(url) or _star_body(
        [(9000, 300), (999000, 99999), (12000, 600),
         (999000, 99999), (18000, 900)]))
    routing.star_legs(session, (43.65, -79.38),
                      [(43.6, -79.3), (43.7, -79.4), (43.8, -79.5)])
    pb = seen[0]
    expected = "".join(routing._waypoint(p) for p in
                       [(43.65, -79.38), (43.6, -79.3), (43.65, -79.38),
                        (43.7, -79.4), (43.65, -79.38), (43.8, -79.5)])
    assert_(expected in pb,
            "the waypoint chain is not O,D0,O,D1,O,D2 — every destination "
            "after the first would be given another pair's leg")


def t_search_pb_page_and_offset_are_not_swapped():
    """`7i` is page size and `8i` is offset. Swapped, paging breaks silently."""
    first = pb.search_pb(43.65, -79.38, 10000, 20, 0)
    second = pb.search_pb(43.65, -79.38, 10000, 20, 20)
    assert_("!7i20!8i0" in first, f"page 1 pb is wrong: {first}")
    assert_("!7i20!8i20" in second, f"page 2 pb is wrong: {second}")


def t_search_returns_the_whole_page_not_the_limit():
    """Asserted on places.search ITSELF.

    The existing limit-vs-filter test patches query_mod.search, so this line
    never runs in it — the bug it is named for could come back untouched.
    """
    body = fixture("search_page1_toronto.txt")
    got = search(FakeSession(lambda url: body), "restaurants", 43.65, -79.38,
                 limit=3)
    assert_(len(got) == 20,
            f"search() returned {len(got)} places for limit=3; it must return "
            "the whole page so the caller's filters see every candidate")


def t_min_rating_and_within_boundaries():
    """Inclusive thresholds, and unrated/unrouted places never sneak through."""
    rows = [{"name": "exact", "rating": 4.5, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"},
            {"name": "under", "rating": 4.4, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"},
            {"name": "unrated", "rating": None, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"}]
    with patched(query_mod, "search", lambda *a, **k: [dict(r) for r in rows]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")):
        res = query_mod.run(None, QuerySpec(near="43.6,-79.4", min_rating=4.5))
    names = [p["name"] for p in res.places]
    assert_(names == ["exact"],
            f"--min-rating 4.5 kept {names}; 4.5 is inclusive and an unrated "
            "place must not pass a rating filter")


def t_nearby_routes_and_search_does_not():
    """The two commands differ by exactly this, and nothing asserted it.

    With routing switched off, `nearby` still sorts by travel time — on a field
    that is never populated.
    """
    rows = [{"name": "a", "rating": 4.0, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"}]
    calls = {"routing": 0}

    def fake_routing(*a, **k):
        calls["routing"] += 1
        return 0

    with patched(query_mod, "search", lambda *a, **k: [dict(r) for r in rows]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")), \
         patched(query_mod.routing, "annotate", fake_routing):
        run_cli(["nearby", "--near", "43.6,-79.4", "--limit", "1"])
        assert_(calls["routing"] == 1, "nearby did not route its results")
        calls["routing"] = 0
        run_cli(["search", "--near", "43.6,-79.4", "--limit", "1"])
        assert_(calls["routing"] == 0, "search routed its results; it must not")


def t_every_validation_guard_is_a_usage_error():
    """All of _validate, not just the ceiling flags.

    A bad flag that answers oddly or returns empty at exit 1 breaks the
    contract: exit 1 means the query worked and matched nothing.
    """
    for flag, value in (("--span", "0"), ("--span", "-100"),
                        ("--limit", "0"), ("--limit", "-3"),
                        ("--min-rating", "9"), ("--min-rating", "-1"),
                        ("--within", "-5"), ("--within", "0"),
                        ("--query", "   ")):
        argv = ["nearby", "--near", "43.65,-79.38", flag, value]
        code, _ = run_cli(argv)
        assert_(code == USAGE,
                f"{flag} {value} exited {code}, expected 2 — a bad flag must "
                "never look like 'nothing matched'")


def t_open_at_implies_fetching_hours():
    """`--open-at` must turn hours on by itself.

    Drop the `or bool(spec.open_at)` and no hours are fetched, so every place
    fails the filter and the command answers "nothing" at exit 1 — forever, for
    every query, while telling a watch loop to keep waiting.
    """
    rows = [{"name": "a", "rating": 4.0, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"}]
    fetched = {"n": 0}

    def fake_hours(session, places, budget=25):
        fetched["n"] += 1
        for place in places:
            place["hours_week_days"] = _week_open_always()
        return len(places)

    with patched(query_mod, "search", lambda *a, **k: [dict(r) for r in rows]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")), \
         patched(query_mod.hours_mod, "annotate", fake_hours):
        res = query_mod.run(None, QuerySpec(near="43.6,-79.4",
                                            open_at="Fri 20:00"))
    assert_(fetched["n"] == 1,
            "--open-at did not trigger an hours lookup, so its filter had "
            "nothing to read and would drop every place")
    assert_(len(res.places) == 1, f"--open-at dropped an open place: {res.places}")


def _week_open_always():
    return [{"day": d, "display": ["always"], "spans": [(0, 1440)],
             "closed": False}
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday")]


def t_network_errors_counter_is_populated():
    """_outcome's branch was fixed; its INPUT was still unguarded.

    If run() stops counting, an outage-emptied list is exit 1 again — the same
    contract breach, one layer down.
    """
    rows = [{"name": "a", "rating": 4.0, "status": OPEN, "lat": 43.6,
             "lng": -79.4, "business_status": "operating"}]

    def failing_hours(session, places, budget=25):
        for place in places:
            place["hours_error"] = "NetworkError: embed down"
            place["hours_week_days"] = None
        return len(places)

    with patched(query_mod, "search", lambda *a, **k: [dict(r) for r in rows]), \
         patched(query_mod, "geocode", lambda *a, **k: (43.6, -79.4, "x")), \
         patched(query_mod.hours_mod, "annotate", failing_hours):
        res = query_mod.run(None, QuerySpec(near="43.6,-79.4", want_hours=True))
    assert_(res.network_errors == 1,
            f"network_errors was {res.network_errors} after every hours lookup "
            "failed — _outcome would report the outage as 'nothing matched'")


def t_parse_when_matches_whole_weekday_names():
    """"Summer 8pm" must not mean Sunday.

    A two-letter prefix compare makes it Sunday and answers confidently about
    the wrong day.
    """
    for bogus in ("Summer 8pm", "Month 9pm", "Satellite 7pm", "Weds 8pm"):
        try:
            hours_mod.parse_when(bogus)
        except UsageError:
            continue
        raise AssertionError(
            f"parse_when({bogus!r}) resolved to a weekday instead of refusing")
    assert_(hours_mod.parse_when("Sun 8pm")[0] == 6, "Sunday stopped parsing")
    assert_(hours_mod.parse_when("Sat 8pm")[0] == 5, "Saturday stopped parsing")


def t_star_legs_reject_a_misaligned_chain():
    """Leg COUNT is what catches a dropped or inserted leg.

    A distance check cannot: an odd shift maps each destination onto its own
    return leg, which has the same endpoints and therefore the same distance.
    Measured — a haversine floor rejected 0 of 3 misattributed legs.
    """
    dests = [(43.6481, -79.3831), (43.7615, -79.4111), (43.6544, -79.4021)]
    short = _star_body([(9000, 300), (999000, 99999), (12000, 600),
                        (999000, 99999)])          # 4 legs, 5 expected
    raises(NetworkError,
           lambda: routing.star_legs(FakeSession(lambda url: short),
                                     (43.6532, -79.3832), dests),
           "a chain with a dropped leg was mapped onto the destinations anyway")


# ---------------------------------------------------------------- [network]
# Every one of these talks to www.google.com. A failure here names the host, so
# a reader can tell "Google changed/blocked something" from "the skill broke".

def t_live_geocode(session):
    lat, lng, label = geocode(session, "Kensington Market, Toronto")
    assert_(43.5 < lat < 43.8 and -79.6 < lng < -79.2,
            f"www.google.com geocoded Kensington Market to {lat},{lng}")
    assert_(label, "www.google.com returned a location with no label")


def t_live_search(session):
    rows = search(session, "restaurants", 43.6532, -79.3832, limit=10)
    assert_(rows, "www.google.com returned no restaurants in downtown Toronto")
    assert_(all(r["name"] for r in rows), "a live result came back with no name")
    assert_(all(r["status"] in (OPEN, CLOSED, UNKNOWN) for r in rows),
            "a live result carried a status outside open/closed/unknown")
    assert_(any(r["status"] in (OPEN, CLOSED) for r in rows),
            "no live result carried an open/closed status — [203] may have moved")
    assert_(sum(1 for r in rows if r.get("rating") is not None) >= len(rows) // 2,
            "most live results had no rating — the [4][7] index may have moved")


def t_live_week(session):
    """Richmond Station: split service, so a live week must show two spans."""
    days = fetch_week(session, "0x89d4cb3323f69325:0xc9f504d1e5cc85a0")
    assert_(days and len(days) == 7,
            f"www.google.com/maps/embed did not return 7 days: {days and len(days)}")
    assert_(days[0]["day"] == "Monday", "week did not come back Monday-first")
    assert_(any(d["spans"] for d in days), "every day parsed as closed")
    assert_(any(len(d["spans"]) > 1 for d in days),
            "no day had split service — the interval list may be truncated")


def t_live_routing_traffic(session):
    """Union Station -> Pearson: ~31 km, ~32 min, with a live-traffic duration.

    Also asserts the lat/lng order, which is REVERSED from the search pb here —
    swapping it routes between two other places and returns a plausible number
    rather than an error.
    """
    found = routes(session, (43.6453, -79.3807), (43.6777, -79.6306))
    assert_(found, "www.google.com/maps/preview/directions returned no routes")
    best = found[0]
    assert_(20 < best["km"] < 45,
            f"Union->Pearson came out as {best['km']} km, expected ~31")
    assert_(15 < best["free_flow_minutes"] < 70,
            f"duration {best['free_flow_minutes']} min is out of range")
    withtraffic = [r for r in found if r.get("traffic")]
    assert_(withtraffic, "no route carried a traffic block — the [10] index may have moved")
    t = withtraffic[0]["traffic"]
    assert_(t["minutes"] and 10 < t["minutes"] < 120,
            f"traffic duration {t['minutes']} min is implausible")


def t_live_walk_is_slower_than_drive(session):
    """Modes must survive the round trip end to end.

    A malformed mode block does not error — Google quietly returns a driving
    route — so the proof that walking really asked for walking is that the same
    pair takes materially longer on foot. 2 km across downtown Toronto: a few
    minutes to drive, over twenty to walk.
    """
    origin, dest = (43.6453, -79.3807), (43.6629, -79.3957)  # Union -> Queen's Park
    drive = routes(session, origin, dest, "drive")
    walk = routes(session, origin, dest, "walk")
    assert_(drive, "www.google.com returned no driving route")
    assert_(walk, "www.google.com returned no walking route — the mode echo "
                  "guard may be dropping a mislabelled drive")
    assert_(all(r["mode"] == "walk" for r in walk), "a walk request came back as another mode")
    assert_(walk[0]["free_flow_minutes"] > drive[0]["free_flow_minutes"] * 1.5,
            f"walking ({walk[0]['free_flow_minutes']} min) is not materially "
            f"slower than driving ({drive[0]['free_flow_minutes']} min) — the "
            "mode is probably being ignored")


def main() -> int:
    offline_only = "--offline" in sys.argv

    print("[offline] logic against captured responses")
    for name, fn in sorted((k[2:], v) for k, v in globals().items()
                           if k.startswith("t_") and not k.startswith("t_live_")):
        check("offline", name, fn)

    if offline_only:
        print("\nskipping [network] (--offline)")
    else:
        print("\n[network] live calls")
        print("  hosts: www.google.com")
        session = new_session()
        for name, fn in sorted((k[2:], v) for k, v in globals().items()
                               if k.startswith("t_live_")):
            check("network", name, lambda fn=fn: fn(session))

    print(f"\n{_ran - len(_failures)}/{_ran} passed")
    for line in _failures:
        print(f"  {line}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
