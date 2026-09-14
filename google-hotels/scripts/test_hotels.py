#!/usr/bin/env python3
"""Self-check for the google-hotels skill.

    python3 test_hotels.py            # everything
    python3 test_hotels.py --offline  # no sockets; safe in any sandbox

Two labelled groups:

    [offline]  pure logic against saved real pages — encoders, parser, echo
               check, validators, block detection, the seller union, the
               amenity table, every command through cli.main with a stubbed
               client, and the transport's sandbox fallbacks. No network.
    [network]  seven live requests to www.google.com. A failure here names
               the host, so "Google is blocking this IP" stays distinguishable
               from "the skill is broken".

What these tests are for (06 §0): the dangerous bug is not a crash — it is
the answer that looks plausible and is wrong. In order of harm: a price for
a different stay than asked; a nightly figure read from the wrong basis slot;
Google's headline reported as the minimum; an ad or a rental fee mistaken for
a hotel price; a blocked page read as "no rates". So the offline group asserts
*exact values from known captures*, mutates real payloads to prove the guards
fire, and drives every exit code through the CLI.

Rules this file keeps (02 §5, learned the hard way in this repo):

* Every `all(...)` states the count it expected, so a comprehension over an
  empty list cannot pass vacuously.
* Stubs sit one layer BELOW the unit under test: `Client.quote` to test
  `cli`, `Transport.get` to test `Client`, `shutil`/`subprocess` to test
  `Transport`. A test that patches the function it names cannot fail.
* No `"error" in stderr` — every failure asserts the documented code and the
  documented message.

Exits non-zero on any failure, so it can gate an install.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import html as htmllib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unicodedata
from dataclasses import replace
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from ghotels import cli  # noqa: E402
from ghotels import http as ghhttp  # noqa: E402
from ghotels.client import Client, QueryError, validate_stay  # noqa: E402
from ghotels.http import (  # noqa: E402
    HOST, RESULTS_MARKER, HotelsHTTPError, RequestBudgetError, Transport, looks_blocked,
)
from ghotels.ids import (  # noqa: E402
    IdError, cid_from_ftid, cid_from_place_id, decode_token, parse_hotel_id,
    read_gmaps_file, token_from_cid,
)
from ghotels.model import (  # noqa: E402
    KIND_HOTEL, KIND_RENTAL, TIE_MARGIN, Echo, EntityRecord, Price, Stay,
    cheapest_seller, comparable,
)
from ghotels.parse import (  # noqa: E402
    HIGHLIGHT_NAMES, PayloadError, UnknownEntity, blocks, entity_record,
)
from ghotels.ts import encode_ts  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SKILL_MD = os.path.join(os.path.dirname(HERE), "SKILL.md")

# -- the captures ------------------------------------------------------------
#
# Six full pages (trimmed, gzipped; header says how) and four synthetic
# wrappers. Which is which matters: only a full page can exercise block
# detection, the unknown-entity signature, the highlight chips or the amenity
# spans; a wrapper exercises the parser and the echo check.
FAIRMONT = "fixture_fairmont_2ad_ny.html"        # #36: the worked example, 11 sellers
SAMESUN = "fixture_samesun_cid.html"             # #37: single-figure lead, Trip.com only in p[2]
PLUS270 = "fixture_plus270.html"                 # #38: lead null, three offers in p[2]
RENTAL = "fixture_rental_ascent_ny.html"         # #41: kind 2, fees 21 %
CHILD5 = "fixture_fairmont_2ad_child5.html"      # #42: occupancy echo [2, [[5]], 0]
UNKNOWN = "unknown_entity_full.html"             # #35: ds:1 = [5], errorHasStatus
WRAP_1AD = "wrap_fairmont_1ad.html"              # synthetic: occupancy echo [1, null, 0]
WRAP_USD = "wrap_fairmont_usd.html"              # synthetic: USD page
WRAP_EMPTY = "wrap_all_slots_none.html"          # synthetic: all four seller slots null
WRAP_P9 = "wrap_fairmont_p9_digest.html"         # synthetic: two-basis cheapest ≠ headline
BLOCKED = "blocked_consent.html"                 # synthetic: no ds:2
GMAPS_FULL = "gmaps_hotels_near_banff.json"
GMAPS_NOFULL = "gmaps_hotels_near_banff_nofull.json"

FAIRMONT_FTID = "0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce"
FAIRMONT_PLACE_ID = "ChIJv7gvLjvKcFMRzvb0LKnpmQA"
FAIRMONT_CID = 43322584249726670
FAIRMONT_TOKEN = "CgkIzu3T55K1-kwQAQ"
FAIRMONT_FULL_TOKEN = "ChQIzu3T55K1-kwaCS9tLzA1MnR2chAB"   # with the KG id; the probe's
SAMESUN_FTID = "0x5370ca4e06121ff1:0x573e93dcf38f6714"
SAMESUN_PLACE_ID = "ChIJ8R8SBk7KcFMRFGeP89yTPlc"
SAMESUN_CID = 0x573E93DCF38F6714
SAMESUN_TOKEN = "CgoIlM69nM_7pJ9XEAE"
RENTAL_TOKEN = "ChkQp5T1uba_qKpQGg0vZy8xMXo5cmh6MG03EAI"
MOUNT_ROYAL_FTID = "0x5370ca460a10d1db:0x72655c032c2b07af"   # third id for shortlists

#: The stay every dated fixture was priced for.
NY_IN, NY_OUT = date(2026, 12, 31), date(2027, 1, 2)
NY = Stay(NY_IN, NY_OUT, 2, (), "CAD")
NY_CHILD5 = Stay(NY_IN, NY_OUT, 2, (5,), "CAD")
PLUS270_STAY = Stay(date(2027, 6, 9), date(2027, 6, 11), 2, (), "CAD")
P9_STAY = Stay(NY_IN, date(2027, 1, 1), 2, (), "CAD")
#: The day the probe ran; validators are given it explicitly so a boundary
#: test means the same thing on every day this file is run.
PROBE_DAY = date(2026, 9, 13)
#: The CLI reads its clock through `ghotels.client.today()`; every offline
#: CLI-level run below pins it to PROBE_DAY so the captured stays never expire.

#: Every distinct `ts` the probe sent (fixtures/requests.log), decoded by hand
#: from the protobuf — NOT with ids.py, which would make the test circular.
CAPTURED_TS = {
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGB8SBwjrDxABGAIYASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 31), date(2027, 1, 2), 2, [], "CAD"),        # #8, #10, #29, #36, #37, #41 …
    "CAESBgoCCAMQABoYEhYSEgoHCOoPEAwYHxIHCOsPEAEYAhgBKgcKBToDQ0FEMgIIAQ":
        (date(2026, 12, 31), date(2027, 1, 2), 1, [], "CAD"),        # #11 one adult
    "CAESEAoCCAMKAggDCgQIAhAFEAAaGBIWEhIKBwjqDxAMGB8SBwjrDxABGAIYASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 31), date(2027, 1, 2), 2, [5], "CAD"),       # #12, #42 child 5
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGB8SBwjrDxABGAIYASoHCgU6A1VTRDICCAE":
        (date(2026, 12, 31), date(2027, 1, 2), 2, [], "USD"),        # #13 USD
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAJGAESBwjqDxAJGAMYASoHCgU6A0NBRDICCAE":
        (date(2026, 9, 1), date(2026, 9, 3), 2, [], "CAD"),          # #14 past
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGB8SBwjqDxAMGB8YASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 31), date(2026, 12, 31), 2, [], "CAD"),      # #15 zero nights
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGAESBwjrDxABGAUYASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 1), date(2027, 1, 5), 2, [], "CAD"),         # #16 35 nights
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAMGAESBwjrDxAMGAMYASoHCgU6A0NBRDICCAE":
        (date(2027, 12, 1), date(2027, 12, 3), 2, [], "CAD"),        # #17 far future
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGAESBwjqDxAMGB8YASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 1), date(2026, 12, 31), 2, [], "CAD"),       # #19 30 nights
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGAESBwjqDxAMGA8YASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 1), date(2026, 12, 15), 2, [], "CAD"),       # #20 14 nights
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxADGAsSBwjrDxADGA0YASoHCgU6A0NBRDICCAE":
        (date(2027, 3, 11), date(2027, 3, 13), 2, [], "CAD"),        # #21 +180 d
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAJGAoSBwjrDxAJGAwYASoHCgU6A0NBRDICCAE":
        (date(2027, 9, 10), date(2027, 9, 12), 2, [], "CAD"),        # #22 +365 d
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGAESBwjrDxABGAEYASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 1), date(2027, 1, 1), 2, [], "CAD"),         # #25 31 nights
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAIGAgSBwjrDxAIGAoYASoHCgU6A0NBRDICCAE":
        (date(2027, 8, 8), date(2027, 8, 10), 2, [], "CAD"),         # #26 +330 d
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAHGAkSBwjrDxAHGAsYASoHCgU6A0NBRDICCAE":
        (date(2027, 7, 9), date(2027, 7, 11), 2, [], "CAD"),         # #27 +300 d
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAGGAkSBwjrDxAGGAsYASoHCgU6A0NBRDICCAE":
        (date(2027, 6, 9), date(2027, 6, 11), 2, [], "CAD"),         # #28, #38 +270 d
    "CAESFgoCCAMKAggDCgQIAhAFCgQIAhAKEAAaGBIWEhIKBwjqDxAMGB8SBwjrDxABGAIYASoHCgU6A0NBRDICCAE":
        (date(2026, 12, 31), date(2027, 1, 2), 2, [5, 10], "CAD"),   # #32 children 5 + 10
    "CAESCgoCCAMKAggDEAAaGBIWEhIKBwjrDxAIGBkSBwjrDxAIGBsYASoHCgU6A0NBRDICCAE":
        (date(2027, 8, 25), date(2027, 8, 27), 2, [], "CAD"),        # #34 +347 d
}

#: The 11 partner ids on the Fairmont New Year page: 9 two-basis, 2 single.
FAIRMONT_PARTNERS = {76, 89, 184, 220, 558573628, 588414280, 1162912808,
                     1397608158, 1745103910, 1930230291, 2138133885}
SAMESUN_PARTNERS = {599653573, 89, 220, 1162912808, 311056061, 695484593, 84}
PLUS270_PARTNERS = {1587851245, 84, 695484593}
CHILD5_PARTNERS = {184, 220}
RENTAL_PARTNERS = {2084533977, 1436973456, 1850600648}

#: What amenity-entity-table.json and 01 §H5 record for the gate ids, per
#: page: id -> (name, has, qualifier). 06 §3.14 says re-check these against
#: the parsed page rather than trust the constant.
GATE_FAIRMONT = {28: ("Wi-Fi", True, "free"), 15: ("Parking", True, "extra_charge"),
                 54: ("Breakfast", True, "extra_charge"), 19: ("Pool", True, None)}
GATE_SAMESUN = {28: ("Wi-Fi", True, "free"), 15: ("Parking", True, "extra_charge"),
                54: ("Breakfast", True, "free"), 19: ("Pool", False, None)}
QUALIFIER_LABEL = {"free": "free", "extra_charge": "extra charge", "24h": "24 hour"}

_failures: list[str] = []
_passed = 0


def check(group: str, name: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  [{group}] ok   {name}")
    else:
        _failures.append(f"[{group}] {name}: {detail}")
        print(f"  [{group}] FAIL {name} {detail}")


def raises(exc, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def raised(fn, *args, **kwargs):
    """The exception `fn` raised, or None — for asserting on its message."""
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - the outcome under test
        return e
    return None


def fixture(name: str) -> str:
    """A fixture's text; `.gz` files are gunzipped transparently."""
    path = os.path.join(FIXTURES, name)
    if os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_DS1_DATA = re.compile(r"(AF_initDataCallback\(\{key: 'ds:1'.*?data:)(.*?)(, sideChannel)", re.S)


def ds1(html: str):
    """The decoded ds:1 block of a page, as the parser will see it."""
    return json.loads(_DS1_DATA.search(html).group(2))


def with_ds1(html: str, payload) -> str:
    """The page with its ds:1 data replaced by `payload` (Google's own JSON
    formatting is not reproducible with json.dumps, so the block is spliced
    by position rather than by html.replace)."""
    m = _DS1_DATA.search(html)
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return html[:m.start(2)] + text + html[m.end(2):]


def spans(html: str) -> list[str]:
    """Every rendered amenity label on a page, in document order."""
    return [htmllib.unescape(s) for s in re.findall(r'<span class="LtjZ2d">([^<]*)</span>', html)]


def qualified_spans(html: str) -> list[tuple[str, str]]:
    """(label, qualifier) for every label followed by an AdLXZd sibling."""
    return [(htmllib.unescape(a), htmllib.unescape(b)) for a, b in re.findall(
        r'<span class="LtjZ2d">([^<]*)</span>\s*<span class="[^"]*\bAdLXZd\b[^"]*">([^<]*)</span>', html)]


_records: dict[str, EntityRecord] = {}


def record(name: str) -> EntityRecord:
    if name not in _records:
        _records[name] = entity_record(fixture(name))
    return _records[name]


def aligned(rec: EntityRecord, stay: Stay) -> EntityRecord:
    """`rec` re-echoed for `stay`, so a stubbed quote answers the stay asked."""
    return replace(rec, echo=Echo(stay.checkin, stay.checkout, stay.nights, stay.adults,
                                  tuple(stay.child_ages), stay.currency))


def cheaper(rec: EntityRecord, factor: float) -> EntityRecord:
    """`rec` with every price scaled — a cheaper (or dearer) date for a sweep."""
    def price(p: Price | None) -> Price | None:
        if p is None:
            return None
        return replace(p,
                       ex_tax=None if p.ex_tax is None else round(p.ex_tax * factor, 4),
                       incl_tax=None if p.incl_tax is None else round(p.incl_tax * factor, 4),
                       amount=None if p.amount is None else round(p.amount * factor, 4))
    sellers = tuple(replace(s, nightly=price(s.nightly), stay=price(s.stay)) for s in rec.sellers)
    headline = replace(rec.headline, nightly=price(rec.headline.nightly),
                       stay=price(rec.headline.stay)) if rec.headline else None
    breakdown = replace(rec.breakdown, base=rec.breakdown.base * factor, taxes=rec.breakdown.taxes * factor,
                        fees=rec.breakdown.fees * factor, total=rec.breakdown.total * factor) if rec.breakdown else None
    return replace(rec, sellers=sellers, headline=headline, breakdown=breakdown)


@contextlib.contextmanager
def quote_stub(result_for):
    """Swap Client.quote for `result_for(n, ids, stay)`; yields the call list.

    One layer below `cli`: everything in cli.py runs for real — argument
    parsing, validators, the budget, rendering, exit codes — and only the
    fetch-parse-echo step is replaced. `result_for` may raise.
    """
    calls: list[tuple] = []
    original = Client.quote

    def stub(self, ids, stay):
        calls.append((ids, stay, self.transport.max_requests))
        return result_for(len(calls) - 1, ids, stay)

    Client.quote = stub
    try:
        yield calls
    finally:
        Client.quote = original


@contextlib.contextmanager
def get_stub(page_for):
    """Swap Transport.get for `page_for(n, path, params)`; yields the call list.

    One layer below `Client`: the client's own checks — 3xx handled in the
    transport aside — block, unknown entity, echo, currency, union — all run.
    """
    calls: list[tuple] = []
    original = Transport.get

    def stub(self, path, params):
        calls.append((path, dict(params or {})))
        return page_for(len(calls) - 1, path, params)

    Transport.get = stub
    try:
        yield calls
    finally:
        Transport.get = original


@contextlib.contextmanager
def pinned_clock(day: date = PROBE_DAY):
    """Make the package believe it is `day` (the capture date), via the one seam."""
    import ghotels.client as client_mod
    saved = client_mod.today
    client_mod.today = lambda: day
    try:
        yield
    finally:
        client_mod.today = saved


def run_cli(argv: list[str], pin: bool = True) -> tuple[int, str, str]:
    """cli.main with stdout and stderr captured separately, clock pinned to
    PROBE_DAY unless `pin=False` (the live group must use the real date)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            (pinned_clock() if pin else contextlib.nullcontext()):
        try:
            code = cli.main(argv)
        except SystemExit as e:  # argparse bailing out — a finding, not the end of the suite
            code = e.code if isinstance(e.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def as_json(out: str, err: str):
    """The one JSON object a --json run printed, wherever it landed, or None."""
    for text in (out, err, out + err):
        try:
            return json.loads(text)
        except ValueError:
            continue
    return None


def client(max_requests: int = 5) -> Client:
    return Client(Transport(max_requests=max_requests, throttle=0))


def quote_page(page: str, stay: Stay, ids=None, max_requests: int = 5):
    """Client.quote against a stubbed page; returns (record|None, exception|None, calls)."""
    with get_stub(lambda n, path, params: page) as calls:
        try:
            return client(max_requests).quote(ids or parse_hotel_id(FAIRMONT_FTID), stay), None, calls
        except Exception as e:  # noqa: BLE001 - the outcome under test
            return None, e, calls


def money(text: str, *figures: str) -> bool:
    """True if every figure appears in `text` with or without thousands separators."""
    return all(f in text or f.replace(",", "") in text for f in figures)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


# ---------------------------------------------------------------------------
# [offline] §3.1 encoders
# ---------------------------------------------------------------------------


def test_encoders() -> None:
    print("\nencoders (ids.py, ts.py) — byte for byte against the probe's URLs")
    check("offline", "ftid -> CID", cid_from_ftid(FAIRMONT_FTID) == FAIRMONT_CID,
          f"got {cid_from_ftid(FAIRMONT_FTID)}")
    check("offline", "place_id -> CID (fixed64 little-endian, field 2)",
          cid_from_place_id(FAIRMONT_PLACE_ID) == FAIRMONT_CID,
          f"got {cid_from_place_id(FAIRMONT_PLACE_ID)}")
    check("offline", "CID -> entity token, unpadded base64url",
          token_from_cid(FAIRMONT_CID) == FAIRMONT_TOKEN, f"got {token_from_cid(FAIRMONT_CID)}")
    check("offline", "token -> (cid, kind) round-trips the Fairmont",
          decode_token(FAIRMONT_TOKEN) == (FAIRMONT_CID, KIND_HOTEL)
          and decode_token(FAIRMONT_FULL_TOKEN) == (FAIRMONT_CID, KIND_HOTEL),
          f"got {decode_token(FAIRMONT_TOKEN)} / {decode_token(FAIRMONT_FULL_TOKEN)}")
    check("offline", "the Samesun round-trips too (a second property, not a second run)",
          cid_from_ftid(SAMESUN_FTID) == SAMESUN_CID
          and cid_from_place_id(SAMESUN_PLACE_ID) == SAMESUN_CID
          and token_from_cid(SAMESUN_CID) == SAMESUN_TOKEN
          and decode_token(SAMESUN_TOKEN) == (SAMESUN_CID, KIND_HOTEL),
          f"got {cid_from_place_id(SAMESUN_PLACE_ID)}, {token_from_cid(SAMESUN_CID)}")
    check("offline", "a rental token decodes to kind 2 with no CID",
          decode_token(RENTAL_TOKEN) == (None, KIND_RENTAL), f"got {decode_token(RENTAL_TOKEN)}")
    check("offline", "token_from_cid refuses kind 2 (rentals cannot be built from a CID)",
          raises((IdError, ValueError), token_from_cid, FAIRMONT_CID, KIND_RENTAL))

    # ftid and place_id carry the same two numbers, so each yields the other;
    # a CID or a token carries only the second, so ftid/place_id stay None.
    for form in (FAIRMONT_FTID, FAIRMONT_PLACE_ID):
        ids = parse_hotel_id(form)
        check("offline", f"parse_hotel_id accepts {form[:12]}… and fills every form",
              ids.cid == FAIRMONT_CID and ids.token == FAIRMONT_TOKEN and ids.ftid == FAIRMONT_FTID
              and ids.place_id == FAIRMONT_PLACE_ID and ids.kind == KIND_HOTEL, f"got {ids}")
    for form in (str(FAIRMONT_CID), FAIRMONT_TOKEN, FAIRMONT_FULL_TOKEN):
        ids = parse_hotel_id(form)
        check("offline", f"parse_hotel_id accepts {form[:12]}… (cid + token; ftid/place_id are not derivable)",
              ids.cid == FAIRMONT_CID and ids.kind == KIND_HOTEL and decode_token(ids.token) == (FAIRMONT_CID, KIND_HOTEL)
              and ids.ftid is None and ids.place_id is None, f"got {ids}")
    name_error = raised(parse_hotel_id, "Fairmont Banff Springs")
    check("offline", "a bare name is refused with the gmaps command to run",
          isinstance(name_error, IdError) and "gmaps.py search" in str(name_error)
          and "Fairmont Banff Springs" in str(name_error),
          f"got {name_error!r}")

    # ts: every distinct string the probe sent, reproduced byte for byte. A
    # single wrong bit prices the wrong stay with no error (01 §ts).
    log = fixture("requests.log")
    logged = set(re.findall(r"[?&]ts=([A-Za-z0-9_-]+)", log))
    check("offline", "every ts in requests.log has a decoded expectation here",
          logged == set(CAPTURED_TS), f"unexpected: {logged ^ set(CAPTURED_TS)}")
    mismatched = {ts: encode_ts(ci, co, adults, ages, cur)
                  for ts, (ci, co, adults, ages, cur) in CAPTURED_TS.items()
                  if encode_ts(ci, co, adults, ages, cur) != ts}
    check("offline", f"encode_ts reproduces all {len(CAPTURED_TS)} captured strings byte for byte",
          not mismatched and len(CAPTURED_TS) == 18, f"mismatched {mismatched}")
    one, two = CAPTURED_TS, encode_ts
    check("offline", "one adult vs two differ only in the party field (2 adults + child 5 too)",
          two(NY_IN, NY_OUT, 2, [5], "CAD") != two(NY_IN, NY_OUT, 2, [], "CAD")
          and two(NY_IN, NY_OUT, 2, [5], "CAD") != two(NY_IN, NY_OUT, 2, [10], "CAD"),
          "a child's age must change the string")
    check("offline", "the currency field carries the ISO code",
          two(NY_IN, NY_OUT, 2, [], "USD") != two(NY_IN, NY_OUT, 2, [], "CAD"))
    check("offline", "encode_ts refuses what it cannot encode",
          raises(ValueError, two, NY_IN, NY_OUT, 0, [], "CAD")
          and raises(ValueError, two, NY_IN, NY_OUT, 2, [-1], "CAD")
          and raises(ValueError, two, NY_IN, NY_OUT, 2, [], "C$"))


# ---------------------------------------------------------------------------
# [offline] §3.2 parser exact values
# ---------------------------------------------------------------------------


def test_parser_values() -> None:
    print("\nparser on the worked example (Fairmont, 2 adults, New Year)")
    rec = record(FAIRMONT)
    raw = ds1(fixture(FAIRMONT))
    p = raw[0][6][2]

    check("offline", "name", rec.name == "Fairmont Banff Springs", f"got {rec.name!r}")
    check("offline", "kind hotel", rec.kind == KIND_HOTEL, f"got {rec.kind}")
    check("offline", "star class (label, 5)", rec.star_class == ("5-star hotel", 5), f"got {rec.star_class}")
    check("offline", "rating (4.7, 17556)", (rec.rating.score, rec.rating.reviews) == (4.7, 17556),
          f"got {rec.rating.score}, {rec.rating.reviews}")
    check("offline", "histogram row is (stars, percent, count)",
          len(rec.rating.histogram) == 5 and tuple(rec.rating.histogram[0]) == (5, 83, 14329),
          f"got {rec.rating.histogram[:1]}")
    check("offline", "per-source review summaries parse: exactly 4 sources, in page order, with counts",
          [(s.name, s.count) for s in rec.rating.sources]
          == [("Tripadvisor", 9906), ("all.accor.com", 5956), ("Trip.com", 51), ("Priceline", 50)]
          and abs(rec.rating.sources[0].score - 4.4) < 1e-6 and rec.rating.sources[0].scale == 5.0
          and abs(rec.rating.sources[3].score - 8.4) < 1e-6 and rec.rating.sources[3].scale == 10.0,
          f"got {[(s.name, s.count, s.score, s.scale) for s in rec.rating.sources]}")
    check("offline", "ids come from the record: e[9] ftid, e[26][4] place_id, the CID, and the page's own e[20] token",
          rec.ids.ftid == FAIRMONT_FTID and rec.ids.place_id == FAIRMONT_PLACE_ID
          and rec.ids.cid == FAIRMONT_CID and rec.ids.token == FAIRMONT_FULL_TOKEN
          and decode_token(rec.ids.token) == (FAIRMONT_CID, KIND_HOTEL), f"got {rec.ids}")
    check("offline", "lat/lng", rec.lat == 51.164331999999995 and rec.lng == -115.56183,
          f"got {rec.lat}, {rec.lng}")
    check("offline", "address, phone, website",
          rec.address == "405 Spray Ave, Banff, AB T1L 1J4" and rec.phone == "(403) 762-2211"
          and rec.website and "fairmont.com" in rec.website,
          f"got {rec.address!r}, {rec.phone!r}, {rec.website!r}")
    check("offline", "check-in time strips U+202F",
          rec.checkin_time == "4:00 PM" and rec.checkout_time == "12:00 PM",
          f"got {rec.checkin_time!r}, {rec.checkout_time!r}")

    check("offline", "echo parses dates, occupancy and currency",
          rec.echo.checkin == NY_IN and rec.echo.checkout == NY_OUT and rec.echo.nights == 2
          and rec.echo.adults == 2 and rec.echo.child_ages == () and rec.echo.currency == "CAD",
          f"got {rec.echo}")
    check("offline", "currency is p[15]", rec.currency == "CAD", f"got {rec.currency}")

    h = rec.headline
    check("offline", "the raw lead p[1][3] is null (so incl-tax cannot have come from it)",
          p[1][3] is None and p[1][2] == 1282.75, f"p[1] = {p[1]}")
    check("offline", "headline.nightly.ex_tax == 1282.75 from p[1][2]",
          h is not None and h.nightly.ex_tax == 1282.75, f"got {h and h.nightly}")
    check("offline", "headline.nightly.incl_tax == 1452.375 from the matched row's o[12][4][3]",
          h is not None and h.nightly.incl_tax == 1452.375, f"got {h and h.nightly}")
    check("offline", "headline seller is dealbase by float match, matched_row True",
          h is not None and h.seller == "dealbase.com" and h.partner_id == 1930230291 and h.matched_row,
          f"got {h}")
    check("offline", "the dealbase row's stay incl-tax is 2904.75",
          h is not None and h.stay is not None and h.stay.incl_tax == 2904.75 and h.stay.ex_tax == 2565.5,
          f"got {h and h.stay}")
    check("offline", "breakdown == [2435.625, 339.25, 129.875, 2904.75] (Google's own, not computed)",
          rec.breakdown is not None
          and (rec.breakdown.base, rec.breakdown.taxes, rec.breakdown.fees, rec.breakdown.total)
          == (2435.625, 339.25, 129.875, 2904.75), f"got {rec.breakdown}")

    partners = {s.partner_id for s in rec.sellers}
    check("offline", "the seller union is exactly 11 unique partner ids",
          len(rec.sellers) == 11 and partners == FAIRMONT_PARTNERS, f"got {sorted(partners)}")
    check("offline", "9 two-basis rows and 2 single-figure rows",
          sum(s.basis == "both" for s in rec.sellers) == 9 and sum(s.basis == "single" for s in rec.sellers) == 2,
          f"got {[(s.seller, s.basis) for s in rec.sellers]}")
    check("offline", "unparsed_rows is 0 on a clean page", rec.unparsed_rows == 0, f"got {rec.unparsed_rows}")

    by_name = {s.seller: s for s in rec.sellers}
    direct = by_name.get("Fairmont Banff Springs")
    check("offline", "Fairmont-direct's free-cancellation raw keeps the U+202F",
          direct is not None and direct.free_cancellation.shown
          and direct.free_cancellation.raw == [1, "Jun 14", "6:00 PM", "6/14"],
          f"got {direct and direct.free_cancellation}")
    check("offline", "and deadline_text normalises it",
          direct is not None and direct.free_cancellation.deadline_text is not None
          and " " not in direct.free_cancellation.deadline_text
          and "Jun 14" in direct.free_cancellation.deadline_text
          and "6:00 PM" in direct.free_cancellation.deadline_text,
          f"got {direct and direct.free_cancellation.deadline_text!r}")
    dealbase = by_name.get("dealbase.com")
    check("offline", "dealbase shows no deadline: shown False, which is not 'non-refundable'",
          dealbase is not None and dealbase.free_cancellation.shown is False
          and dealbase.free_cancellation.deadline_text is None, f"got {dealbase and dealbase.free_cancellation}")
    check("offline", "own_site is true only for the row named like the hotel",
          direct is not None and direct.own_site and direct.partner_id == 76
          and sum(s.own_site for s in rec.sellers) == 1, f"got {[s.seller for s in rec.sellers if s.own_site]}")
    # own_site is name equality with e[1], not "partner id 76": rename the
    # hotel to a seller with another id and the flag must follow the name.
    renamed = copy.deepcopy(raw)
    renamed[0][1] = "dealbase.com"
    flagged = [s.partner_id for s in entity_record(with_ds1(fixture(FAIRMONT), renamed)).sellers if s.own_site]
    check("offline", "own_site follows the hotel's name (e[1]), not a partner id",
          flagged == [1930230291], f"got {flagged}")
    check("offline", "the direct row lists exactly its 7 room names, o[7] order, first the DELUXE King",
          direct is not None and len(direct.rooms) == 7
          and direct.rooms[0] == "DELUXE King - 350sf, 33sm, spacious guest room."
          and direct.rooms[-1].startswith("FAIRMONT GOLD JUNIOR SUITE King")
          and sum(1 for s in rec.sellers if s.rooms) == 4,   # rooms live on p[2] rows only
          f"got {direct and direct.rooms[:3]}, {sum(1 for s in rec.sellers if s.rooms)} rows with rooms")

    # A time-less deadline: [1, "Jun 14", null, "6/14"] renders without inventing one.
    # The direct row sits in all four slots and the union keeps the fullest
    # copy, so every copy is mutated.
    mutated = copy.deepcopy(raw)
    for slot in (2, 12, 21, 22):
        for o in mutated[0][6][2][slot] or []:
            if o[0][1] == 76:
                o[12][12][1] = [1, "Jun 14", None, "6/14"]
    timeless = entity_record(with_ds1(fixture(FAIRMONT), mutated))
    fc = next(s for s in timeless.sellers if s.partner_id == 76).free_cancellation
    check("offline", "a deadline with a null time renders 'Jun 14 (time not shown)'",
          fc.shown and fc.deadline_text == "Jun 14 (time not shown)", f"got {fc}")

    # o[9] (room count) null on a p[2] row is ordinary, not a crash.
    mutated = copy.deepcopy(raw)
    mutated[0][6][2][2][0][9] = None
    check("offline", "a null o[9] on a p[2] row does not crash",
          not raises(Exception, entity_record, with_ds1(fixture(FAIRMONT), mutated)))


# ---------------------------------------------------------------------------
# [offline] §3.3 headline, minimum, tie rule, union
# ---------------------------------------------------------------------------


def test_headline_and_minimum() -> None:
    print("\nheadline by float match, minimum by comparable(), the tie rule")
    fairmont, samesun = record(FAIRMONT), record(SAMESUN)
    fp = ds1(fixture(FAIRMONT))[0][6][2]
    sp = ds1(fixture(SAMESUN))[0][6][2]

    # Positional mutants: the lead is p[22][1] on the Fairmont and p[22][0] on
    # the Samesun. Both are asserted so neither "select p[22][k]" survives.
    check("offline", "on the Fairmont the lead float sits in p[22][1], not [0]",
          fp[22][1][12][4][2] == 1282.75 and fp[22][0][12][4][2] != 1282.75)
    check("offline", "on the Samesun the lead float sits in p[22][0], not [1]",
          sp[22][0][12][4][2] == 246.7099 and sp[22][1][12][4][2] != 246.7099)
    check("offline", "Fairmont headline is dealbase (p[22][1]) by float match",
          fairmont.headline and fairmont.headline.seller == "dealbase.com" and fairmont.headline.matched_row)
    check("offline", "Samesun headline is Bluepillow (p[22][0]), a single-figure row, by float match on slot [2]",
          samesun.headline and samesun.headline.seller == "Bluepillow.ca"
          and samesun.headline.partner_id == 599653573 and samesun.headline.matched_row
          and samesun.headline.nightly.basis == "single" and samesun.headline.nightly.amount == 246.7099
          and samesun.headline.nightly.incl_tax is None,
          f"got {samesun.headline}")

    # Tie rule (04 §6.2): BusinessHotels' single 1452.3022 undercuts dealbase's
    # 1452.375 by seven cents — less than TIE_MARGIN — so dealbase is cheapest.
    row, tie = cheapest_seller(fairmont.sellers, "incl")
    check("offline", "Fairmont cheapest under incl is dealbase (two-basis), tie flagged",
          row is not None and row.seller == "dealbase.com" and tie is True, f"got {row and row.seller}, tie={tie}")
    business = next(s for s in fairmont.sellers if s.seller == "BusinessHotels.com")
    check("offline", "BusinessHotels' single figure really is within the margin",
          business.nightly.basis == "single" and business.nightly.amount == 1452.3022
          and 0 < 1452.375 - business.nightly.amount < TIE_MARGIN)
    check("offline", "TIE_MARGIN is one unit of currency, not five cents", TIE_MARGIN == 1.00)
    row_ex, _ = cheapest_seller(fairmont.sellers, "ex")
    check("offline", "Fairmont cheapest under ex is dealbase 1282.75; single rows have no ex figure",
          row_ex is not None and row_ex.seller == "dealbase.com"
          and comparable(business.nightly, "ex") is None and comparable(business.nightly, "incl") == 1452.3022)

    # The Samesun: Bluepillow's single 246.71 undercuts the best two-basis row
    # (Super.com 803.63) by far — so it IS cheapest, and never "incl. tax".
    row, tie = cheapest_seller(samesun.sellers, "incl")
    check("offline", "Samesun cheapest under incl is Bluepillow at 246.7099, basis single, no tie",
          row is not None and row.seller == "Bluepillow.ca" and row.basis == "single"
          and comparable(row.nightly, "incl") == 246.7099 and tie is False,
          f"got {row and (row.seller, row.basis)}, tie={tie}")
    super_ = next(s for s in samesun.sellers if s.seller == "Super.com")
    check("offline", "Super.com 803.63434 is the best two-basis row (what excluding singles would report)",
          super_.nightly.incl_tax == 803.63434
          and min(comparable(s.nightly, "incl") for s in samesun.sellers if s.basis == "both") == 803.63434)
    row_ex, _ = cheapest_seller(samesun.sellers, "ex")
    check("offline", "under ex the Samesun cheapest is Trip.com 722.21875 — a p[2]-only row",
          row_ex is not None and row_ex.seller == "Trip.com" and row_ex.nightly.ex_tax == 722.21875,
          f"got {row_ex and (row_ex.seller, row_ex.nightly.ex_tax)}")

    # Single-figure rows from the extract: [2] is the all-in figure.
    extract = json.loads(fixture("one_basis_rows.json"))
    check("offline", "the extract holds Amimir, BusinessHotels and Bluepillow",
          [o[0][0] for o in extract["rows"]] == ["Amimir.com", "BusinessHotels.com", "Bluepillow.ca"]
          and extract["rows"][0][12][4] == ["$1,453", None, 1452.8091, None, 1453])
    amimir = next(s for s in fairmont.sellers if s.seller == "Amimir.com")
    check("offline", "a single-figure row parses as basis single with amount, ex/incl None",
          amimir.basis == "single" and amimir.nightly.amount == 1452.8091
          and amimir.nightly.ex_tax is None and amimir.nightly.incl_tax is None, f"got {amimir.nightly}")
    check("offline", "its stay slot is single too (2905.6182)",
          amimir.stay.basis == "single" and amimir.stay.amount == 2905.6182
          and business.stay.amount == 2904.6045, f"got {amimir.stay}, {business.stay}")
    check("offline", "the Samesun p[44] parses with a zero tax line",
          samesun.breakdown is not None
          and (samesun.breakdown.base, samesun.breakdown.taxes, samesun.breakdown.fees, samesun.breakdown.total)
          == (435.81625, 0, 57.603542, 493.4198), f"got {samesun.breakdown}")

    # The #9 digest wrapper: a two-basis row cheaper than the headline.
    p9 = record(WRAP_P9)
    row, tie = cheapest_seller(p9.sellers, "incl")
    check("offline", "on the #9 digest the headline is dealbase 1053 but the cheapest two-basis row is the hotel itself at 1022.5625",
          p9.headline and p9.headline.seller == "dealbase.com" and p9.headline.nightly.ex_tax == 1053
          and p9.headline.nightly.incl_tax == 1192.1875
          and row is not None and row.seller == "Fairmont Banff Springs" and row.own_site
          and row.nightly.ex_tax == 1022.5625 and row.nightly.incl_tax == 1148 and tie is False,
          f"headline {p9.headline and p9.headline.seller}, cheapest {row and row.seller}")
    check("offline", "the digest wrapper's 27 sellers all parse (p[12] null, p[2] four rows)",
          len(p9.sellers) == 27 and p9.unparsed_rows == 0, f"got {len(p9.sellers)}, {p9.unparsed_rows}")


def test_seller_union() -> None:
    print("\nthe seller union p[2] ∪ p[12] ∪ p[21] ∪ p[22]")
    for name, want in ((FAIRMONT, FAIRMONT_PARTNERS), (SAMESUN, SAMESUN_PARTNERS),
                       (PLUS270, PLUS270_PARTNERS), (CHILD5, CHILD5_PARTNERS), (RENTAL, RENTAL_PARTNERS)):
        rec = record(name)
        got = {s.partner_id for s in rec.sellers}
        check("offline", f"{name}: union of exactly {len(want)} partners",
              got == want and len(rec.sellers) == len(want), f"got {sorted(got)}")
    samesun = record(SAMESUN)
    check("offline", "Samesun: Trip.com (84) is in the union although absent from p[21]",
          any(s.partner_id == 84 and s.seller == "Trip.com" for s in samesun.sellers)
          and 84 not in {o[0][1] for o in ds1(fixture(SAMESUN))[0][6][2][21]})
    plus = record(PLUS270)
    check("offline", "plus270: headline None, breakdown None, three priced offers",
          plus.headline is None and plus.breakdown is None and len(plus.sellers) == 3
          and all(s.nightly.ex_tax == 1254.1875 and s.nightly.incl_tax == 1410.3125 for s in plus.sellers),
          f"got {plus.headline}, {plus.breakdown}, {[(s.seller, s.nightly) for s in plus.sellers]}")
    check("offline", "plus270: echo matches its request; p[15] is null so record.currency is None but every price carries the echo's CAD",
          plus.echo.matches(PLUS270_STAY) and plus.echo.currency == "CAD" and plus.currency is None
          and len(plus.sellers) == 3 and all(s.nightly.currency == "CAD" and s.stay.currency == "CAD" for s in plus.sellers),
          f"got echo {plus.echo}, currency {plus.currency!r}")
    check("offline", "plus270's p is 43 long with slots 21/22 absent — parsing must not raise",
          len(ds1(fixture(PLUS270))[0][6][2]) == 43)
    empty = record(WRAP_EMPTY)
    check("offline", "all four seller slots null -> empty union, echo still matches (the exit-1 signature)",
          empty.sellers == () and empty.echo.matches(PLUS270_STAY) and empty.headline is None,
          f"got {len(empty.sellers)} sellers")
    rental = record(RENTAL)
    check("offline", "rental: p[2]/p[12] null (not []), rows only in p[21]/p[22]",
          ds1(fixture(RENTAL))[0][6][2][2] is None and len(rental.sellers) == 3)
    canmore = next((s for s in rental.sellers if s.partner_id == 2084533977), None)
    check("offline", "rental: Canmore Rental Management is a single-figure row (505.605) beside two two-basis rows",
          canmore is not None and canmore.basis == "single" and canmore.nightly.amount == 505.605
          and canmore.stay.amount == 1011.21 and sum(s.basis == "both" for s in rental.sellers) == 2,
          f"got {[(s.seller, s.basis) for s in rental.sellers]}")
    # Stay comparables (04 §6.2): the same rule ranks stays.
    fairmont = record(FAIRMONT)
    business = next(s for s in fairmont.sellers if s.seller == "BusinessHotels.com")
    check("offline", "BusinessHotels' stay comparable is 2904.6045 under incl and None under ex",
          comparable(business.stay, "incl") == 2904.6045 and comparable(business.stay, "ex") is None)
    stays = sorted((comparable(s.stay, "incl"), s.seller) for s in fairmont.sellers)
    check("offline", "ranking stays by comparable() puts dealbase (2904.75) after BusinessHotels' single 2904.6045 by raw value",
          stays[0] == (2904.6045, "BusinessHotels.com") and stays[1] == (2904.75, "dealbase.com"),
          f"got {stays[:3]}")


# ---------------------------------------------------------------------------
# [offline] §3.4 the echo check
# ---------------------------------------------------------------------------


def test_echo() -> None:
    print("\nthe echo check — a price for a different stay is exit 3, never 1")
    wrong = Stay(date(2026, 9, 1), date(2026, 9, 3), 2, (), "CAD")
    rec, err, calls = quote_page(fixture(FAIRMONT), wrong)
    check("offline", "Fairmont page against a 2026-09-01→03 request raises PayloadError",
          rec is None and isinstance(err, PayloadError), f"got {err!r}")
    check("offline", "and the message names both stays",
          err is not None and "2026-09-01" in str(err) and "2026-12-31" in str(err), f"got {err}")
    check("offline", "and it took exactly one request", len(calls) == 1, f"got {len(calls)}")

    rec, err, _ = quote_page(fixture(FAIRMONT), NY)
    check("offline", "the same page against the stay it priced parses", rec is not None and err is None,
          f"raised {err!r}")

    # Occupancy: the page must have priced the party asked for.
    rec, err, _ = quote_page(fixture(WRAP_1AD), NY_CHILD5)
    check("offline", "child 5 requested against a one-adult page is a mismatch",
          rec is None and isinstance(err, PayloadError), f"got {err!r}")
    rec, err, _ = quote_page(fixture(WRAP_1AD), Stay(NY_IN, NY_OUT, 1, (), "CAD"))
    check("offline", "one adult against the one-adult page matches", rec is not None and rec.echo.adults == 1,
          f"got {err!r}")
    rec, err, _ = quote_page(fixture(CHILD5), NY)
    check("offline", "two adults requested against the child-5 page is a mismatch",
          rec is None and isinstance(err, PayloadError), f"got {err!r}")
    rec, err, _ = quote_page(fixture(CHILD5), NY_CHILD5)
    check("offline", "two adults + child 5 against it matches, echo child_ages (5,)",
          rec is not None and rec.echo.child_ages == (5,) and rec.echo.adults == 2, f"got {err!r}")

    # Currency is step 4b: a refusal (2), separate from the echo (3).
    rec, err, _ = quote_page(fixture(FAIRMONT), Stay(NY_IN, NY_OUT, 2, (), "USD"))
    check("offline", "USD requested against a CAD page is QueryError (exit 2), naming CAD",
          rec is None and isinstance(err, QueryError) and "CAD" in str(err) and not isinstance(err, PayloadError),
          f"got {err!r}")
    rec, err, _ = quote_page(fixture(WRAP_USD), Stay(NY_IN, NY_OUT, 2, (), "USD"))
    check("offline", "USD against the USD page parses with currency USD",
          rec is not None and rec.currency == "USD" and rec.echo.currency == "USD"
          and rec.headline and rec.headline.nightly.ex_tax == 925.5385, f"got {err!r}")

    # A null occupancy echo on a request that sent ts is a mismatch.
    raw = ds1(fixture(FAIRMONT))
    raw[0][6][1][13] = None
    rec, err, _ = quote_page(with_ds1(fixture(FAIRMONT), raw), NY)
    check("offline", "a null [13] occupancy echo is a mismatch (the client always sends ts)",
          rec is None and isinstance(err, PayloadError), f"got {err!r}")
    check("offline", "Echo.matches itself treats child_ages None as a mismatch",
          not Echo(NY_IN, NY_OUT, 2, 2, None, "CAD").matches(NY)
          and Echo(NY_IN, NY_OUT, 2, 2, (), "CAD").matches(NY))
    check("offline", "dates compare as dates, not strings: a one-day shift is a mismatch",
          not Echo(NY_IN + timedelta(days=1), NY_OUT, 1, 2, (), "CAD").matches(NY)
          and not Echo(NY_IN, NY_OUT + timedelta(days=1), 3, 2, (), "CAD").matches(NY))

    # The exit-1 path: echo matches, union empty.
    rec, err, _ = quote_page(fixture(WRAP_EMPTY), PLUS270_STAY)
    check("offline", "all seller slots null with a matching echo returns an empty record (not an error)",
          rec is not None and rec.sellers == () and err is None, f"got {err!r}")
    # And the echo check is not skipped when p[1] is null.
    rec, err, _ = quote_page(fixture(WRAP_EMPTY), NY)
    check("offline", "the echo is still checked when p[1] is null",
          rec is None and isinstance(err, PayloadError), f"got {err!r}")


# ---------------------------------------------------------------------------
# [offline] §3.5 validators
# ---------------------------------------------------------------------------


def test_validators() -> None:
    print("\nup-front validators — refused before any request")
    today = PROBE_DAY
    d = lambda n: today + timedelta(days=n)  # noqa: E731

    def refused(ci, co, adults=2, ages=(), cur="CAD"):
        return raises(QueryError, validate_stay, ci, co, adults, list(ages), cur, today=today)

    check("offline", "past check-in is refused", refused(d(-1), d(1)))
    check("offline", "checkout == checkin is refused", refused(d(10), d(10)))
    check("offline", "checkout < checkin is refused", refused(d(10), d(9)))
    check("offline", "31 nights is refused", refused(d(10), d(41)))
    check("offline", "30 nights is allowed (inclusive)", not refused(d(10), d(40)))
    check("offline", "331 days out is refused", refused(d(331), d(332)))
    check("offline", "330 days out is allowed (inclusive)", not refused(d(330), d(331)))
    check("offline", "same-day check-in is allowed (the echo check guards it)", not refused(d(0), d(1)))
    check("offline", "adults 0 and 9 are refused, 1 and 8 allowed",
          refused(d(10), d(12), adults=0) and refused(d(10), d(12), adults=9)
          and not refused(d(10), d(12), adults=1) and not refused(d(10), d(12), adults=8))
    check("offline", "child age 18 is refused, 17 and 0 allowed",
          refused(d(10), d(12), ages=[18]) and not refused(d(10), d(12), ages=[17])
          and not refused(d(10), d(12), ages=[0]))
    check("offline", "seven children are refused, six allowed",
          refused(d(10), d(12), ages=[5] * 7) and not refused(d(10), d(12), ages=[5] * 6))
    check("offline", "a currency outside the catalogue is refused, one inside allowed",
          refused(d(10), d(12), cur="XXX") and not refused(d(10), d(12), cur="USD")
          and not refused(d(10), d(12), cur="JPY"))
    stay = validate_stay(d(10), d(12), 2, [5], "CAD", today=today)
    check("offline", "a valid stay comes back as a Stay with nights computed",
          isinstance(stay, Stay) and stay.nights == 2 and stay.child_ages == (5,))

    # The clock seam: one definition, everything else goes through it.
    import ghotels.client as client_mod
    check("offline", "ghotels.client.today() is the package clock",
          callable(getattr(client_mod, "today", None)) and client_mod.today() == date.today())
    uses = {}
    for fname in sorted(os.listdir(os.path.join(HERE, "ghotels"))):
        if fname.endswith(".py"):
            with open(os.path.join(HERE, "ghotels", fname), encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    code_part = line.split("#", 1)[0]
                    if "date.today()" in code_part and not code_part.strip().startswith(('"""', "'", "`")):
                        uses[f"{fname}:{lineno}"] = line.strip()
    check("offline", "date.today() appears in ghotels/ only inside client.today()",
          len(uses) == 1 and next(iter(uses)).startswith("client.py")
          and next(iter(uses.values())) == "return date.today()", f"got {uses}")
    with pinned_clock(date(2030, 1, 1)):
        check("offline", "pinning the seam moves the CLI's clock",
              cli._today() == date(2030, 1, 1), f"got {cli._today()}")

    # The same refusals through the CLI, each with zero requests issued.
    ci, co = NY_IN.isoformat(), NY_OUT.isoformat()
    bad = {
        "past check-in": ["quote", FAIRMONT_FTID, "--checkin", (PROBE_DAY - timedelta(days=1)).isoformat(), "--checkout", co],
        "checkout == checkin": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", ci],
        "checkout < checkin": ["quote", FAIRMONT_FTID, "--checkin", co, "--checkout", ci],
        "31 nights": ["quote", FAIRMONT_FTID, "--checkin", "2026-12-01", "--checkout", "2027-01-01"],
        "331 days out": ["quote", FAIRMONT_FTID, "--checkin", "+331", "--checkout", "+332"],
        "--adults 0": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--adults", "0"],
        "--adults 9": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--adults", "9"],
        "--child-age 18": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--child-age", "18"],
        "seven children": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co] + ["--child-age", "5"] * 7,
        "--limit 0": ["shortlist", "--hotel", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--limit", "0"],
        "--limit 11": ["shortlist", "--hotel", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--limit", "11"],
        "--days 22": ["cheapest", FAIRMONT_FTID, "--checkin", ci, "--nights", "2", "--days", "22"],
        "--step 0": ["cheapest", FAIRMONT_FTID, "--checkin", ci, "--nights", "2", "--step", "0"],
        "--under 0": ["watch", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--under", "0"],
        "--max-requests 41": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--max-requests", "41"],
        "a currency not in the catalogue": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--currency", "XXX"],
    }
    # The phrase each refusal must carry (client.validate_stay / cli's
    # argparse types), so a refusal for the wrong reason cannot pass.
    phrase = {
        "past check-in": "is in the past",
        "checkout == checkin": "is not after check-in",
        "checkout < checkin": "is not after check-in",
        "31 nights": "31 nights is more than the 30",
        "331 days out": "is more than 330 days out",
        "--adults 0": "--adults must be between 1 and 8 (got 0)",
        "--adults 9": "--adults must be between 1 and 8 (got 9)",
        "--child-age 18": "--child-age must be between 0 and 17 (got 18)",
        "seven children": "at most 6 children",
        "--limit 0": "--limit must be between 1 and 10 (got 0)",
        "--limit 11": "--limit must be between 1 and 10 (got 11)",
        "--days 22": "--days must be between 1 and 21 (got 22)",
        "--step 0": "--step must be between 1 and 21 (got 0)",
        "--under 0": "--under must be above 0",
        "--max-requests 41": "--max-requests must be between 1 and 40",
        "a currency not in the catalogue": "not one Google Hotels offers",
    }
    check("offline", "every refusal case has a documented phrase to assert", set(phrase) == set(bad),
          f"unmatched {set(phrase) ^ set(bad)}")
    for label, argv in bad.items():
        with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
            code, out, err = run_cli(argv + ["--json"])
        payload = as_json(out, err)
        check("offline", f"{label} -> exit 2 before any request, saying '{phrase[label]}'",
              code == 2 and len(calls) == 0 and payload is not None and payload.get("ok") is False
              and isinstance(payload.get("error"), str) and phrase[label] in payload["error"],
              f"exit {code}, {len(calls)} requests, {payload}")
    good = {
        "30 nights": ["quote", FAIRMONT_FTID, "--checkin", "2026-12-01", "--checkout", "2026-12-31"],
        "330 days out": ["quote", FAIRMONT_FTID, "--checkin", "+330", "--checkout", "+331"],
        "--limit 10": ["shortlist", "--hotel", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--limit", "10"],
        "--days 21": ["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "21"],
    }
    for label, argv in good.items():
        with quote_stub(lambda n, ids, stay: aligned(record(FAIRMONT), stay)):
            code, out, err = run_cli(argv + ["--json"])
        check("offline", f"{label} is accepted (inclusive boundary)", code == 0, f"exit {code}: {err[:200]}")


# ---------------------------------------------------------------------------
# [offline] §3.6 block and unknown-entity detection
# ---------------------------------------------------------------------------


def test_block_and_unknown() -> None:
    print("\nblock and unknown-entity detection")
    unknown = fixture(UNKNOWN)
    check("offline", "the unknown-entity page carries ds:2 and a ds:1 block with errorHasStatus",
          RESULTS_MARKER in unknown and "key: 'ds:1'" in unknown and "errorHasStatus: true" in unknown)
    check("offline", "entity_record raises UnknownEntity on it (not PayloadError)",
          raises(UnknownEntity, entity_record, unknown) and not issubclass(UnknownEntity, PayloadError))
    rec, err, _ = quote_page(unknown, NY)
    check("offline", "Client.quote raises UnknownEntity with 'no such hotel id'",
          isinstance(err, UnknownEntity) and "no such hotel id" in str(err), f"got {err!r}")

    # Mutant: "unknown = empty <title>". A healthy page with its title stripped
    # must still parse.
    untitled = re.sub(r"<title>.*?</title>", "<title></title>", fixture(FAIRMONT), flags=re.S)
    check("offline", "a healthy page with an empty <title> still parses (title is not the signature)",
          not raises(Exception, entity_record, untitled))
    # Mutant: "unknown = ds:1 absent". The block is present on this page; a
    # parser keyed on presence would index into [5] and raise the wrong thing.
    check("offline", "the ds:1 block is present on the unknown page and decodes to [5] (so 'absent' cannot be the rule)",
          blocks(unknown).get("ds:1") == [5], f"got {blocks(unknown).get('ds:1')!r}")
    check("offline", "the synthetic all-slots-null page is a record, never UnknownEntity",
          not raises(UnknownEntity, entity_record, fixture(WRAP_EMPTY)))
    # The signature is "a list whose ONLY element is an INT": ds:1 = [record]
    # (wrapper trimmed) still parses, and ds:1 = ["5"] is a shape drift.
    from ghotels.parse import is_error_block
    check("offline", "is_error_block: [5] and [3] yes; [], [5, null], ['5'], [true], [[…]] no",
          is_error_block([5]) and is_error_block([3]) and not is_error_block([]) and not is_error_block([5, None])
          and not is_error_block(["5"]) and not is_error_block([True]) and not is_error_block([[1]]) and not is_error_block(5))
    trimmed = entity_record(with_ds1(fixture(FAIRMONT), [ds1(fixture(FAIRMONT))[0]]))
    check("offline", "ds:1 = [record] (a one-element wrapper) parses as the Fairmont, not as unknown",
          trimmed.name == "Fairmont Banff Springs" and len(trimmed.sellers) == 11, f"got {trimmed.name!r}")
    check("offline", "ds:1 = ['5'] is PayloadError, not UnknownEntity",
          raises(PayloadError, entity_record, with_ds1(fixture(FAIRMONT), ["5"]))
          and not raises(UnknownEntity, entity_record, with_ds1(fixture(FAIRMONT), ["5"])))

    check("offline", "a page without ds:2 looks blocked", looks_blocked(fixture(BLOCKED)))
    no_ds2 = re.sub(r"<script[^>]*>AF_initDataCallback\(\{key: 'ds:2'.*?\);</script>", "", fixture(FAIRMONT), flags=re.S)
    check("offline", "a hotel page with its ds:2 block removed looks blocked",
          looks_blocked(no_ds2) and not looks_blocked(fixture(FAIRMONT)))
    rec, err, _ = quote_page(no_ds2, NY)
    check("offline", "and Client.quote reports it as HotelsHTTPError (exit 3), not as no rates",
          rec is None and isinstance(err, HotelsHTTPError), f"got {err!r}")
    rec, err, _ = quote_page(fixture(BLOCKED), NY)
    check("offline", "the consent page is exit-3 class too", rec is None and isinstance(err, HotelsHTTPError),
          f"got {err!r}")
    healthy = [FAIRMONT, SAMESUN, PLUS270, RENTAL, CHILD5]
    check("offline", f"none of the {len(healthy)} healthy fixtures looks blocked",
          len(healthy) == 5 and not any(looks_blocked(fixture(n)) for n in healthy))
    check("offline", "and every one of them contains 'recaptcha' (a phrase list would block them all)",
          len(healthy) == 5 and all("recaptcha" in fixture(n) for n in healthy))

    # A 302: raised with its Location, never followed. Stubbed one layer below
    # Transport.get — the session — so get() runs for real.
    transport = Transport(max_requests=5, throttle=0)
    if transport._session is None:
        check("offline", "302 handling (requests path)", False, "requests is not installed here")
    else:
        seen = []

        def fake_get(url, **kwargs):
            seen.append((url, kwargs.get("allow_redirects")))
            return types.SimpleNamespace(status_code=302, headers={"Location": "https://www.google.com/travel/search?q=Banff"},
                                         text="", url=url)

        transport._session.get = fake_get
        err = raised(transport.get, "/travel/hotels/entity/x", {"hl": "en"})
        check("offline", "a 302 is HotelsHTTPError naming the Location",
              isinstance(err, HotelsHTTPError) and "/travel/search" in str(err), f"got {err!r}")
        check("offline", "and it is never followed: one call, allow_redirects=False",
              len(seen) == 1 and seen[0][1] is False, f"got {seen}")
        check("offline", "and that one attempt was charged", transport.requests_made == 1)


# ---------------------------------------------------------------------------
# [offline] §3.7 shape guards
# ---------------------------------------------------------------------------


def test_guards() -> None:
    print("\nshape guards (the silent-wrong-answer defence)")
    html = fixture(FAIRMONT)
    raw = ds1(html)

    shifted = with_ds1(html, [None] + raw)
    check("offline", "a ds:1 shifted by one raises PayloadError rather than reporting wrong prices",
          raises(PayloadError, entity_record, shifted))

    one_bad = copy.deepcopy(raw)
    idx = next(i for i, o in enumerate(one_bad[0][6][2][21]) if o[0][1] == 558573628)  # Reserving: only in p[21]
    one_bad[0][6][2][21][idx] = "not-a-row"
    rec = entity_record(with_ds1(html, one_bad))
    check("offline", "one broken OTA row is counted in unparsed_rows and the other 10 kept",
          rec.unparsed_rows == 1 and len(rec.sellers) == 10 and 558573628 not in {s.partner_id for s in rec.sellers},
          f"got {rec.unparsed_rows} unparsed, {len(rec.sellers)} kept")

    all_bad = copy.deepcopy(raw)
    for slot in (2, 12, 21, 22):
        all_bad[0][6][2][slot] = ["not-a-row"] * len(all_bad[0][6][2][slot])
    check("offline", "every row broken raises PayloadError (>25 % unparseable)",
          raises(PayloadError, entity_record, with_ds1(html, all_bad)))

    removed = re.sub(r"<script[^>]*>AF_initDataCallback\(\{key: 'ds:1'.*?\);</script>", "", html, flags=re.S)
    check("offline", "ds:1 wholly removed is PayloadError, not UnknownEntity",
          raises(PayloadError, entity_record, removed) and not raises(UnknownEntity, entity_record, removed))
    rec, err, _ = quote_page(removed, NY)
    check("offline", "and through the client it is still PayloadError (exit 3)", isinstance(err, PayloadError),
          f"got {err!r}")

    no_breakdown = copy.deepcopy(raw)
    no_breakdown[0][6][2][44] = None
    rec = entity_record(with_ds1(html, no_breakdown))
    check("offline", "p[44] removed -> breakdown None and the quote still succeeds with 11 sellers",
          rec.breakdown is None and len(rec.sellers) == 11 and rec.headline is not None,
          f"got {rec.breakdown}, {len(rec.sellers)}")
    check("offline", "a page with no blocks at all raises PayloadError",
          raises((PayloadError, HotelsHTTPError), entity_record, "<html><body>nothing</body></html>"))


def test_parser_hardening() -> None:
    """Shapes the independent parser review found the suite did not pin down.

    Each is a plausible drift of one slot, and each used to produce either a
    raw TypeError (a traceback, exit 1) or — worse — a smaller, plausible
    answer: a 12-slot price table parsed to four sellers with no headline.
    """
    print("\nparser hardening (the review's five gaps and the matching fixes)")
    html = fixture(FAIRMONT)
    raw = ds1(html)

    def mutated(fn):
        payload = copy.deepcopy(raw)
        fn(payload)
        return with_ds1(html, payload)

    def every_row(payload, fn):
        for slot in (2, 12, 21, 22):
            for o in payload[0][6][2][slot] or []:
                fn(o)

    def rows_named(payload, partner, fn):
        every_row(payload, lambda o: fn(o) if o[0][1] == partner else None)

    def error_type(page):
        try:
            entity_record(page)
        except Exception as e:  # noqa: BLE001 - the type is the assertion
            return type(e)
        return None

    # 1. A "single" row is one whose incl-tax STRING is null too. An incl
    #    string beside a null incl float is a shape we do not understand.
    page = mutated(lambda p: rows_named(p, 1745103910, lambda o: o[12][4].__setitem__(1, "$1,452")))
    rec = entity_record(page)
    check("offline", "a row with an incl-tax string but no incl float is unparsed, not single",
          rec.unparsed_rows >= 1 and 1745103910 not in {s.partner_id for s in rec.sellers} and len(rec.sellers) == 10,
          f"got unparsed {rec.unparsed_rows}, {len(rec.sellers)} sellers")

    # 2. A record shorter than E_MIN_LEN.
    check("offline", "ds:1 = [e[:26]] (a short record) is PayloadError",
          error_type(mutated(lambda p: p.__setitem__(0, p[0][:26]))) is PayloadError)

    # 3. A price table truncated to 12 slots: the worst survivor — it parsed to
    #    four sellers and no headline, a silent under-report.
    check("offline", "p truncated to 12 slots is PayloadError, never four sellers",
          error_type(mutated(lambda p: p[0][6].__setitem__(2, p[0][6][2][:12]))) is PayloadError)

    # 4. A bool where the partner id belongs.
    page = mutated(lambda p: rows_named(p, 1930230291, lambda o: o[0].__setitem__(1, True)))
    rec = entity_record(page)
    dealbase = [s for s in rec.sellers if s.seller == "dealbase.com"]
    check("offline", "o[0][1] = true parses as partner_id None, and the row is kept",
          len(dealbase) == 1 and dealbase[0].partner_id is None and len(rec.sellers) == 11,
          f"got {[(s.seller, s.partner_id) for s in dealbase]}, {len(rec.sellers)} sellers")

    # 5. A star-class slot of the wrong shape.
    check("offline", "e[3] = ['x'] is PayloadError",
          error_type(mutated(lambda p: p[0].__setitem__(3, ["x"]))) is PayloadError)

    # Drifted NON-price blocks degrade (lead's ruling): the field empties, the
    # prices are untouched, and the CLI still answers with exit 0.
    baseline = record(FAIRMONT)
    for label, page, field in (
        ("e[7][1] = [4.7] (histogram)", mutated(lambda p: p[0][7].__setitem__(1, [4.7])), "histogram"),
        ("e[7][4] = 7 (sources)", mutated(lambda p: p[0][7].__setitem__(4, 7)), "sources"),
    ):
        exc = error_type(page)
        rec = entity_record(page) if exc is None else None
        check("offline", f"{label} degrades: no exception, rating.{field} == (), sellers and headline unchanged",
              exc is None and getattr(rec.rating, field) == ()
              and [s.to_dict() for s in rec.sellers] == [s.to_dict() for s in baseline.sellers]
              and rec.headline.to_dict() == baseline.headline.to_dict()
              and rec.rating.score == 4.7,
              f"raised {exc and exc.__name__}")
        if rec is not None:
            with quote_stub(lambda n, ids, stay, rec=rec: aligned(rec, stay)):
                code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42", "--json"])
            check("offline", f"  and the CLI still answers exit 0", code == 0 and (as_json(out, err) or {}).get("ok") is True,
                  f"exit {code}: {err[:200]}")
    rental_raw = ds1(fixture(RENTAL))
    rental_raw[0][10][1][1] = 3
    exc = error_type(with_ds1(fixture(RENTAL), rental_raw))
    rec = entity_record(with_ds1(fixture(RENTAL), rental_raw)) if exc is None else None
    check("offline", "rental e[10][1][1] = 3 degrades: no exception, amenities == (), sellers unchanged",
          exc is None and rec.amenities == () and len(rec.sellers) == 3
          and rec.headline and rec.headline.seller == "Expedia.ca", f"raised {exc and exc.__name__}")

    # Drifted PRICE, ECHO and IDENTITY blocks are fatal: PayloadError, never a
    # raw TypeError/IndexError that would reach the CLI as a traceback.
    hard = {
        "p[1] = 'x' (lead not a rate array)": mutated(lambda p: p[0][6][2].__setitem__(1, "x")),
        "every seller o[12] = 7": mutated(lambda p: every_row(p, lambda o: o.__setitem__(12, 7))),
        "p[44] = [1, 2] (breakdown not four numbers)": mutated(lambda p: p[0][6][2].__setitem__(44, [1, 2])),
        "[6][1] = 5 (echo not a list)": mutated(lambda p: p[0][6].__setitem__(1, 5)),
        "e[1] = None (no name)": mutated(lambda p: p[0].__setitem__(1, None)),
        "e[14] = 'hotel' (kind not an int)": mutated(lambda p: p[0].__setitem__(14, "hotel")),
    }
    for label, page in hard.items():
        exc = error_type(page)
        check("offline", f"{label} is PayloadError", exc is PayloadError, f"got {exc and exc.__name__}")
    one_bad = mutated(lambda p: rows_named(p, 558573628, lambda o: o.__setitem__(12, 7)))   # Reserving: p[21] only
    rec = entity_record(one_bad)
    check("offline", "one seller with o[12] = 7 is counted unparsed, the other 10 kept",
          rec.unparsed_rows == 1 and len(rec.sellers) == 10, f"got {rec.unparsed_rows}, {len(rec.sellers)}")

    def swap(o):
        o[12][4][2], o[12][4][3] = o[12][4][3], o[12][4][2]
        o[12][5][2], o[12][5][3] = o[12][5][3], o[12][5][2]

    page = mutated(lambda p: rows_named(p, 1930230291, swap))
    rec = entity_record(page)
    check("offline", "one row with incl < ex is counted unparsed and dropped (10 sellers left)",
          rec.unparsed_rows >= 1 and "dealbase.com" not in {s.seller for s in rec.sellers} and len(rec.sellers) == 10,
          f"got unparsed {rec.unparsed_rows}, {len(rec.sellers)} sellers")
    check("offline", "every two-basis row with [2]/[3] swapped is PayloadError",
          error_type(mutated(lambda p: every_row(p, lambda o: swap(o) if o[12][4][3] is not None else None))) is PayloadError)

    # A malformed age list: [2, [5], 0] instead of [2, [[5]], 0].
    rec = entity_record(mutated(lambda p: p[0][6][1].__setitem__(13, [2, [5], 0])))
    check("offline", "echo [2, [5], 0] (malformed ages) parses with child_ages None, not ()",
          rec.echo.adults == 2 and rec.echo.child_ages is None and not rec.echo.matches(NY),
          f"got {rec.echo}")

    # Headline tie: on the child-5 page Booking.com and Priceline both list
    # 7139.0. The p[22] member decides, in p[22]'s order — deterministically.
    child_html = fixture(CHILD5)
    child_raw = ds1(child_html)
    p = child_raw[0][6][2]
    check("offline", "the child-5 fixture really has two rows at the lead float 7139.0",
          p[1][2] == 7139 and [o[0][1] for o in p[22]] == [184, 220]
          and all(o[12][4][2] == 7139 for o in p[22]))
    rec = record(CHILD5)
    check("offline", "the headline resolves to Booking.com (first in p[22])",
          rec.headline and rec.headline.partner_id == 184 and rec.headline.seller == "Booking.com",
          f"got {rec.headline and rec.headline.seller}")
    reversed_21 = copy.deepcopy(child_raw)
    reversed_21[0][6][2][21].reverse()
    rec = entity_record(with_ds1(child_html, reversed_21))
    check("offline", "reversing p[21] does not change it (p[22] decides, not union order)",
          rec.headline and rec.headline.partner_id == 184, f"got {rec.headline and rec.headline.seller}")
    reversed_22 = copy.deepcopy(child_raw)
    reversed_22[0][6][2][22].reverse()
    rec = entity_record(with_ds1(child_html, reversed_22))
    check("offline", "reversing p[22] makes Priceline the headline",
          rec.headline and rec.headline.partner_id == 220, f"got {rec.headline and rec.headline.seller}")


# ---------------------------------------------------------------------------
# [offline] §3.8 the gmaps file reader
# ---------------------------------------------------------------------------


def test_gmaps_reader() -> None:
    print("\nreading a gmaps.py search --full --json file")
    full = os.path.join(FIXTURES, GMAPS_FULL)
    candidates, skipped, center = read_gmaps_file(full)
    check("offline", "the --full capture yields 10 candidates and skips none",
          len(candidates) == 10 and skipped == [], f"got {len(candidates)}, skipped {skipped}")
    check("offline", "every candidate carries ftid, place_id, cid, token and coordinates",
          len(candidates) == 10 and all(c.ids.ftid and c.ids.place_id and c.ids.cid and c.ids.token
                                        and c.lat is not None and c.lng is not None for c in candidates))
    first = candidates[0] if candidates else None
    check("offline", "the first candidate is the Samesun with ids matching its fixture",
          first is not None and first.name == "Samesun Banff" and first.ids.ftid == SAMESUN_FTID
          and first.ids.place_id == SAMESUN_PLACE_ID and first.ids.cid == SAMESUN_CID
          and first.ids.token == SAMESUN_TOKEN and first.rating == 4.3,
          f"got {first}")
    check("offline", "the `from` centre is read with its label",
          center is not None and abs(center.lat - 51.1784304) < 1e-6 and abs(center.lng + 115.5707903) < 1e-6
          and center.label == "Banff", f"got {center}")

    candidates, skipped, center = read_gmaps_file(os.path.join(FIXTURES, GMAPS_NOFULL))
    names = [r["name"] for r in json.load(open(os.path.join(FIXTURES, GMAPS_NOFULL)))["results"]]
    check("offline", "a non-full capture yields zero candidates and every name in skipped[]",
          candidates == [] and skipped == names and len(names) == 10, f"got {len(candidates)}, {skipped}")

    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "notresults.json")
        with open(bad, "w") as fh:
            json.dump({"count": 0}, fh)
        err = raised(read_gmaps_file, bad)
        check("offline", "a file without results raises IdError naming the file",
              isinstance(err, IdError) and "notresults.json" in str(err), f"got {err!r}")
        partial = os.path.join(tmp, "place_id_only.json")
        data = json.load(open(full))
        entry = dict(data["results"][0])
        del entry["ftid"]
        with open(partial, "w") as fh:
            json.dump({"from": data["from"], "results": [entry]}, fh)
        candidates, skipped, _ = read_gmaps_file(partial)
        check("offline", "an entry with place_id but no ftid still converts",
              len(candidates) == 1 and candidates[0].ids.cid == SAMESUN_CID and candidates[0].ids.ftid == SAMESUN_FTID
              and skipped == [], f"got {candidates}, {skipped}")


# ---------------------------------------------------------------------------
# [offline] §3.9 the commands through cli.main
# ---------------------------------------------------------------------------


def _ny(cmd: str, *ids: str) -> list[str]:
    return [cmd, *ids, "--checkin", NY_IN.isoformat(), "--checkout", NY_OUT.isoformat()]


def test_commands() -> None:
    print("\ncommands through cli.main with Client.quote stubbed")
    fairmont = record(FAIRMONT)

    # -- quote -------------------------------------------------------------
    with quote_stub(lambda n, ids, stay: fairmont) as calls:
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--json"])
    payload = as_json(out, err)
    check("offline", "quote exits 0 on the worked example, one quote call", code == 0 and len(calls) == 1,
          f"exit {code}, {len(calls)} calls: {err[:200]}")
    check("offline", "quote JSON is one ok:true object with the envelope",
          payload is not None and payload.get("ok") is True and payload.get("schema_version") == 1
          and payload.get("command") == "quote", f"got {str(payload)[:200]}")
    q = (payload or {}).get("query") or {}
    check("offline", "query.echo_matched True and echoed == requested",
          q.get("echo_matched") is True and q.get("echoed") == q.get("requested")
          and (q.get("requested") or {}).get("checkin") == "2026-12-31", f"got {q}")
    check("offline", "stay.days_ahead is counted from the package clock: 2026-12-31 is +109 from the probe day",
          (payload or {}).get("stay") == {"checkin": "2026-12-31", "checkout": "2027-01-02", "nights": 2, "days_ahead": 109}
          and (payload or {}).get("occupancy") == {"adults": 2, "child_ages": [], "rooms": 1},
          f"got {(payload or {}).get('stay')}, {(payload or {}).get('occupancy')}")
    check("offline", "quote JSON: cheapest is dealbase with basis both, headline is dealbase, breakdown total 2904.75",
          payload is not None and (payload.get("cheapest") or {}).get("seller") == "dealbase.com"
          and (payload.get("headline") or {}).get("seller") == "dealbase.com"
          and (payload.get("breakdown") or {}).get("total") == 2904.75
          and abs((payload.get("fees_share") or 0) - 129.875 / 2904.75) < 1e-9,
          f"got cheapest={payload and payload.get('cheapest')}")
    with quote_stub(lambda n, ids, stay: fairmont):
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID))
    check("offline", "human quote leads with the incl-tax figure and its basis, and flags the tie",
          code == 0 and money(out, "1,452.38") and "incl. tax" in out and "before tax" in out
          and "≈ same price, basis not stated" in out and "BusinessHotels.com" in out,
          f"exit {code}; output starts {out[:400]!r}")
    check("offline", "human quote restates the party from the echo and cannot book",
          "2 adults" in out and "cannot book" in out and "sold out" not in out.lower(), f"got {out[:300]!r}")
    check("offline", "human quote prints the free-cancellation deadline as shown and what is missing",
          "Jun 14" in out and "6:00 PM" in out, f"got {out[:600]!r}")

    with quote_stub(lambda n, ids, stay: record(WRAP_EMPTY)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "2027-06-09", "--checkout", "2027-06-11", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "quote on an empty union exits 1 with an ok object, empty sellers, cheapest null",
          code == 1 and payload.get("ok") is True and payload.get("sellers") == [] and payload.get("cheapest") is None,
          f"exit {code}: {str(payload)[:200]}")
    with quote_stub(lambda n, ids, stay: record(WRAP_EMPTY)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "2027-06-09", "--checkout", "2027-06-11"])
    check("offline", "and the human output says 'no rates listed for these nights', never 'sold out'",
          code == 1 and "no rates listed for these nights" in out.lower() and "sold out" not in out.lower(),
          f"exit {code}: {out[:300]!r}")
    with quote_stub(lambda n, ids, stay: record(PLUS270)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "2027-06-09", "--checkout", "2027-06-11", "--json"])
    payload = as_json(out, err)
    check("offline", "quote on plus270 (no headline, three offers) exits 0 with headline null",
          code == 0 and payload and payload.get("headline") is None and len(payload.get("sellers") or []) == 3
          and payload.get("breakdown") is None, f"exit {code}: {str(payload)[:200]}")

    # -- quote --token on a rental ----------------------------------------
    with quote_stub(lambda n, ids, stay: record(RENTAL)) as calls:
        code, out, err = run_cli(_ny("quote") + ["--token", RENTAL_TOKEN, "--json"])
    payload = as_json(out, err)
    hotel = (payload or {}).get("hotel") or {}
    check("offline", "quote --token on the rental: kind rental, star_class None, highlights None, amenities populated",
          code == 0 and hotel.get("kind") == "rental" and hotel.get("star_class") is None
          and payload.get("highlights") is None and len(payload.get("amenities") or []) == 21
          and (payload.get("rental") or {}).get("sleeps") == 4,
          f"exit {code}: {str(payload)[:300]}")
    check("offline", "the token given was decoded as kind 2",
          calls and calls[0][0].kind == KIND_RENTAL and calls[0][0].cid is None, f"got {calls and calls[0][0]}")
    check("offline", "rental headline is Expedia.ca at 464.05",
          (payload.get("headline") or {}).get("seller") == "Expedia.ca"
          and ((payload.get("headline") or {}).get("nightly") or {}).get("ex_tax") == 464.04688)
    with quote_stub(lambda n, ids, stay: record(RENTAL)):
        code, out, err = run_cli(_ny("quote") + ["--token", RENTAL_TOKEN])
    # On this capture the cheapest by comparable() is Canmore Rental
    # Management's single-figure 505.61 (stay 1,011.21) — it undercuts
    # Expedia's two-basis 515.09 by more than TIE_MARGIN — and Expedia is the
    # headline (464.05 ex / 1,030.19 incl stay). A rental leads with the STAY.
    lines = [ln for ln in out.splitlines() if re.search(r"\d,\d{3}|\d{3,}\.\d", ln)]
    first_money = lines[0] if lines else ""
    check("offline", "rental human output leads with the cheapest STAY figure (1,011.21, single) not a nightly one",
          money(first_money, "1,011.21") and "stay" in first_money.lower() and "single figure" in first_money
          and first_money.lower().find("stay") < first_money.lower().find("night"),
          f"first priced line: {first_money!r}")
    check("offline", "and names the headline's stay incl-tax 1,030.19 too", money(out, "1,030.19"), f"got {out[:600]!r}")
    check("offline", "and prints the fee line (212, 21 %)",
          money(out, "212") and re.search(r"\b21\s?%|\b20\.6\s?%", out) is not None, f"got {out[:600]!r}")

    # -- shortlist ---------------------------------------------------------
    three = ["--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--hotel", MOUNT_ROYAL_FTID]
    dead = HotelsHTTPError(f"cannot resolve {HOST} — no DNS, or no network egress.")

    def mixed(n, ids, stay):
        if n == 0:
            return aligned(fairmont, stay)
        if n == 1:
            return aligned(record(WRAP_EMPTY), stay)
        raise dead

    with quote_stub(mixed):
        code, out, err = run_cli(_ny("shortlist") + three + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "shortlist priced/empty/failed -> priced 1, unpriced 1, failed 1, exit 0",
          code == 0 and payload.get("priced") == 1 and len(payload.get("unpriced") or []) == 1
          and len(payload.get("failed") or []) == 1 and payload.get("candidates_considered") == 3,
          f"exit {code}: {str(payload)[:300]}")
    with quote_stub(mixed):
        code, out, err = run_cli(_ny("shortlist") + three)
    check("offline", "the human header says 'of the 3 checked' and never 'cheapest in'",
          code == 0 and "of the 3 checked" in out and "cheapest in" not in out.lower(), f"got {out[:300]!r}")

    def all_failed(n, ids, stay):
        raise dead

    with quote_stub(all_failed):
        code, out, err = run_cli(_ny("shortlist") + three + ["--json"])
    check("offline", "all three failed -> exit 3", code == 3, f"exit {code}")

    def empty_and_failed(n, ids, stay):
        if n == 0:
            return aligned(record(WRAP_EMPTY), stay)
        raise dead

    with quote_stub(empty_and_failed):
        code, out, err = run_cli(_ny("shortlist") + three + ["--json"])
    check("offline", "empty + failed with nothing priced -> exit 3, not 1", code == 3, f"exit {code}")

    from ghotels.client import EchoMismatch  # the client's own subclass of PayloadError

    def echo_mismatch(n, ids, stay):
        if n == 0:
            return aligned(fairmont, stay)
        raise EchoMismatch("Google priced 2026-10-13→14 for 2 adults, not 2026-12-31→2027-01-02 "
                           "for 2 adults. Refusing to report it.")

    with quote_stub(echo_mismatch):
        code, out, err = run_cli(_ny("shortlist") + three[:4] + ["--json"])
    payload = as_json(out, err) or {}
    failed = payload.get("failed") or []
    check("offline", "an echo-mismatch candidate lands in failed[] with reason 'priced a different stay'",
          code == 0 and len(failed) == 1 and "priced a different stay" in (failed[0].get("reason") or ""),
          f"exit {code}: {failed}")

    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--max-price", "100", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "--max-price below every rate -> exit 1 with filtered_out 1",
          code == 1 and payload.get("filtered_out") == 1 and payload.get("priced") == 1,
          f"exit {code}: {str(payload)[:200]}")
    # The lowest comparable figures on the page are BusinessHotels' single
    # 1452.3022 / 2904.6045 — so an inclusive boundary on exactly those kills
    # both "> instead of >=" and "rank on incl_tax, skipping single rows".
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        at = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--max-price", "1452.3022", "--json"])[0]
        under = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--max-price", "1452.30", "--json"])[0]
        total_in = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--max-total", "2904.6045", "--json"])[0]
        total_out = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--max-total", "2904.60", "--json"])[0]
    check("offline", "--max-price is inclusive on the comparable figure (1452.3022 keeps, 1452.30 drops)",
          at == 0 and under == 1, f"got {at}, {under}")
    check("offline", "--max-total uses the same rule on the stay (2904.6045 keeps, 2904.60 drops)",
          total_in == 0 and total_out == 1, f"got {total_in}, {total_out}")

    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "empty.json")
        with open(bad, "w") as fh:
            json.dump({"count": 0}, fh)
        code, out, err = run_cli(_ny("shortlist") + ["--ids-from", bad, "--json"])
        check("offline", "--ids-from a file without results -> exit 2 naming the file",
              code == 2 and "empty.json" in (as_json(out, err) or {}).get("error", ""), f"exit {code}: {out + err}")
    code, out, err = run_cli(_ny("shortlist") + ["--ids-from", os.path.join(FIXTURES, GMAPS_NOFULL), "--json"])
    payload = as_json(out, err) or {}
    check("offline", "--ids-from a non-full gmaps file (no ids) -> exit 2 and says --full is required",
          code == 2 and "--full" in payload.get("error", ""), f"exit {code}: {payload}")
    with quote_stub(lambda n, ids, stay: aligned(record(SAMESUN) if n == 0 else fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist") + ["--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--limit", "2", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "--ids-from the full gmaps file with --limit 2 prices the first two in Maps' order",
          code == 0 and len(calls) == 2 and calls[0][0].ftid == SAMESUN_FTID and calls[1][0].ftid == MOUNT_ROYAL_FTID
          and payload.get("candidates_considered") == 2 and payload.get("source") == "ids-from",
          f"exit {code}, {len(calls)} calls: {str(payload)[:200]}")
    rows = payload.get("rows") or []
    # Sorted by stay total: the Samesun (Bluepillow's 493.42 stay) precedes the
    # Fairmont record priced under Mount Royal's coordinates (2,904.60).
    check("offline", "rows carry the exact distance_km from the file's from centre: Samesun 0.6, then 0.3",
          [(r["hotel"]["name"], r.get("distance_km")) for r in rows]
          == [("Samesun Banff", 0.6), ("Fairmont Banff Springs", 0.3)],
          f"got {[(r.get('hotel', {}).get('name'), r.get('distance_km')) for r in rows]}")
    check("offline", "and the place block is the file's centre, labelled Banff",
          (payload.get("place") or {}).get("label") == "Banff"
          and abs((payload.get("place") or {}).get("lat", 0) - 51.1784304) < 1e-6, f"got {payload.get('place')}")
    # Sorting: feed the Fairmont FIRST and the Samesun second; by total the
    # Samesun must still lead, and --sort rating puts the Fairmont (4.7) first.
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else record(SAMESUN), stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--json"])
    by_total = [r["hotel"]["name"] for r in (as_json(out, err) or {}).get("rows") or []]
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else record(SAMESUN), stay)):
        code2, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "rating", "--json"])
    payload2 = as_json(out, err) or {}
    by_rating = [r["hotel"]["name"] for r in payload2.get("rows") or []]
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else record(SAMESUN), stay)):
        code3, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "nightly", "--json"])
    by_nightly = [r["hotel"]["name"] for r in (as_json(out, err) or {}).get("rows") or []]
    check("offline", "shortlist rows are sorted by stay total, not fed order: Samesun first although fetched second",
          code == 0 and by_total == ["Samesun Banff", "Fairmont Banff Springs"], f"exit {code}: {by_total}")
    check("offline", "--sort rating puts the Fairmont (4.7) before the Samesun (4.3) and echoes sort: rating",
          code2 == 0 and by_rating == ["Fairmont Banff Springs", "Samesun Banff"] and payload2.get("sort") == "rating",
          f"exit {code2}: {by_rating}, sort {payload2.get('sort')}")
    check("offline", "--sort nightly ranks on comparable(nightly): Samesun (246.71 single) first",
          code3 == 0 and by_nightly == ["Samesun Banff", "Fairmont Banff Springs"], f"exit {code3}: {by_nightly}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else record(SAMESUN), stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID))
    check("offline", "the human shortlist leads with the Samesun's single figure, labelled as such, via Bluepillow",
          code == 0 and "Cheapest of the 2 checked: Samesun Banff" in out and money(out, "246.71")
          and "via Bluepillow.ca" in out and "(single figure, treated as all-in) via" in out, f"got {out[:400]!r}")

    # -- cheapest ----------------------------------------------------------
    base = ["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "3"]
    later = cheaper(fairmont, 0.5)
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else later, stay)) as calls:
        code, out, err = run_cli(base + ["--json"])
    payload = as_json(out, err) or {}
    best = payload.get("best") or {}
    check("offline", "cheapest with a cheaper later date -> exit 0 and best is that date (no first-date floor)",
          code == 0 and len(calls) == 3 and best.get("checkin") == (PROBE_DAY + timedelta(days=31)).isoformat()
          and payload.get("cheapest_is_reliable") is True and payload.get("days_priced") == 3,
          f"exit {code}: best={best}, {str(payload)[:200]}")
    rows = payload.get("rows") or []
    d30 = PROBE_DAY + timedelta(days=30)   # 2026-10-13, a Tuesday
    check("offline", "every row carries the checkout it priced and the exact weekday (+30 from the probe day is Tue)",
          len(rows) == 3
          and [(r.get("checkin"), r.get("checkout"), r.get("weekday")) for r in rows]
          == [((d30 + timedelta(days=i)).isoformat(), (d30 + timedelta(days=i + 2)).isoformat(), w)
              for i, w in enumerate(("Tue", "Wed", "Thu"))]
          and best.get("weekday") == "Wed",
          f"got {[(r.get('checkin'), r.get('checkout'), r.get('weekday')) for r in rows]}")

    def one_failed(n, ids, stay):
        if n == 1:
            raise dead
        return aligned(fairmont, stay)

    with quote_stub(one_failed):
        code, out, err = run_cli(base + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "one failed date -> days_failed 1, cheapest_is_reliable False, exit 0",
          code == 0 and payload.get("days_failed") == 1 and payload.get("cheapest_is_reliable") is False
          and payload.get("days_priced") == 2, f"exit {code}: {str(payload)[:200]}")
    with quote_stub(one_failed):
        code, out, err = run_cli(base)
    check("offline", "and the human output withholds the word 'cheapest' and says '1 failed'",
          not re.search(r"\bcheapest\b", out, re.I) and re.search(r"1 failed", out) is not None, f"got {out[:400]!r}")

    def all_failed_dates(n, ids, stay):
        raise dead

    with quote_stub(all_failed_dates):
        code, out, err = run_cli(base + ["--json"])
    check("offline", "every date failed -> exit 3", code == 3, f"exit {code}")
    with quote_stub(lambda n, ids, stay: aligned(record(WRAP_EMPTY), stay)):
        code, out, err = run_cli(base + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "every date empty (echo matched, union empty) -> exit 1 with days_empty 3, days_priced 0, best null",
          code == 1 and payload.get("ok") is True and payload.get("days_empty") == 3 and payload.get("days_priced") == 0
          and payload.get("days_failed") == 0 and payload.get("days_visited") == 3 and payload.get("best") is None
          and payload.get("cheapest_is_reliable") is True,
          f"exit {code}: {str(payload)[:300]}")
    with quote_stub(lambda n, ids, stay: aligned(record(WRAP_EMPTY), stay)):
        code, out, err = run_cli(base)
    check("offline", "and the human sweep says '3 of 3 days had no rates listed'",
          code == 1 and "3 of 3 days had no rates listed" in out and out.count("no rates listed") == 4, f"got {out[:400]!r}")
    with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
        code, out, err = run_cli(["cheapest", FAIRMONT_FTID, "--checkin", "+325", "--nights", "2", "--days", "10", "--json"])
    check("offline", "a window running past the horizon is refused up front (exit 2, zero fetches)",
          code == 2 and len(calls) == 0, f"exit {code}, {len(calls)} fetches")

    # -- watch -------------------------------------------------------------
    def watch(*extra, rec=fairmont):
        with quote_stub(lambda n, ids, stay: aligned(rec, stay)):
            return run_cli(_ny("watch", FAIRMONT_FTID) + list(extra))

    code, out, _ = watch("--under", "100")
    check("offline", "watch --under 100 against a $1,452 page polls (exit 1), never refuses", code == 1, f"exit {code}")
    code, out, _ = watch("--under", "1500")
    check("offline", "watch --under 1500 fires (exit 0) and says 'AT OR UNDER your 1,500.00 per night (incl. tax): listed at 1,452.38 …'",
          code == 0 and "AT OR UNDER your 1,500.00 per night (incl. tax)" in out and money(out, "1,452.38")
          and "listed at" in out and "dealbase.com" in out and "keep waiting" not in out, f"exit {code}: {out[:300]!r}")
    check("offline", "the fire line names the party", "2 adults" in out, f"got {out[:300]!r}")
    check("offline", "watch --basis ex --under 1300 fires (dealbase ex 1282.75)", watch("--basis", "ex", "--under", "1300")[0] == 0)
    check("offline", "watch --under-total 2900 polls, 2905 fires (2904.75 incl)",
          watch("--under-total", "2900")[0] == 1 and watch("--under-total", "2905")[0] == 0)
    check("offline", "the threshold is inclusive: --under-total 2904.75 fires, 2904.74 polls",
          watch("--under-total", "2904.75")[0] == 0 and watch("--under-total", "2904.74")[0] == 1)
    check("offline", "the tie rule holds in watch: --under 1452.37 polls (BusinessHotels' 1452.30 is not selected)",
          watch("--under", "1452.375")[0] == 0 and watch("--under", "1452.37")[0] == 1)
    check("offline", "watch --under 0 is dead configuration (exit 2)", watch("--under", "0")[0] == 2)
    with quote_stub(lambda n, ids, stay: aligned(record(WRAP_EMPTY), stay)):
        code, out, _ = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "100"])
    check("offline", "watch on an empty union exits 1 (keep waiting for a cancellation)", code == 1, f"exit {code}")
    code, out, _ = watch("--under", "250", rec=record(SAMESUN))
    fire = re.search(r"listed at.*?via", out, re.S)
    check("offline", "Samesun --under 250 fires on Bluepillow's single 246.71 via comparable()",
          code == 0 and "Bluepillow" in out and fire is not None and "246.71" in fire.group(0)
          and "single figure, treated as all-in" in fire.group(0) and "incl. tax" not in fire.group(0),
          f"exit {code}: {out[:300]!r}")
    code, out, _ = watch("--basis", "ex", "--under", "250", rec=record(SAMESUN))
    check("offline", "with --basis ex the single row is ignored and the watch polls", code == 1, f"exit {code}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "1500", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "watch JSON: fired True, threshold {value, basis, per}, observed_at",
          payload.get("fired") is True and (payload.get("threshold") or {}).get("basis") == "incl"
          and (payload.get("threshold") or {}).get("per") == "night" and payload.get("observed_at"),
          f"got {str(payload)[:300]}")

    # -- names are refused; resolve is offline ----------------------------
    for argv in (_ny("quote", "Fairmont Banff Springs"), _ny("watch", "Fairmont Banff Springs") + ["--under", "100"],
                 ["cheapest", "Fairmont Banff Springs", "--checkin", "+30", "--nights", "2"],
                 _ny("shortlist", "--hotel", "Fairmont Banff Springs"), ["resolve", "Fairmont Banff Springs"]):
        with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
            code, out, err = run_cli(argv + ["--json"])
        payload = as_json(out, err) or {}
        check("offline", f"{argv[0]} with a bare name -> exit 2, message has the gmaps command with the name",
              code == 2 and len(calls) == 0 and "gmaps.py search" in payload.get("error", "")
              and "Fairmont Banff Springs" in payload.get("error", ""), f"exit {code}: {payload}")
    with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
        code, out, err = run_cli(["resolve", FAIRMONT_FTID, "--json"])
    payload = as_json(out, err) or {}
    found = payload.get("hotels") or payload.get("candidates") or []
    check("offline", "resolve <ftid> exits 0 with all four id forms and zero requests",
          code == 0 and len(calls) == 0 and len(found) == 1
          and found[0].get("ftid") == FAIRMONT_FTID and found[0].get("place_id") == FAIRMONT_PLACE_ID
          and found[0].get("cid") == FAIRMONT_CID and found[0].get("token") == FAIRMONT_TOKEN,
          f"exit {code}, {len(calls)} requests: {str(payload)[:300]}")
    code, out, err = run_cli(["resolve", "--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--json"])
    payload = as_json(out, err) or {}
    found = payload.get("hotels") or payload.get("candidates") or []
    check("offline", "resolve --ids-from the gmaps capture lists 10 hotels with names",
          code == 0 and len(found) == 10 and found[0].get("name") == "Samesun Banff", f"exit {code}: {len(found)}")


# ---------------------------------------------------------------------------
# [offline] §3.10 exit-code invariants
# ---------------------------------------------------------------------------


def test_exit_invariants() -> None:
    print("\nexit-code invariants")
    ci, co = "+40", "+42"
    commands = {
        "quote": ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--json"],
        "shortlist": ["shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--checkin", ci, "--checkout", co, "--json"],
        "cheapest": ["cheapest", FAIRMONT_FTID, "--checkin", ci, "--nights", "2", "--days", "2", "--json"],
        "watch": ["watch", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--under", "200", "--json"],
        "doctor": ["doctor", "--json"],
    }
    dead = HotelsHTTPError(f"cannot resolve {HOST} — no DNS, or no network egress.")

    def unreachable(n, path, params):
        raise dead

    codes = {}
    with get_stub(unreachable):
        for name, argv in commands.items():
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                codes[name] = (cli.main(argv), out.getvalue())
    check("offline", "every fetching command exits 3 when the network is gone",
          len(codes) == 5 and all(code == 3 for code, _ in codes.values()),
          "got " + repr({n: c for n, (c, _) in codes.items()}) + " — exit 1 would make a watch poll a dead network forever")

    def error_line(text):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        return payload.get("error") if payload.get("ok") is False else None

    lines = {name: error_line(text) for name, (_, text) in codes.items()}
    check("offline", "and each prints one parseable {ok:false, error} object", len(codes) == 5 and all(lines.values()),
          f"got {lines}")
    check("offline", "and the error is one line naming the host",
          len(codes) == 5 and all(line and HOST in line and "\n" not in line for line in lines.values()), f"got {lines}")
    noisy = sorted(name for name, (_, text) in codes.items() if "Traceback" in text)
    check("offline", "and none of them prints a traceback", not noisy and len(codes) == 5, f"{noisy}")
    with get_stub(unreachable) as calls:
        code, out, err = run_cli(["resolve", FAIRMONT_FTID, "--json"])
    check("offline", "resolve is unaffected by a dead network (offline, exit 0)", code == 0 and len(calls) == 0,
          f"exit {code}")

    for argv in (["quote", "--json"], ["--json", "nonsense"], ["quote", FAIRMONT_FTID, "--checkin", "+40", "--json"],
                 ["cheapest", FAIRMONT_FTID, "--checkin", "+40", "--nights", "x", "--json"]):
        code, out, err = run_cli(argv)
        payload = as_json(out, err)
        check("offline", f"argparse error {' '.join(argv)[:40]!r} under --json is the JSON error object, exit 2",
              code == 2 and payload is not None and payload.get("ok") is False and isinstance(payload.get("error"), str)
              and "Traceback" not in out + err, f"exit {code}: {(out + err)[:200]!r}")

    def explode(n, ids, stay):
        raise ZeroDivisionError("injected")

    with quote_stub(explode):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--json"])
    payload = as_json(out, err)
    check("offline", "an unexpected exception -> exit 3, one JSON error line, no traceback",
          code == 3 and payload is not None and payload.get("ok") is False and "Traceback" not in out + err,
          f"exit {code}: {(out + err)[:200]!r}")
    with quote_stub(explode):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co])
    check("offline", "and in human mode it is one 'error:' line on stderr",
          code == 3 and "Traceback" not in out + err and err.strip().startswith("error:") and err.count("\n") <= 1,
          f"exit {code}: {err!r}")


# ---------------------------------------------------------------------------
# [offline] §3.11 degraded sandbox (from google-flights, in full)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched(**attrs):
    """Swap module attributes on ghotels.http, and always put them back."""
    saved = {name: getattr(ghhttp, name) for name in attrs}
    for name, value in attrs.items():
        setattr(ghhttp, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(ghhttp, name, value)


def _fake_shutil(curl_path):
    return types.SimpleNamespace(which=lambda name: curl_path if name == "curl" else None)


def _fake_subprocess(returncode, stdout=b"", stderr=b"", raises_oserror=False):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        if raises_oserror:
            raise OSError(8, "Exec format error")
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr, args=argv)

    return types.SimpleNamespace(run=run), seen


def _fallback(url, params, curl, oserror=False, returncode=0, stdout=b"", stderr=b"", detail=False):
    fetched: list[str] = []
    transport = Transport(max_requests=5, throttle=0)
    transport._session = None
    transport._get_urllib = lambda full: fetched.append(full) or "<html>"
    fake, invoked = _fake_subprocess(returncode, stdout, stderr, oserror)
    page = error = None
    with _patched(shutil=_fake_shutil(curl), subprocess=fake):
        try:
            page = transport._get_fallback(url, params)
        except Exception as e:  # noqa: BLE001 - the outcome under test
            error = f"{type(e).__name__}: {e}"
    if detail:
        return fetched, page, error, invoked, transport
    return fetched, page, error


def test_degraded_sandbox() -> None:
    """The transport fallbacks as they behave on claude.ai: no `requests`, no
    curl, curl unspawnable, an egress proxy, no HOME. A transport failure must
    surface as an error, never as an empty result."""
    print("\nsandbox degradation")
    url = f"https://{HOST}/travel/hotels/entity/{FAIRMONT_TOKEN}"
    params = {"ts": "abc==", "hl": "en", "gl": "CA", "q": "a b"}

    with _patched(requests=None):
        transport = Transport(max_requests=5, throttle=0)
        check("offline", "no `requests` still builds a transport", transport._session is None)
        took = []
        transport._get_fallback = lambda u, p: took.append(("fallback", u)) or "<html>"
        transport._get_requests = lambda u, p: took.append(("requests", u)) or "<html>"
        transport.get("/travel/hotels/entity/x", params)
        check("offline", "no `requests` routes the fetch to the fallback", [s for s, _ in took] == ["fallback"], f"took {took!r}")

    fetched, page, error = _fallback(url, params, curl=None)
    check("offline", "no curl either falls through to urllib", error is None and page == "<html>" and len(fetched) == 1,
          f"urllib saw {len(fetched)} fetches, raised {error!r}")
    check("offline", "the urllib URL percent-encodes every parameter",
          bool(fetched) and "q=a+b" in fetched[0] and "ts=abc%3D%3D" in fetched[0], f"got {fetched[:1]!r}")

    fetched, page, error, invoked, transport = _fallback(url, params, curl="/usr/bin/curl", oserror=True, detail=True)
    check("offline", "a curl that cannot be spawned hands the same attempt to urllib",
          error is None and page == "<html>" and len(invoked) == 1 and len(fetched) == 1,
          f"curl {len(invoked)}x, urllib {len(fetched)}x, raised {error!r}")
    check("offline", "and nothing was charged twice for it", transport.requests_made == 0)

    for code, label in ((6, "cannot resolve the host"), (7, "cannot reach the host"), (5, "cannot resolve the proxy")):
        _, page, error = _fallback(url, params, curl="/usr/bin/curl", returncode=code, stdout=b"\n000", stderr=b"curl: fail")
        final = error is not None and error.startswith("HotelsHTTPError: ")
        message = error.split(": ", 1)[1] if final else error
        check("offline", f"curl exit {code} ({label}) is a one-line, final error",
              page is None and final and "\n" not in message and (HOST in message or "proxy" in message), f"got {error!r}")

    if ghhttp.requests is not None:
        proxy_error = ghhttp.requests.exceptions.ProxyError("tunnel refused")
        check("offline", "a proxy failure is never retried", ghhttp._transient(proxy_error) is False)
        check("offline", "a DNS failure is never retried either",
              ghhttp._transient(ghhttp.requests.exceptions.ConnectionError("NameResolutionError: Failed to resolve")) is False)
        check("offline", "but a timeout still is", ghhttp._transient(ghhttp.requests.exceptions.Timeout("slow")) is True)
        check("offline", "the proxy message names the variable to look at",
              "HTTPS_PROXY" in ghhttp._describe(proxy_error, 60) and HOST in ghhttp._describe(proxy_error, 60))

    saved_env = {k: os.environ.get(k) for k in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY",
                                                "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")}
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ["HTTPS_PROXY"] = "http://proxy.internal:3128"
        os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/corp.pem"
        args = ghhttp._curl_env_args()
        check("offline", "curl is handed the proxy and CA bundle requests would use",
              "--proxy" in args and "--cacert" in args
              and args[args.index("--proxy") + 1] == "http://proxy.internal:3128"
              and args[args.index("--cacert") + 1] == "/etc/ssl/corp.pem", f"got {args!r}")
        check("offline", "and never a flag that would disable verification", not any(a in ("-k", "--insecure") for a in args))
        check("offline", "and never -L (a redirect is an answer, not a hop)", "-L" not in args and "--location" not in args)
        os.environ["NO_PROXY"] = "google.com"
        bypassed = ghhttp._curl_env_args()
        check("offline", "NO_PROXY covering the host takes curl off the proxy", "--noproxy" in bypassed and "--proxy" not in bypassed)
        os.environ["NO_PROXY"] = "example.com"
        check("offline", "an unrelated NO_PROXY entry leaves the proxy in place", "--proxy" in ghhttp._curl_env_args())
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # Charged before the request, not after: a settled failure still costs one.
    transport = Transport(max_requests=5, throttle=0)
    transport._get_requests = lambda u, p: (_ for _ in ()).throw(HotelsHTTPError("settled"))
    transport._get_fallback = transport._get_requests
    raised(transport.get, "/travel/hotels/entity/x", {})
    check("offline", "a failed request is still charged (metered before it leaves)", transport.requests_made == 1,
          f"got {transport.requests_made}")

    # -- no HOME, nothing written to disk ---------------------------------
    saved_home = os.environ.get("HOME")
    saved_cwd = os.getcwd()
    scratch = tempfile.mkdtemp(prefix="ghotels-sandbox-")
    error = None
    try:
        os.environ.pop("HOME", None)
        os.chdir(scratch)
        try:
            homeless = Transport(max_requests=5, throttle=0)
            homeless.plan(1, "a quote")
            built = homeless.max_requests == 5
            with quote_stub(lambda n, ids, stay: aligned(record(FAIRMONT), stay)):
                run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42", "--json"])
            run_cli(["resolve", FAIRMONT_FTID, "--json"])
        except Exception as e:  # noqa: BLE001 - the outcome under test
            built, error = False, f"{type(e).__name__}: {e}"
        left_behind = sorted(os.listdir(scratch))
    finally:
        os.chdir(saved_cwd)
        if saved_home is not None:
            os.environ["HOME"] = saved_home
    check("offline", "a container with no HOME still builds a transport and runs a command", built, f"raised {error!r}")
    check("offline", "and the skill writes nothing to the working directory", left_behind == [], f"left behind {left_behind}")


def _no_sleep():
    """A `time` for ghotels.http whose sleep records instead of waiting."""
    import time as _time
    slept: list[float] = []
    return types.SimpleNamespace(monotonic=_time.monotonic, sleep=lambda s: slept.append(s)), slept


def _curl_get(stdout, returncode=0, stderr=b"", max_requests=5, params=None):
    """Transport.get with no `requests`, curl stubbed: (page, error, argv list, urllib calls, transport, sleeps).

    Unlike `_fallback` this goes through `get()`, so the budget, the retry
    loop and `transport_used` are all exercised.
    """
    transport = Transport(max_requests=max_requests, throttle=0)
    transport._session = None
    fetched: list[str] = []
    transport._get_urllib = lambda full: fetched.append(full) or "<urllib>"
    fake, invoked = _fake_subprocess(returncode, stdout, stderr)
    clock, slept = _no_sleep()
    page = error = None
    with _patched(shutil=_fake_shutil("/usr/bin/curl"), subprocess=fake, time=clock):
        try:
            page = transport.get("/travel/hotels/entity/x", params or {"hl": "en", "ts": "abc=="})
        except Exception as e:  # noqa: BLE001 - the outcome under test
            error = e
    return page, error, invoked, fetched, transport, slept


def _requests_get(responder, max_requests=5, calls=None):
    """Transport.get on the `requests` path with the session's get stubbed by `responder(url, **kw)`.

    Returns (page, error, transport, sleeps). `responder` may raise.
    """
    transport = Transport(max_requests=max_requests, throttle=0)
    transport._session.get = responder
    clock, slept = _no_sleep()
    page = error = None
    with _patched(time=clock):
        try:
            page = transport.get("/travel/hotels/entity/x", {"hl": "en"})
        except Exception as e:  # noqa: BLE001 - the outcome under test
            error = e
    return page, error, transport, slept


def _response(status, headers=None, text="<html>ok"):
    return types.SimpleNamespace(status_code=status, headers=headers or {}, text=text)


def test_transport_contract() -> None:
    """Review A: the promises http.py makes that the sandbox tests did not pin.

    Every stub sits below Transport.get — the session, subprocess, urllib's
    opener — so get() itself (budget, retries, no-redirect, 429/403 handling,
    the TLS recovery chain) runs for real. No socket is ever opened.
    """
    print("\ntransport contract (redirects, 429/403, the budget, TLS recovery)")
    check("offline", "THROTTLE_SECONDS is 2.5 s (the probe's pace) and Transport defaults to it",
          ghhttp.THROTTLE_SECONDS == 2.5 and Transport().throttle == 2.5 and Transport().max_requests == 5,
          f"got {ghhttp.THROTTLE_SECONDS}, {Transport().throttle}")
    check("offline", "RETRIES 3, MAX_ATTEMPTS 5, BACKOFF 1 s: the documented retry envelope",
          ghhttp.RETRIES == 3 and ghhttp.MAX_ATTEMPTS == 5 and ghhttp.BACKOFF_SECONDS == 1.0
          and ghhttp.DEFAULT_MAX_REQUESTS == 5 and ghhttp.MAX_MAX_REQUESTS == 40)

    # -- the curl path, through get() -------------------------------------
    page, error, invoked, fetched, transport, slept = _curl_get(b"<html>\n200\t")
    check("offline", "curl 200: the body before the trailer is the page, transport_used == 'curl', one spawn",
          page == "<html>" and error is None and len(invoked) == 1 and fetched == []
          and transport.transport_used == "curl" and transport.tls_path == "curl", f"got {page!r}, {error!r}, {transport.transport_used}")
    check("offline", "and that fetch was charged to the budget (requests_made == 1 on the fallback path)",
          transport.requests_made == 1, f"got {transport.requests_made}")
    argv = invoked[0] if invoked else []
    check("offline", "curl argv carries no -L/--location and --max-redirs 0 (a 3xx is an answer, not a hop)",
          argv and "-L" not in argv and "--location" not in argv and "--max-redirs" in argv
          and argv[argv.index("--max-redirs") + 1] == "0", f"argv {argv}")
    check("offline", "curl argv never disables verification and carries the consent cookie, -g and the UA",
          argv and not any(a in ("-k", "--insecure") for a in argv) and "Cookie: CONSENT=YES+cb" in argv
          and "-g" in argv and ghhttp.USER_AGENT in argv and argv[-1].startswith(f"https://{HOST}/travel/hotels/entity/x?"),
          f"argv {argv}")
    check("offline", "curl's -w trailer asks for the status and the redirect_url it did not follow",
          argv and "-w" in argv and argv[argv.index("-w") + 1] == "\n%{http_code}\t%{redirect_url}", f"argv {argv}")

    page, error, invoked, _, transport, _ = _curl_get(b"\n302\thttps://www.google.com/travel/search?q=Banff", returncode=0)
    check("offline", "curl 3xx: HotelsHTTPError naming the Location, one spawn, never urllib",
          page is None and isinstance(error, HotelsHTTPError) and "302" in str(error)
          and "https://www.google.com/travel/search?q=Banff" in str(error) and "hotel list page" in str(error)
          and len(invoked) == 1, f"got {error!r}, {len(invoked)} spawns")
    page, error, invoked, _, transport, slept = _curl_get(b"\n429\t", returncode=22)
    check("offline", "curl 429: HotelsHTTPError saying wait, exactly one spawn, no backoff, budget charged once",
          page is None and isinstance(error, HotelsHTTPError) and "429" in str(error) and "wait" in str(error)
          and "NOT \"no rates\"" in str(error) and len(invoked) == 1 and slept == [] and transport.requests_made == 1,
          f"got {error!r}, {len(invoked)} spawns, slept {slept}")
    page, error, invoked, _, transport, slept = _curl_get(b"\n503\t", returncode=22, stderr=b"curl: (22) 503", max_requests=10)
    check("offline", "curl 5xx: retried with backoff up to MAX_ATTEMPTS (5 spawns, 4 sleeps of 1/2/4/8 s), then final",
          page is None and isinstance(error, HotelsHTTPError) and "503" in str(error)
          and len(invoked) == 5 and slept == [1.0, 2.0, 4.0, 8.0] and transport.requests_made == 5,
          f"got {error!r}, {len(invoked)} spawns, slept {slept}, charged {transport.requests_made}")
    page, error, invoked, _, transport, _ = _curl_get(b"\n503\t", returncode=22, max_requests=2)
    check("offline", "and a 5xx retry stops at the budget: 2 spawns with --max-requests 2, HotelsHTTPError not RequestBudgetError",
          isinstance(error, HotelsHTTPError) and not isinstance(error, RequestBudgetError)
          and len(invoked) == 2 and transport.requests_made == 2, f"got {error!r}, {len(invoked)} spawns")
    page, error, invoked, _, _, slept = _curl_get(b"\n000", returncode=56, stderr=b"curl: (56) CONNECT tunnel failed, response 403")
    check("offline", "curl exit 56 with 'connect tunnel failed' is final (the proxy allowlist), one spawn",
          isinstance(error, HotelsHTTPError) and "proxy" in str(error) and "allowlist" in str(error)
          and len(invoked) == 1 and slept == [], f"got {error!r}, {len(invoked)} spawns")
    page, error, invoked, _, _, slept = _curl_get(b"\n000", returncode=28, stderr=b"curl: (28) timed out", max_requests=10)
    check("offline", "curl exit 28 (timeout) is transient: 5 spawns then the exit-28 message",
          isinstance(error, HotelsHTTPError) and "exit 28" in str(error) and len(invoked) == 5 and len(slept) == 4,
          f"got {error!r}, {len(invoked)} spawns")

    # -- the urllib path: build_opener is stubbed, so _get_urllib runs for real
    import urllib.error as uerr
    import urllib.parse as uparse
    import urllib.request as ureq

    def urllib_run(exc_or_body, max_requests=5):
        opened: list = []
        handlers: list = []

        class Opener:
            def open(self, req, timeout=None):
                opened.append((req, timeout))
                if isinstance(exc_or_body, Exception):
                    raise exc_or_body
                return io.BytesIO(exc_or_body)

        def build_opener(*hs):
            handlers.extend(hs)
            return Opener()

        fake = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=ureq.Request, build_opener=build_opener,
                                          HTTPRedirectHandler=ureq.HTTPRedirectHandler),
            error=uerr, parse=uparse)
        transport = Transport(max_requests=max_requests, throttle=0)
        transport._session = None
        clock, slept = _no_sleep()
        page = error = None
        with _patched(urllib=fake, shutil=_fake_shutil(None), time=clock):
            try:
                page = transport.get("/travel/hotels/entity/x", {"hl": "en", "ts": "abc=="})
            except Exception as e:  # noqa: BLE001 - the outcome under test
                error = e
        return page, error, opened, handlers, transport, slept

    page, error, opened, handlers, transport, _ = urllib_run(
        uerr.HTTPError("https://x", 302, "Found", {"Location": "https://www.google.com/travel/search?q=Banff"}, None))
    check("offline", "urllib 3xx: HotelsHTTPError naming the Location, one open, never followed",
          page is None and isinstance(error, HotelsHTTPError) and "302" in str(error)
          and "https://www.google.com/travel/search?q=Banff" in str(error) and len(opened) == 1,
          f"got {error!r}, {len(opened)} opens")
    check("offline", "the opener was built with the redirect-refusing handler, and nothing installed globally",
          any(isinstance(h, ghhttp._NoRedirect) for h in handlers) and len(handlers) == 1
          and ureq._opener is None, f"handlers {handlers}, global opener {ureq._opener!r}")
    probe_req = ureq.Request(f"https://{HOST}/travel/hotels/entity/x")
    check("offline", "_NoRedirect.redirect_request returns None (never a new Request) for every 3xx code",
          all(ghhttp._NoRedirect().redirect_request(probe_req, None, c, "x", {}, "https://elsewhere") is None
              for c in (301, 302, 303, 307, 308)))
    req = opened[0][0] if opened else None
    check("offline", "the urllib Request carries the consent cookie, the UA and the percent-encoded query",
          req is not None and req.get_header("Cookie") == "CONSENT=YES+cb" and req.get_header("User-agent") == ghhttp.USER_AGENT
          and "ts=abc%3D%3D" in req.full_url and opened[0][1] == transport.timeout, f"got {req and req.header_items()}")
    page, error, opened, _, transport, slept = urllib_run(
        uerr.HTTPError("https://x", 429, "Too Many", {"Retry-After": "120"}, None))
    check("offline", "urllib 429: one open, no retry, the message quotes Retry-After 120 seconds",
          isinstance(error, HotelsHTTPError) and "120 seconds" in str(error) and len(opened) == 1 and slept == []
          and transport.requests_made == 1, f"got {error!r}, {len(opened)} opens")
    page, error, opened, _, transport, _ = urllib_run(b"<html>via urllib")
    check("offline", "urllib 200: the decoded body is the page, transport_used == 'urllib', charged once",
          page == "<html>via urllib" and transport.transport_used == "urllib" and transport.tls_path == "urllib"
          and transport.requests_made == 1, f"got {page!r}, {error!r}")

    if ghhttp.requests is None:
        check("offline", "the requests-path contract (needs `requests` installed here)", False, "requests missing")
        return
    rexc = ghhttp.requests.exceptions

    # -- the requests path -------------------------------------------------
    transport = Transport(max_requests=5, throttle=0)
    check("offline", "the session mounts an adapter with urllib3 retries OFF (max_retries.total == 0)",
          transport._session.get_adapter("https://").max_retries.total == 0,
          f"got {transport._session.get_adapter('https://').max_retries}")
    check("offline", "the session sends the consent cookie, the UA and en-CA",
          transport._session.headers.get("Cookie") == "CONSENT=YES+cb"
          and transport._session.headers.get("User-Agent") == ghhttp.USER_AGENT
          and transport._session.headers.get("Accept-Language") == "en-CA,en;q=0.9", f"got {dict(transport._session.headers)}")

    seen: list = []

    def ok(url, **kw):
        seen.append((url, kw))
        return _response(200, text="<html>page")

    page, error, transport, slept = _requests_get(ok)
    check("offline", "requests 200: the text is the page, transport_used == 'requests', tls_path names the anchors",
          page == "<html>page" and transport.transport_used == "requests"
          and transport.tls_path in ("certifi", "truststore") and transport.requests_made == 1,
          f"got {page!r}, {transport.transport_used}, {transport.tls_path}")
    check("offline", "and the GET went to the entity URL with allow_redirects=False and the timeout",
          len(seen) == 1 and seen[0][0] == f"https://{HOST}/travel/hotels/entity/x"
          and seen[0][1].get("allow_redirects") is False and seen[0][1].get("timeout") == transport.timeout
          and seen[0][1].get("params") == {"hl": "en"}, f"got {seen}")

    calls: list = []

    def r429(url, **kw):
        calls.append(url)
        return _response(429, {"Retry-After": "120"})

    page, error, transport, slept = _requests_get(r429)
    check("offline", "requests 429: exactly one call, no sleep, HotelsHTTPError that says wait and quotes Retry-After",
          isinstance(error, HotelsHTTPError) and len(calls) == 1 and slept == []
          and "429" in str(error) and "wait" in str(error) and "120 seconds" in str(error)
          and "NOT \"no rates\"" in str(error) and transport.requests_made == 1,
          f"got {error!r}, {len(calls)} calls, slept {slept}")
    calls.clear()

    def r429_date(url, **kw):
        calls.append(url)
        return _response(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    _, error, _, _ = _requests_get(r429_date)
    check("offline", "a date-valued Retry-After is quoted as 'left alone until …'",
          isinstance(error, HotelsHTTPError) and "left alone until Wed, 21 Oct 2026 07:28:00 GMT" in str(error), f"got {error!r}")
    calls.clear()

    def r403(url, **kw):
        calls.append(url)
        return _response(403)

    page, error, transport, slept = _requests_get(r403)
    check("offline", "requests 403: exactly one call, HotelsHTTPError naming 403, never retried",
          isinstance(error, HotelsHTTPError) and "403" in str(error) and len(calls) == 1 and slept == []
          and transport.requests_made == 1, f"got {error!r}, {len(calls)} calls")
    calls.clear()

    def r500(url, **kw):
        calls.append(url)
        return _response(500)

    page, error, transport, slept = _requests_get(r500, max_requests=10)
    check("offline", "requests permanent 500 with budget 10: exactly MAX_ATTEMPTS (5) calls, backoff 1/2/4/8, then final",
          isinstance(error, HotelsHTTPError) and "500" in str(error) and len(calls) == 5
          and slept == [1.0, 2.0, 4.0, 8.0] and transport.requests_made == 5,
          f"got {error!r}, {len(calls)} calls, slept {slept}")
    calls.clear()
    _, error, transport, _ = _requests_get(lambda url, **kw: (calls.append(url), _response(418))[1])
    check("offline", "an unexpected status (418) is final: one call, HotelsHTTPError naming it",
          isinstance(error, HotelsHTTPError) and "418" in str(error) and len(calls) == 1, f"got {error!r}")

    # Budget off-by-one: max_requests=1 allows exactly one get.
    calls.clear()
    transport = Transport(max_requests=1, throttle=0)
    transport._session.get = ok
    first = raised(transport.get, "/travel/hotels/entity/x", {})
    second = raised(transport.get, "/travel/hotels/entity/x", {})
    check("offline", "max_requests=1: the first get succeeds, the second is RequestBudgetError with one call made",
          first is None and isinstance(second, RequestBudgetError) and len(seen) == 2 and transport.requests_made == 1
          and "ceiling of 1" in str(second), f"got {first!r}, {second!r}, {transport.requests_made} charged")
    check("offline", "plan() is the same arithmetic: 0 more fit, 1 does not",
          not raises(RequestBudgetError, transport.plan, 0, "nothing") and raises(RequestBudgetError, transport.plan, 1, "one"))

    # Transient connection failure: retried; DNS and proxy failures: not.
    calls.clear()
    _, error, transport, slept = _requests_get(
        lambda url, **kw: (calls.append(url), (_ for _ in ()).throw(rexc.Timeout("slow")))[1], max_requests=10)
    check("offline", "a Timeout is retried MAX_ATTEMPTS times then reported as 'did not respond within'",
          isinstance(error, HotelsHTTPError) and "did not respond within" in str(error) and len(calls) == 5
          and len(slept) == 4, f"got {error!r}, {len(calls)} calls")
    calls.clear()
    _, error, transport, slept = _requests_get(
        lambda url, **kw: (calls.append(url), (_ for _ in ()).throw(
            rexc.ConnectionError("NameResolutionError: Failed to resolve 'www.google.com'")))[1], max_requests=10)
    check("offline", "a DNS failure is final: one call, 'cannot resolve' naming the host",
          isinstance(error, HotelsHTTPError) and "cannot resolve" in str(error) and HOST in str(error)
          and len(calls) == 1 and slept == [], f"got {error!r}, {len(calls)} calls")
    calls.clear()
    _, error, _, slept = _requests_get(
        lambda url, **kw: (calls.append(url), (_ for _ in ()).throw(OSError("Could not find a suitable TLS CA certificate bundle, invalid path: /x")))[1])
    check("offline", "an unreadable CA bundle (bare OSError) is final: one call, names the bundle problem",
          isinstance(error, HotelsHTTPError) and "CA bundle" in str(error) and len(calls) == 1 and slept == [],
          f"got {error!r}, {len(calls)} calls")

    # -- the TLS recovery chain: certifi -> OS bundle -> curl, every step metered
    saved_env = {k: os.environ.pop(k, None) for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")}
    try:
        bundle = "/etc/ssl/certs/ca-certificates.crt"
        ssl_error = rexc.SSLError("HTTPSConnectionPool: certificate verify failed: certificate has expired (_ssl.c:1006)")

        def tls_chain(fail_first: int, max_requests: int = 5, curl: str | None = "/usr/bin/curl"):
            attempts: list = []

            def responder(url, **kw):
                attempts.append(kw)
                if len(attempts) <= fail_first:
                    raise ssl_error
                return _response(200, text="<html>via os bundle")

            fake, invoked = _fake_subprocess(0, b"<html>via curl\n200\t")
            clock, slept = _no_sleep()
            with _patched(_ssl_context=lambda: None, _os_ca_bundle=lambda: bundle,
                          shutil=_fake_shutil(curl), subprocess=fake, time=clock):
                transport = Transport(max_requests=max_requests, throttle=0)
                transport._session.get = responder
                verify_after: list = []
                original = transport._session.get

                def spy(url, **kw):
                    verify_after.append(transport._session.verify)
                    return original(url, **kw)

                transport._session.get = spy
                page = error = None
                try:
                    page = transport.get("/travel/hotels/entity/x", {"hl": "en"})
                except Exception as e:  # noqa: BLE001 - the outcome under test
                    error = e
            return page, error, attempts, invoked, transport, verify_after, slept

        page, error, attempts, invoked, transport, verify_after, slept = tls_chain(1)
        check("offline", "one SSLError: attempt 2 retries with the OS bundle as verify, tls_path 'os-bundle:…', 2 charged",
              page == "<html>via os bundle" and error is None and len(attempts) == 2 and verify_after == [True, bundle]
              and transport.tls_path == f"os-bundle:{bundle}" and transport.transport_used == "requests"
              and transport.requests_made == 2 and invoked == [] and slept == [],
              f"got {page!r}, {error!r}, verify {verify_after}, tls {transport.tls_path}, charged {transport.requests_made}")
        page, error, attempts, invoked, transport, verify_after, slept = tls_chain(2)
        check("offline", "two SSLErrors: attempt 3 goes to curl, 3 charged, transport_used 'curl', tls_path 'curl'",
              page == "<html>via curl" and error is None and len(attempts) == 2 and len(invoked) == 1
              and transport.requests_made == 3 and transport.transport_used == "curl" and transport.tls_path == "curl"
              and transport._session is None and slept == [],
              f"got {page!r}, {error!r}, {len(attempts)} session calls, {len(invoked)} spawns, charged {transport.requests_made}")
        page, error, attempts, invoked, transport, _, _ = tls_chain(1, max_requests=1)
        check("offline", "budget exhausted before the cure: the message says the OS bundle was untried, names it and the verdict",
              page is None and isinstance(error, HotelsHTTPError) and len(attempts) == 1 and transport.requests_made == 1
              and "no request left in the budget" in str(error) and bundle in str(error)
              and "certificate has expired" in str(error) and "did not verify" not in str(error),
              f"got {error!r}")
        page, error, attempts, invoked, transport, _, _ = tls_chain(2, max_requests=2)
        check("offline", "and with budget 2 the curl step is the untried cure named",
              isinstance(error, HotelsHTTPError) and len(attempts) == 2 and invoked == [] and transport.requests_made == 2
              and "curl was the next thing to try" in str(error) and "no request left in the budget" in str(error),
              f"got {error!r}")
        page, error, attempts, invoked, transport, _, _ = tls_chain(2, curl=None)
        check("offline", "no curl to fall back on: final after the OS bundle, message says it did not verify either",
              isinstance(error, HotelsHTTPError) and len(attempts) == 2 and transport.requests_made == 2
              and "curl is not installed" in str(error) and f"{bundle} did not verify" in str(error),
              f"got {error!r}")
        os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/corp.pem"
        page, error, attempts, invoked, transport, verify_after, _ = tls_chain(1)
        check("offline", "with REQUESTS_CA_BUNDLE set the OS bundle is skipped (the caller chose) and curl is next",
              page == "<html>via curl" and len(attempts) == 1 and len(invoked) == 1 and transport.requests_made == 2
              and transport.tls_path == "curl:/etc/ssl/corp.pem", f"got {page!r}, {error!r}, tls {transport.tls_path}")
        check("offline", "TLS recovery never disables verification: every attempt ran with verify True or a bundle path",
              bool(verify_after) and all(v is True or isinstance(v, str) for v in verify_after), f"got {verify_after}")
        check("offline", "and curl was handed the same bundle with --cacert (never -k)",
              invoked and "--cacert" in invoked[0] and invoked[0][invoked[0].index("--cacert") + 1] == "/etc/ssl/corp.pem"
              and "-k" not in invoked[0], f"argv {invoked and invoked[0]}")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_review_gaps() -> None:
    """Review F and the ids nits: assertions that the reviewers' mutants survived."""
    print("\nreview gaps (ids, parser boundaries, client, cli, model)")
    from ghotels.client import EchoMismatch
    from ghotels.ids import _b64url_encode, pb_bytes, pb_message, pb_uint
    from ghotels.model import FreeCancellation, SellerRow
    fairmont = record(FAIRMONT)
    html = fixture(FAIRMONT)
    raw = ds1(html)

    # -- ids ---------------------------------------------------------------
    name_error = raised(parse_hotel_id, "Fairmont Banff Springs")
    check("offline", "the bare-name refusal quotes the exact gmaps invocation",
          isinstance(name_error, IdError)
          and '--query "Fairmont Banff Springs" --limit 1 --full --json > hotel.json' in str(name_error)
          and 'gmaps.py search --near "<place>"' in str(name_error) and "--ids-from hotel.json" in str(name_error),
          f"got {name_error}")
    rental = parse_hotel_id(RENTAL_TOKEN)
    check("offline", "parse_hotel_id on a rental token: kind rental, token verbatim, no cid/ftid/place_id",
          rental.kind == KIND_RENTAL and rental.token == RENTAL_TOKEN and rental.cid is None
          and rental.ftid is None and rental.place_id is None, f"got {rental}")
    check("offline", "surrounding whitespace is stripped from every form",
          parse_hotel_id(f" {FAIRMONT_PLACE_ID}\n").cid == FAIRMONT_CID
          and parse_hotel_id(f"\t{FAIRMONT_FTID} ").cid == FAIRMONT_CID
          and parse_hotel_id(f" {FAIRMONT_CID} ").token == FAIRMONT_TOKEN
          and parse_hotel_id(f" {FAIRMONT_TOKEN}\n").cid == FAIRMONT_CID)
    padded = parse_hotel_id(f"{FAIRMONT_PLACE_ID}=")
    check("offline", "a padded place_id ('ChIJ…=') is accepted and returned unpadded",
          padded.cid == FAIRMONT_CID and padded.place_id == FAIRMONT_PLACE_ID and "=" not in padded.place_id, f"got {padded}")
    kind3 = _b64url_encode(pb_message(1, pb_uint(1, FAIRMONT_CID)) + pb_uint(2, 3))
    check("offline", "decode_token refuses kind 3 (only 1 hotel / 2 rental)", raises(IdError, decode_token, kind3))
    check("offline", "and parse_hotel_id refuses it too, as a non-id (with the gmaps hint)",
          isinstance(raised(parse_hotel_id, kind3), IdError) and "gmaps.py" in str(raised(parse_hotel_id, kind3)))
    big = _b64url_encode(pb_message(1, pb_uint(1, (1 << 64) + 5)) + pb_uint(2, 1))
    check("offline", "a token whose CID needs 10 varint bytes (> 2^64) is IdError, not a bogus CID",
          raises(IdError, decode_token, big) and raises(IdError, parse_hotel_id, big), f"got {raised(decode_token, big)!r}")
    kg_only = _b64url_encode(pb_message(1, pb_bytes(3, b"/m/052tvr")) + pb_uint(2, 1))
    check("offline", "a KG-id-only hotel token decodes to (None, 1) and parse_hotel_id refuses it, saying why",
          decode_token(kg_only) == (None, KIND_HOTEL) and "Knowledge Graph" in str(raised(parse_hotel_id, kg_only)),
          f"got {decode_token(kg_only)}, {raised(parse_hotel_id, kg_only)!r}")
    check("offline", "CID bounds: 0 and 2^64 are refused, 2^64-1 accepted",
          raises(IdError, parse_hotel_id, "0") and raises(IdError, parse_hotel_id, str(1 << 64))
          and parse_hotel_id(str((1 << 64) - 1)).cid == (1 << 64) - 1)
    check("offline", "a malformed ftid ('0x12') is refused naming the expected shape",
          "0x<hex>:0x<hex>" in str(raised(parse_hotel_id, "0x12")))
    with tempfile.TemporaryDirectory() as tmp:
        as_dict = os.path.join(tmp, "dict.json")
        with open(as_dict, "w") as fh:
            json.dump({"results": {"name": "x"}}, fh)
        err = raised(read_gmaps_file, as_dict)
        check("offline", "a gmaps file whose results is a dict (not a list) is IdError naming the file",
              isinstance(err, IdError) and "dict.json" in str(err) and "results" in str(err), f"got {err!r}")
        not_json = os.path.join(tmp, "bad.json")
        with open(not_json, "w") as fh:
            fh.write("{not json")
        err = raised(read_gmaps_file, not_json)
        check("offline", "a non-JSON file is IdError 'is not JSON'", isinstance(err, IdError) and "is not JSON" in str(err), f"got {err!r}")
        err = raised(read_gmaps_file, os.path.join(tmp, "missing.json"))
        check("offline", "a missing file is IdError 'cannot read'", isinstance(err, IdError) and "cannot read" in str(err), f"got {err!r}")
    code, out, err = run_cli(["resolve", f" {FAIRMONT_PLACE_ID}= ", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "resolve ' ChIJ…= ' reports input_kind place_id (classified after stripping)",
          code == 0 and (payload.get("query") or {}).get("input_kind") == "place_id"
          and (payload.get("hotels") or [{}])[0].get("place_id") == FAIRMONT_PLACE_ID, f"exit {code}: {payload.get('query')}")
    for text, kind in ((FAIRMONT_FTID, "ftid"), (str(FAIRMONT_CID), "cid"), (FAIRMONT_TOKEN, "token")):
        code, out, err = run_cli(["resolve", text, "--json"])
        check("offline", f"resolve reports input_kind {kind}", (as_json(out, err) or {}).get("query", {}).get("input_kind") == kind)
    code, out, err = run_cli(["resolve", FAIRMONT_FTID, "--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--json"])
    check("offline", "resolve with both an id and --ids-from is exit 2", code == 2 and "not both" in (as_json(out, err) or {}).get("error", ""))
    code, out, err = run_cli(["resolve", "--ids-from", os.path.join(FIXTURES, GMAPS_NOFULL), "--json"])
    payload = as_json(out, err) or {}
    check("offline", "resolve --ids-from a no-ids file is exit 2 with ok:false (never ok:true with an empty list)",
          code == 2 and payload.get("ok") is False and "10 skipped" in payload.get("error", ""), f"exit {code}: {payload}")

    # -- client / cli exit codes with the page stubbed ------------------------
    with get_stub(lambda n, p, q: fixture(UNKNOWN)) as calls:
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "quote on the unknown-entity page: exit 2, 'no such hotel id', one fetch",
          code == 2 and "no such hotel id" in payload.get("error", "") and "error status 5" in payload.get("error", "")
          and len(calls) == 1, f"exit {code}: {payload}")
    with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42", "--json"])
    payload = as_json(out, err) or {}
    d40, d42 = (PROBE_DAY + timedelta(days=40)).isoformat(), (PROBE_DAY + timedelta(days=42)).isoformat()
    check("offline", "quote of the New Year page for +40/+42: exit 3 naming both stays, one fetch",
          code == 3 and payload.get("ok") is False and "2026-12-31→2027-01-02" in payload.get("error", "")
          and f"{d40}→{d42}" in payload.get("error", "") and "different stay" in payload.get("error", "")
          and len(calls) == 1, f"exit {code}: {payload}")
    with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    check("offline", "and in human mode it is one 'error:' line on stderr, nothing on stdout",
          code == 3 and err.startswith("error: Google priced") and out == "" and err.count("\n") == 1, f"got {out!r} / {err!r}")
    with get_stub(lambda n, p, q: fixture(FAIRMONT)) as calls:
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--currency", "USD", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "quote --currency USD of a CAD page: exit 2 telling the user to re-run with --currency CAD",
          code == 2 and "--currency CAD" in payload.get("error", "") and "in CAD, not the USD" in payload.get("error", ""),
          f"exit {code}: {payload}")
    rec, err, _ = quote_page(fixture(PLUS270), Stay(date(2027, 6, 9), date(2027, 6, 11), 2, (), "USD"))
    check("offline", "plus270 (p[15] null) against a USD request: the echo's CAD alone decides -> QueryError naming CAD",
          rec is None and isinstance(err, QueryError) and "CAD" in str(err) and "--currency CAD" in str(err), f"got {err!r}")
    rec, err, _ = quote_page(fixture(FAIRMONT), Stay(NY_IN, NY_OUT, 1, (), "CAD"))
    check("offline", "one adult requested against the two-adult page is EchoMismatch (adults are compared)",
          rec is None and isinstance(err, EchoMismatch) and "for 1 adult." in str(err) and "for 2 adults, not" in str(err), f"got {err!r}")
    rec, err, _ = quote_page(fixture(FAIRMONT), Stay(NY_IN, NY_OUT, 2, (7,), "CAD"))
    check("offline", "a child requested against the no-child page is EchoMismatch naming 'child 7'",
          isinstance(err, EchoMismatch) and "child 7" in str(err), f"got {err!r}")
    null13 = copy.deepcopy(raw)
    null13[0][6][1][13] = None
    e = entity_record(with_ds1(html, null13)).echo
    check("offline", "a null [13] occupancy echo parses to adults None AND child_ages None (dates intact)",
          e.adults is None and e.child_ages is None and e.checkin == NY_IN and e.nights == 2, f"got {e}")
    check("offline", "and the mismatch message calls it 'an unstated party'",
          "an unstated party" in str(quote_page(with_ds1(html, null13), NY)[1]))
    no_ages = copy.deepcopy(raw)
    no_ages[0][6][1][13] = [2, None, 0]
    e = entity_record(with_ds1(html, no_ages)).echo
    check("offline", "[2, null, 0] is two adults and child_ages () — a match for a no-child request",
          e.adults == 2 and e.child_ages == () and e.matches(NY))

    # --ids-from on a single-hotel command: exactly one entry or a refusal.
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("quote") + ["--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--json"])
    payload = as_json(out, err) or {}
    check("offline", "quote --ids-from a 10-hotel file: exit 2, zero quotes, the error lists the candidates by ftid",
          code == 2 and calls == [] and "lists 10 hotels" in payload.get("error", "")
          and f"Samesun Banff = {SAMESUN_FTID}" in payload.get("error", "") and f"Mount Royal Hotel = {MOUNT_ROYAL_FTID}" in payload.get("error", "")
          and "shortlist --ids-from" in payload.get("error", ""), f"exit {code}: {payload}")
    with tempfile.TemporaryDirectory() as tmp:
        data = json.load(open(os.path.join(FIXTURES, GMAPS_FULL)))
        one = os.path.join(tmp, "one.json")
        with open(one, "w") as fh:
            json.dump({"from": data["from"], "results": [data["results"][0]]}, fh)
        with quote_stub(lambda n, ids, stay: aligned(record(SAMESUN), stay)) as calls:
            code, out, err = run_cli(_ny("quote") + ["--ids-from", one, "--json"])
        payload = as_json(out, err) or {}
        check("offline", "quote --ids-from a one-entry file: exit 0, exactly one quote call for the Samesun, input_kind ids-from",
              code == 0 and len(calls) == 1 and calls[0][0].cid == SAMESUN_CID
              and (payload.get("query") or {}).get("input_kind") == "ids-from"
              and (payload.get("query") or {}).get("file_candidates") == 1
              and ((payload.get("query") or {}).get("hotel") or {}).get("name") == "Samesun Banff",
              f"exit {code}, {len(calls)} calls: {payload.get('query')}")
        with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
            code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--ids-from", one, "--json"])
        check("offline", "an id AND --ids-from together is exit 2 naming both", code == 2 and calls == []
              and "got HOTEL, --ids-from" in (as_json(out, err) or {}).get("error", ""), f"exit {code}: {(as_json(out, err) or {}).get('error')}")
        with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
            code, out, err = run_cli(_ny("quote") + ["--json"])
        check("offline", "no hotel at all is exit 2 'give exactly one hotel'", code == 2 and calls == []
              and "give exactly one hotel" in (as_json(out, err) or {}).get("error", ""))
        with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
            code, out, err = run_cli(_ny("quote") + ["--ids-from", os.path.join(FIXTURES, GMAPS_NOFULL), "--json"])
        check("offline", "quote --ids-from a no-ids file is exit 2 saying --full", code == 2 and calls == []
              and "--full" in (as_json(out, err) or {}).get("error", ""))

    # -- model: the tie margin boundary --------------------------------------
    dealbase = next(s for s in fairmont.sellers if s.seller == "dealbase.com")   # incl 1452.375
    both_only = tuple(s for s in fairmont.sellers if s.basis == "both")
    best_both = min(comparable(s.nightly, "incl") for s in both_only)

    def single(amount: float) -> SellerRow:
        return SellerRow(seller="Single.com", partner_id=999, own_site=False,
                         nightly=Price(None, None, "CAD", "single", amount), stay=Price(None, None, "CAD", "single", amount * 2),
                         free_cancellation=FreeCancellation(False, None, None))

    check("offline", "the best two-basis nightly is dealbase's 1452.375", best_both == 1452.375 and dealbase.nightly.incl_tax == best_both)
    row, tie = cheapest_seller(both_only + (single(best_both - 1.00),), "incl")
    check("offline", "a single row exactly TIE_MARGIN under the best two-basis row IS cheapest, no tie",
          row is not None and row.seller == "Single.com" and tie is False, f"got {row and row.seller}, tie {tie}")
    row, tie = cheapest_seller(both_only + (single(best_both - 0.99),), "incl")
    check("offline", "0.99 under: the two-basis row is returned and the tie is flagged",
          row is not None and row.seller == "dealbase.com" and tie is True, f"got {row and row.seller}, tie {tie}")
    row, tie = cheapest_seller(both_only + (single(best_both),), "incl")
    check("offline", "equal: the two-basis row, and NO tie flag (the single is not strictly lower)",
          row is not None and row.seller == "dealbase.com" and tie is False, f"got {row and row.seller}, tie {tie}")
    row, tie = cheapest_seller(both_only + (single(best_both + 5),), "incl")
    check("offline", "a dearer single row: the two-basis row, no tie", row.seller == "dealbase.com" and tie is False)
    row, tie = cheapest_seller((single(300.0), single(200.0)), "incl")
    check("offline", "only single rows: the lowest amount wins, no tie", row.nightly.amount == 200.0 and tie is False)
    row, tie = cheapest_seller((single(300.0),), "ex")
    check("offline", "a single row alone under ex has nothing comparable: (None, False)", row is None and tie is False)
    check("offline", "cheapest_seller of no rows is (None, False)", cheapest_seller((), "incl") == (None, False))
    check("offline", "comparable(): both -> incl or ex by basis; single -> amount under incl, None under ex; None -> None",
          comparable(Price(10.0, 12.0, "CAD"), "incl") == 12.0 and comparable(Price(10.0, 12.0, "CAD"), "ex") == 10.0
          and comparable(Price(None, None, "CAD", "single", 11.0), "incl") == 11.0
          and comparable(Price(None, None, "CAD", "single", 11.0), "ex") is None and comparable(None, "incl") is None)
    check("offline", "Price.to_dict carries amount only for a single-figure price",
          "amount" not in Price(10.0, 12.0, "CAD").to_dict() and Price(None, None, "CAD", "single", 11.0).to_dict()["amount"] == 11.0)
    # cheapest_row(per="stay") ranks on the stay figure and returns the ORIGINAL row.
    from ghotels.client import cheapest_row
    row, tie = cheapest_row(fairmont.sellers, "incl", "stay")
    check("offline", "cheapest_row per stay: BusinessHotels' single 2904.6045 is within 1.00 of dealbase's 2904.75, so dealbase, tie",
          row is not None and row.seller == "dealbase.com" and tie is True and row.nightly.incl_tax == 1452.375,
          f"got {row and row.seller}, tie {tie}")
    row, tie = cheapest_row(record(SAMESUN).sellers, "incl", "stay")
    check("offline", "cheapest_row per stay on the Samesun is Bluepillow (single stay 493.4198), returned with its own nightly",
          row is not None and row.seller == "Bluepillow.ca" and row.nightly.amount == 246.7099 and row.stay.amount == 493.4198
          and tie is False, f"got {row and (row.seller, row.nightly, row.stay)}")

    # -- parser: headline without a match, [0] cancellation, the 25 % boundary, dedupe by id
    def mutated(fn):
        payload = copy.deepcopy(raw)
        fn(payload)
        return entity_record(with_ds1(html, payload))

    def every_row(payload, fn):
        for slot in (2, 12, 21, 22):
            for o in payload[0][6][2][slot] or []:
                fn(o)

    rec = mutated(lambda p: p[0][6][2][1].__setitem__(2, 999.0))
    h = rec.headline
    check("offline", "p[1][2] = 999.0 matches no row: headline.seller None, matched_row False, ex 999.0, incl None, stay None",
          h is not None and h.seller is None and h.partner_id is None and h.matched_row is False
          and h.nightly.ex_tax == 999.0 and h.nightly.incl_tax is None and h.nightly.basis == "both" and h.stay is None
          and len(rec.sellers) == 11, f"got {h}")
    with quote_stub(lambda n, ids, stay, rec=rec: aligned(rec, stay)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    head_line = next((ln for ln in out.splitlines() if ln.startswith("Google's headline:")), "")
    check("offline", "and the human headline line says 'seller not identified' and 'incl-tax figure not shown', never 'incl. tax'",
          code == 0 and "seller not identified" in head_line and "999.00 before tax (incl-tax figure not shown)" in head_line
          and "incl. tax" not in head_line and "(the cheapest row)" not in head_line, f"got {head_line!r}")
    with quote_stub(lambda n, ids, stay, rec=rec: aligned(rec, stay)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42", "--json"])
    hj = (as_json(out, err) or {}).get("headline") or {}
    check("offline", "and the JSON headline carries seller null, matched_row false, nightly.incl_tax null, stay null",
          hj.get("seller") is None and hj.get("matched_row") is False and (hj.get("nightly") or {}).get("incl_tax") is None
          and (hj.get("nightly") or {}).get("ex_tax") == 999.0 and hj.get("stay") is None and "stay" in hj, f"got {hj}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    head_line = next((ln for ln in out.splitlines() if ln.startswith("Google's headline:")), "")
    check("offline", "on the real page the headline line names dealbase and marks it '(the cheapest row)'",
          "via dealbase.com (the cheapest row)" in head_line and money(head_line, "1,452.38") and "incl. tax" in head_line,
          f"got {head_line!r}")

    def set_fc(value):
        def fn(p):
            every_row(p, lambda o: o[12][12].__setitem__(1, value) if o[0][1] == 76 else None)
        return fn

    fc = next(s for s in mutated(set_fc([0])).sellers if s.partner_id == 76).free_cancellation
    check("offline", "o[12][12][1] = [0]: shown False, deadline_text None, raw [0]",
          fc.shown is False and fc.deadline_text is None and fc.raw == [0], f"got {fc}")
    fc = next(s for s in mutated(set_fc(None)).sellers if s.partner_id == 76).free_cancellation
    check("offline", "o[12][12][1] = null: shown False, raw None", fc.shown is False and fc.raw is None and fc.deadline_text is None, f"got {fc}")
    fc = next(s for s in mutated(set_fc([1, None, None, None])).sellers if s.partner_id == 76).free_cancellation
    check("offline", "[1, null, null, null]: shown True with no deadline text (rendered 'free cancellation shown')",
          fc.shown is True and fc.deadline_text is None, f"got {fc}")
    with quote_stub(lambda n, ids, stay: aligned(mutated(set_fc([0])), stay)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    direct_line = next((ln for ln in out.splitlines() if ln.startswith("Fairmont Banff Springs (the")), "")
    check("offline", "and the table renders it 'no deadline shown', never 'non-refundable'",
          "no deadline shown" in direct_line and "non-refundable" not in out.lower(), f"got {direct_line!r}")
    # Rows with a deadline on the real page: 76, 89, 184, 1162912808, 220,
    # 2138133885, 1397608158, 588414280 (8); without: dealbase, Reserving,
    # BusinessHotels. So --free-cancellation drops the cheapest (dealbase) and
    # the single-figure near-tie, and Luxury Escapes' 1452.8125 leads — still
    # a near-tie with Amimir's single 1452.8091.
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--free-cancellation", "--json"])
    payload = as_json(out, err) or {}
    top = (payload.get("rows") or [{}])[0]
    check("offline", "--free-cancellation drops dealbase (no deadline): Luxury Escapes 1452.8125 leads, free_cancellation true, near-tie with Amimir",
          code == 0 and (top.get("cheapest") or {}).get("seller") == "Luxury Escapes" and top.get("free_cancellation") is True
          and (top.get("cheapest") or {}).get("nightly", {}).get("incl_tax") == 1452.8125 and top.get("near_tie") is True
          and top.get("sellers_listed") == 11, f"exit {code}: {top}")
    check("offline", "exactly 8 of the 11 Fairmont rows show a deadline (the filter's input)",
          sum(1 for s in fairmont.sellers if s.free_cancellation.shown) == 8
          and {s.seller for s in fairmont.sellers if not s.free_cancellation.shown} == {"dealbase.com", "Reserving", "BusinessHotels.com"})

    p21_only = [220, 558573628, 2138133885, 1397608158, 1745103910, 588414280]
    check("offline", "six partners appear in p[21] only (the rows the boundary test breaks)",
          all(pid in {o[0][1] for o in raw[0][6][2][21]} for pid in p21_only)
          and not any(pid in {o[0][1] for slot in (2, 12, 22) for o in raw[0][6][2][slot]} for pid in p21_only))

    def break_rows(pids):
        def fn(p):
            rows = p[0][6][2][21]
            for i, o in enumerate(rows):
                if o[0][1] in pids:
                    rows[i] = "not-a-row"
        return fn

    check("offline", "3 of 11 sellers unreadable (27 %) is PayloadError",
          raises(PayloadError, mutated, break_rows(p21_only[:3])))
    rec = mutated(break_rows(p21_only[:2]))
    check("offline", "2 of 11 (18 %): 9 sellers kept, unparsed_rows 2, the broken partners absent",
          len(rec.sellers) == 9 and rec.unparsed_rows == 2 and not ({220, 558573628} & {s.partner_id for s in rec.sellers}),
          f"got {len(rec.sellers)} sellers, unparsed {rec.unparsed_rows}")
    check("offline", "MAX_UNPARSED_SHARE is 0.25 and the guard is strict (>)",
          __import__("ghotels.parse", fromlist=["MAX_UNPARSED_SHARE"]).MAX_UNPARSED_SHARE == 0.25)
    # The ratio is over SELLERS: a partner broken in one slot but readable in
    # another is not unparsed. plus270 lists 1587851245 in p[2] and p[12].
    plus_raw = ds1(fixture(PLUS270))
    check("offline", "plus270's first offer (1587851245) sits in p[2] and p[12]",
          plus_raw[0][6][2][2][0][0][1] == 1587851245 and plus_raw[0][6][2][12][0][0][1] == 1587851245)
    plus_raw[0][6][2][2][0][12] = 7          # rates unreadable, partner id intact
    rec = entity_record(with_ds1(fixture(PLUS270), plus_raw))
    check("offline", "plus270 with its first offer's rates broken in p[2] only: still 3 sellers (the p[12] copy is read), unparsed 0",
          len(rec.sellers) == 3 and rec.unparsed_rows == 0 and 1587851245 in {s.partner_id for s in rec.sellers},
          f"got {len(rec.sellers)}, unparsed {rec.unparsed_rows}")
    plus_raw[0][6][2][12][0][12] = 7
    check("offline", "and broken in p[12] too (1 of 3 sellers, 33 %) it is PayloadError",
          raises(PayloadError, entity_record, with_ds1(fixture(PLUS270), plus_raw)))
    # A row with NO recoverable identity ("not-a-row") is its own unparsed
    # seller: 3 kept + 1 unknown = 25 %, which is not above the share.
    plus_raw = ds1(fixture(PLUS270))
    plus_raw[0][6][2][2][0] = "not-a-row"
    rec = entity_record(with_ds1(fixture(PLUS270), plus_raw))
    check("offline", "an identity-less broken row counts as one unparsed seller beside the 3 kept (exactly 25 %: not an error)",
          len(rec.sellers) == 3 and rec.unparsed_rows == 1, f"got {len(rec.sellers)}, unparsed {rec.unparsed_rows}")
    with quote_stub(lambda n, ids, stay: aligned(mutated(break_rows(p21_only[:2])), stay)):
        code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    check("offline", "the human quote says '2 seller row(s) could not be read'",
          code == 0 and "(2 seller row(s) could not be read and are missing above.)" in out, f"got {out[-300:]!r}")

    def literal_rate(o):
        o[12][4] = ["$x", "$y", 1452.8, None, 1453]
        o[12][5] = ["$x", "$y", 2905.6, None, 2906]

    rec = mutated(lambda p: every_row(p, lambda o: literal_rate(o) if o[0][1] == 220 else None))
    check("offline", "a rate ['$x', '$y', 1452.8, null, 1453] (incl string beside a null incl float) is unparsed, not single",
          rec.unparsed_rows == 1 and 220 not in {s.partner_id for s in rec.sellers} and len(rec.sellers) == 10,
          f"got unparsed {rec.unparsed_rows}, {len(rec.sellers)} sellers")
    rec = mutated(lambda p: every_row(p, lambda o: o[12][4].__setitem__(2, "1452.8") if o[0][1] == 220 else None))
    check("offline", "a rate whose [2] is a string is unparsed", rec.unparsed_rows == 1 and len(rec.sellers) == 10)
    rec = mutated(lambda p: every_row(p, lambda o: o[12][4].__setitem__(2, True) if o[0][1] == 220 else None))
    check("offline", "a rate whose [2] is `true` is unparsed (bool is not a number)", rec.unparsed_rows == 1 and len(rec.sellers) == 10,
          f"got unparsed {rec.unparsed_rows}")

    def rename(p):
        for o in p[0][6][2][21]:
            if o[0][1] == 220:
                o[0][0], o[0][1] = "Dup.com", 1
            if o[0][1] == 558573628:
                o[0][0], o[0][1] = "Dup.com", 2

    rec = mutated(rename)
    dups = [s for s in rec.sellers if s.seller == "Dup.com"]
    check("offline", "two rows with the same seller name but partner ids 1 and 2 stay two sellers (dedupe is by id)",
          len(dups) == 2 and {s.partner_id for s in dups} == {1, 2} and len(rec.sellers) == 11, f"got {[(s.seller, s.partner_id) for s in dups]}")

    def no_ids(p):
        for o in p[0][6][2][21]:
            if o[0][1] in (220, 558573628):
                o[0][0], o[0][1] = "NoId.com", None

    rec = mutated(no_ids)
    check("offline", "two rows with no partner id and the same name merge into one (dedupe by name when no id)",
          sum(1 for s in rec.sellers if s.seller == "NoId.com") == 1 and len(rec.sellers) == 10, f"got {len(rec.sellers)}")

    def richer_copy(p):
        # the direct row (76) in p[21] gets a bogus 5th room; the p[2] copy has 7 rooms and more slots
        for o in p[0][6][2][21]:
            if o[0][1] == 76:
                o[7] = [["Only room"]]
    rec = mutated(richer_copy)
    direct = next(s for s in rec.sellers if s.partner_id == 76)
    check("offline", "of a partner's several copies the richest (most non-null slots) is kept: the p[2] row's 7 rooms",
          len(direct.rooms) == 7, f"got {direct.rooms}")

    # -- run-level refusals propagate out of a shortlist/sweep; item-level ones are yielded
    def budget_out(n, ids, stay):
        if n == 1:
            raise RequestBudgetError("request ceiling of 2 reached. Narrow the range, or raise --max-requests deliberately.")
        return aligned(fairmont, stay)

    with quote_stub(budget_out) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--hotel", MOUNT_ROYAL_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "a budget shortfall mid-shortlist ends the run: exit 2, ok:false, no third quote (not one hotel's failure)",
          code == 2 and payload.get("ok") is False and "ceiling of 2" in payload.get("error", "") and len(calls) == 2,
          f"exit {code}, {len(calls)} calls: {payload}")
    with quote_stub(budget_out) as calls:
        code, out, err = run_cli(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "3", "--json"])
    check("offline", "and mid-sweep too: exit 2 after the second date", code == 2 and len(calls) == 2, f"exit {code}, {len(calls)} calls")

    def currency_out(n, ids, stay):
        if n == 1:
            raise QueryError("Google priced this hotel in CAD, not the USD asked for")
        return aligned(fairmont, stay)

    with quote_stub(currency_out) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--hotel", MOUNT_ROYAL_FTID) + ["--json"])
    check("offline", "a currency refusal mid-shortlist is the run's too: exit 2, two quotes",
          code == 2 and len(calls) == 2 and "in CAD" in (as_json(out, err) or {}).get("error", ""), f"exit {code}, {len(calls)} calls")

    def type_error(n, ids, stay):
        if n == 1:
            raise TypeError("'NoneType' object is not subscriptable")
        return aligned(fairmont, stay)

    with quote_stub(type_error) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--hotel", MOUNT_ROYAL_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "a raw TypeError on one hotel is that hotel's failure, named 'unexpected TypeError', the other two priced, exit 0",
          code == 0 and len(calls) == 3 and payload.get("priced") == 2 and len(payload.get("failed") or []) == 1
          and payload["failed"][0]["reason"].startswith("unexpected TypeError: 'NoneType'"), f"exit {code}: {payload.get('failed')}")

    # -- transport budget charged once per get under the fallback (F.c) --------
    fake, invoked = _fake_subprocess(0, b"<html>\n200\t")
    with _patched(requests=None, shutil=_fake_shutil("/usr/bin/curl"), subprocess=fake):
        transport = Transport(max_requests=5, throttle=0)
        transport.get("/travel/hotels/entity/x", {"hl": "en"})
    check("offline", "with `requests` absent, one get through curl charges exactly one request",
          transport.requests_made == 1 and len(invoked) == 1 and transport.transport_used == "curl", f"charged {transport.requests_made}")

    # -- doctor with the page stubbed ------------------------------------------
    raw_s = ds1(fixture(SAMESUN))
    d0 = PROBE_DAY + timedelta(days=cli.DOCTOR_DAYS_AHEAD)
    d1 = d0 + timedelta(days=1)
    raw_s[0][6][1][4] = [[d0.year, d0.month, d0.day], [d1.year, d1.month, d1.day], 1, None, 0]
    with get_stub(lambda n, p, q: with_ds1(fixture(SAMESUN), raw_s)) as calls:
        code, out, err = run_cli(["doctor", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "doctor on a healthy Samesun page: exit 0, hotel named, echo_matched, currency CAD, one fetch of the doctor token",
          code == 0 and payload.get("healthy_page") is True and payload.get("echo_matched") is True
          and payload.get("currency") == "CAD" and (payload.get("hotel") or {}).get("name") == "Samesun Banff"
          and (payload.get("hotel") or {}).get("cid") == cli.DOCTOR_CID and len(calls) == 1
          and calls[0][0] == f"/travel/hotels/entity/{cli.DOCTOR_TOKEN}" and calls[0][1].get("gl") == "CA",
          f"exit {code}: {str(payload)[:300]}")
    with get_stub(lambda n, p, q: fixture(SAMESUN)) as calls:
        code, out, err = run_cli(["doctor", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "doctor on a page echoing the wrong stay: exit 3, echo_matched False, host_reachable and healthy_page True",
          code == 3 and payload.get("ok") is False and payload.get("echo_matched") is False and payload.get("healthy_page") is True
          and payload.get("host_reachable") is True and "ts encoding" in payload.get("error", ""), f"exit {code}: {str(payload)[:300]}")
    with get_stub(lambda n, p, q: fixture(BLOCKED)):
        code, out, err = run_cli(["doctor", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "doctor on the consent page: exit 3, host_reachable True, healthy_page False",
          code == 3 and payload.get("host_reachable") is True and payload.get("healthy_page") is False
          and "ds:2" in payload.get("error", ""), f"exit {code}: {str(payload)[:300]}")
    with get_stub(lambda n, p, q: fixture(UNKNOWN)):
        code, out, err = run_cli(["doctor", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "doctor on an unknown-entity page: exit 3 (never 2 — doctor is 0 or 3) naming UnknownEntity",
          code == 3 and payload.get("host_reachable") is True and "UnknownEntity" in payload.get("error", ""), f"exit {code}: {str(payload)[:300]}")

    # -- watch: JSON threshold for --under-total / --basis ex, compared figure ----
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under-total", "2900", "--basis", "ex", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "watch --under-total 2900 --basis ex: threshold {2900, ex, stay}, compared 2565.5 (dealbase ex stay), fired, exit 0",
          code == 0 and payload.get("threshold") == {"value": 2900.0, "basis": "ex", "per": "stay"}
          and payload.get("compared") == 2565.5 and payload.get("fired") is True
          and (payload.get("cheapest") or {}).get("seller") == "dealbase.com", f"exit {code}: {str(payload)[:300]}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "100", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "watch --under 100 (polling): fired False, compared 1452.375, cheapest still reported, exit 1",
          code == 1 and payload.get("fired") is False and payload.get("compared") == 1452.375
          and (payload.get("cheapest") or {}).get("seller") == "dealbase.com" and payload.get("ok") is True,
          f"exit {code}: {str(payload)[:300]}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "100", "--under-total", "200", "--json"])
    check("offline", "watch with both --under and --under-total is exit 2",
          code == 2 and "exactly one of --under" in (as_json(out, err) or {}).get("error", ""))
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--json"])
    check("offline", "watch with neither is exit 2 too", code == 2)
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "1400"])
    check("offline", "the polling line says 'still above your 1,400.00 per night (incl. tax); keep waiting' and names the seller",
          code == 1 and "still above your 1,400.00 per night (incl. tax); keep waiting" in out and "via dealbase.com" in out
          and money(out, "1,452.38"), f"got {out[:400]!r}")

    # -- cheapest: the best row is by STAY comparable, weekday and headline carried
    later = cheaper(fairmont, 0.5)
    base = ["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "3"]
    with quote_stub(lambda n, ids, stay: aligned(later if n == 2 else fairmont, stay)):
        code, out, err = run_cli(base + ["--json"])
    payload = as_json(out, err) or {}
    best = payload.get("best") or {}
    check("offline", "cheapest: best carries the exact halved stay figure 1452.375, basis both, weekday Thu, seller dealbase",
          code == 0 and best.get("stay_incl_tax") == 1452.375 and best.get("stay_basis") == "both" and best.get("weekday") == "Thu"
          and best.get("seller") == "dealbase.com" and best.get("checkin") == (PROBE_DAY + timedelta(days=32)).isoformat(),
          f"exit {code}: {best}")
    row0 = (payload.get("rows") or [{}])[0]
    check("offline", "each priced row carries headline, breakdown and sellers_listed 11",
          (row0.get("headline") or {}).get("seller") == "dealbase.com" and (row0.get("breakdown") or {}).get("total") == 2904.75
          and row0.get("sellers_listed") == 11 and row0.get("near_tie") is True and row0.get("failed") is False, f"got {row0}")
    check("offline", "window/nights/currency blocks are the run's",
          payload.get("window") == {"start": (PROBE_DAY + timedelta(days=30)).isoformat(), "end": (PROBE_DAY + timedelta(days=32)).isoformat(), "step": 1}
          and payload.get("nights") == 2 and payload.get("currency") == "CAD", f"got {payload.get('window')}")
    with quote_stub(lambda n, ids, stay: aligned(later if n == 2 else fairmont, stay)):
        code, out, err = run_cli(base)
    check("offline", "the human sweep marks the cheapest row and states it: 'Cheapest: check in … (Thu) — stay 1,452.38 incl. tax'",
          code == 0 and out.count("<- cheapest") == 1 and "Cheapest: check in " in out and "(Thu)" in out
          and money(out, "1,452.38") and "via dealbase.com" in out, f"got {out[:600]!r}")


def _row(seller: str, nightly: float, stay: float, partner: int, single: bool = False):
    """A synthetic two-basis (incl = figure, ex = figure − 10) or single-figure seller row."""
    from ghotels.model import FreeCancellation, SellerRow
    if single:
        n, s = Price(None, None, "CAD", "single", nightly), Price(None, None, "CAD", "single", stay)
    else:
        n, s = Price(nightly - 10, nightly, "CAD"), Price(stay - 20, stay, "CAD")
    return SellerRow(seller=seller, partner_id=partner, own_site=False, nightly=n, stay=s,
                     free_cancellation=FreeCancellation(False, None, None))


def test_review_d_gaps() -> None:
    """Lane D's independent review: ranking basis, the ts on the wire, filters, sort, hygiene."""
    print("\nreview D gaps (client/cli)")
    fairmont, samesun = record(FAIRMONT), record(SAMESUN)
    from ghotels.client import cheapest_row

    # -- stay vs nightly ranking ------------------------------------------------
    a, b = _row("NightlyCheap", 100.0, 300.0, 1), _row("StayCheap", 110.0, 250.0, 2)
    check("offline", "cheapest_row per night picks the nightly minimum, per stay the stay minimum (they differ)",
          cheapest_row((a, b), "incl", "night")[0].seller == "NightlyCheap"
          and cheapest_row((a, b), "incl", "stay")[0].seller == "StayCheap"
          and cheapest_row((a, b), "ex", "stay")[0].seller == "StayCheap")
    synthetic = replace(fairmont, sellers=(a, b))
    with quote_stub(lambda n, ids, stay: aligned(synthetic, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under-total", "260", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "watch --under-total ranks on the STAY: StayCheap (250) fires at 260, compared 250.0",
          code == 0 and (payload.get("cheapest") or {}).get("seller") == "StayCheap" and payload.get("compared") == 250.0
          and payload.get("fired") is True, f"exit {code}: {str(payload)[:200]}")
    with quote_stub(lambda n, ids, stay: aligned(synthetic, stay)):
        code, out, err = run_cli(_ny("watch", FAIRMONT_FTID) + ["--under", "105", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "and --under ranks on the nightly: NightlyCheap (100) fires at 105, compared 100.0",
          code == 0 and (payload.get("cheapest") or {}).get("seller") == "NightlyCheap" and payload.get("compared") == 100.0,
          f"exit {code}: {str(payload)[:200]}")
    with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
        code, out, err = run_cli(_ny("watch", SAMESUN_FTID) + ["--basis", "ex", "--under", "723", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "Samesun --basis ex --under 723: ranks on the ex figure, Trip.com 722.21875 fires (Bluepillow's single has no ex)",
          code == 0 and (payload.get("cheapest") or {}).get("seller") == "Trip.com" and payload.get("compared") == 722.21875
          and (payload.get("threshold") or {}).get("basis") == "ex", f"exit {code}: {str(payload)[:300]}")
    with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
        code, out, err = run_cli(_ny("watch", SAMESUN_FTID) + ["--basis", "ex", "--under", "722", "--json"])
    check("offline", "and --under 722 polls (722.21875 is above it)", code == 1 and (as_json(out, err) or {}).get("fired") is False)

    # -- the ts on the wire carries the party ---------------------------------
    log = fixture("requests.log")
    child_ts = re.search(r"f_fairmont_child5\s+\S+\?hl=en&gl=CA&ts=([A-Za-z0-9_-]+)", log)
    rec, err, calls = quote_page(fixture(CHILD5), NY_CHILD5)
    check("offline", "Client.quote for 2 adults + child 5 sends the exact ts captured for #42 (f_fairmont_child5)",
          child_ts is not None and rec is not None and len(calls) == 1
          and calls[0][1].get("ts") == child_ts.group(1) and calls[0][1].get("hl") == "en" and calls[0][1].get("gl") == "CA"
          and calls[0][0] == f"/travel/hotels/entity/{FAIRMONT_TOKEN}",
          f"sent {calls and calls[0]}, log {child_ts and child_ts.group(1)}")
    adults_ts = re.search(r"f_fairmont_2ad_ny\s+\S+\?hl=en&gl=CA&ts=([A-Za-z0-9_-]+)", log) or \
        re.search(r"p8_fairmont_ts\s+\S+\?hl=en&gl=CA&ts=([A-Za-z0-9_-]+)", log)
    rec, err, calls = quote_page(fixture(FAIRMONT), NY)
    check("offline", "and for 2 adults the ts is the no-child capture (differs from the child-5 one)",
          adults_ts is not None and calls[0][1].get("ts") == adults_ts.group(1) and adults_ts.group(1) != child_ts.group(1),
          f"sent {calls and calls[0][1].get('ts')}, log {adults_ts and adults_ts.group(1)}")
    rec, err, calls = quote_page(fixture(FAIRMONT), Stay(NY_IN, NY_OUT, 2, (), "USD"))
    check("offline", "--currency USD changes the ts (the currency rides in field 5)",
          calls[0][1].get("ts") != adults_ts.group(1) and "USD" in str(err))

    # -- JSON cheapest / near_tie on the two real pages -------------------------
    with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
        code, out, err = run_cli(_ny("quote", SAMESUN_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "Samesun quote JSON: cheapest is Bluepillow.ca, basis single, amount 246.7099, near_tie null",
          code == 0 and (payload.get("cheapest") or {}).get("seller") == "Bluepillow.ca"
          and (payload.get("cheapest") or {}).get("basis") == "single"
          and ((payload.get("cheapest") or {}).get("nightly") or {}).get("amount") == 246.7099
          and payload.get("near_tie") is None, f"exit {code}: {payload.get('cheapest')}, near_tie {payload.get('near_tie')}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    nt = payload.get("near_tie") or {}
    check("offline", "Fairmont quote JSON: near_tie is BusinessHotels.com, basis single, 1452.3022, beside cheapest dealbase",
          nt.get("seller") == "BusinessHotels.com" and nt.get("basis") == "single" and (nt.get("nightly") or {}).get("amount") == 1452.3022
          and (payload.get("cheapest") or {}).get("seller") == "dealbase.com", f"got {nt}")
    with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
        code, out, err = run_cli(_ny("quote", SAMESUN_FTID))
    blue = next((ln for ln in out.splitlines() if ln.startswith("Bluepillow.ca")), "")
    check("offline", "the Bluepillow table row reads '246.71 single figure' and never 'incl. tax'",
          money(blue, "246.71") and "single figure" in blue and "incl. tax" not in blue and "493.42 single figure" in blue.replace(",", ""),
          f"got {blue!r}")
    check("offline", "and the single-figure legend is printed once because a single row exists",
          out.count("single figure = one figure shown") == 1)

    # -- --amenity nonsense ---------------------------------------------------------
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--amenity", "nonsense", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "--amenity nonsense: exit 2 before any fetch, error names it and lists the known names",
          code == 2 and calls == [] and "'nonsense'" in payload.get("error", "") and "Known names:" in payload.get("error", "")
          and all(n in payload.get("error", "") for n in ("Wi-Fi", "Parking", "Breakfast", "Hot tub")), f"exit {code}: {str(payload)[:300]}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--amenity", "wifi:cheap", "--json"])
    check("offline", "--amenity wifi:cheap: exit 2, the only qualifier is ':free'", code == 2 and calls == []
          and "':free'" in (as_json(out, err) or {}).get("error", ""))

    # -- hygiene: Ctrl-C, multi-line messages, dedupe ----------------------------------
    def interrupt(n, ids, stay):
        raise KeyboardInterrupt

    with quote_stub(interrupt):
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--json"])
    check("offline", "a KeyboardInterrupt inside a fetch makes main() return 130 with no traceback",
          code == 130 and "Traceback" not in out + err, f"exit {code}: {(out + err)[:200]!r}")

    def multiline(n, ids, stay):
        raise HotelsHTTPError("line one\nline two\n\n   line three")

    with quote_stub(multiline):
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "a multi-line exception message becomes one line in the JSON error",
          code == 3 and payload.get("error") == "line one line two line three", f"got {payload.get('error')!r}")
    with quote_stub(multiline):
        code, out, err = run_cli(_ny("quote", FAIRMONT_FTID))
    check("offline", "and one 'error:' line on stderr in human mode",
          code == 3 and err == "error: line one line two line three\n", f"got {err!r}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", str(FAIRMONT_CID), "--hotel", FAIRMONT_PLACE_ID) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "the same hotel as ftid, CID and place_id is one candidate: 1 quote, considered 1, available 1",
          code == 0 and len(calls) == 1 and payload.get("candidates_considered") == 1 and payload.get("candidates_available") == 1
          and len(payload.get("query", {}).get("hotels") or []) == 1, f"exit {code}, {len(calls)} calls: {str(payload)[:200]}")
    # The pasted full token carries the KG id, so its token STRING differs from
    # the CID-built one while the CID is the same: dedupe must key on the CID.
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", FAIRMONT_FULL_TOKEN) + ["--json"])
    payload = as_json(out, err) or {}
    check("offline", "ftid + the full pasted token (same CID, different token string) is still one candidate (dedupe by CID, not token)",
          parse_hotel_id(FAIRMONT_FULL_TOKEN).token != parse_hotel_id(FAIRMONT_FTID).token
          and code == 0 and len(calls) == 1 and payload.get("candidates_considered") == 1,
          f"exit {code}, {len(calls)} calls, considered {payload.get('candidates_considered')}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", SAMESUN_TOKEN) + ["--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--limit", "10", "--json"])
    payload = as_json(out, err) or {}
    hotels = payload.get("query", {}).get("hotels") or []
    check("offline", "--ids-from (Samesun first) plus --hotel the Samesun's token: 10 available, the file's entry kept first with its name, no duplicate",
          code == 0 and payload.get("candidates_available") == 10 and len(calls) == 10
          and sum(1 for h in hotels if h.get("cid") == SAMESUN_CID) == 1 and hotels and hotels[0].get("name") == "Samesun Banff",
          f"exit {code}, {len(calls)} calls, available {payload.get('candidates_available')}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID) + ["--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--limit", "10", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "a --hotel not in the file is appended after the file's entries: 11 available, 10 checked, the note says 1 more",
          code == 0 and payload.get("candidates_available") == 11 and payload.get("candidates_considered") == 10 and len(calls) == 10
          and (payload.get("query", {}).get("hotels") or [{}])[0].get("name") == "Samesun Banff"
          and payload.get("source") == "ids-from", f"exit {code}, {len(calls)} calls, available {payload.get('candidates_available')}")

    # -- cheapest: counts add up --------------------------------------------------
    dead = HotelsHTTPError(f"cannot resolve {HOST}")

    def one_failed(n, ids, stay):
        if n == 1:
            raise dead
        return aligned(fairmont, stay)

    with quote_stub(one_failed):
        code, out, err = run_cli(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "3", "--json"])
    payload = as_json(out, err) or {}
    check("offline", "cheapest with one failed date of three: days_empty 0 and visited == priced + empty + failed (3 = 2 + 0 + 1)",
          code == 0 and payload.get("days_empty") == 0 and payload.get("days_visited") == 3
          and payload.get("days_visited") == payload.get("days_priced") + payload.get("days_empty") + payload.get("days_failed")
          and payload.get("days_failed") == 1 and (payload.get("rows") or [{}])[1].get("failed") is True
          and HOST in ((payload.get("rows") or [{}, {}])[1].get("reason") or ""), f"exit {code}: {str(payload)[:300]}")

    # -- --min-rating / --min-stars are independent, inclusive filters -------------------
    def samesun_only(*extra):
        with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
            code, out, err = run_cli(_ny("shortlist", "--hotel", SAMESUN_FTID) + [*extra, "--json"])
        return code, as_json(out, err) or {}

    check("offline", "Samesun is 4.3 and 3-star (the filter inputs)", samesun.rating.score == 4.3 and samesun.star_class == ("3-star hotel", 3))
    code, payload = samesun_only("--min-rating", "4.3")
    check("offline", "--min-rating 4.3 keeps the Samesun (inclusive), exit 0", code == 0 and len(payload.get("rows") or []) == 1 and payload.get("filtered_out") == 0)
    code, payload = samesun_only("--min-rating", "4.4")
    check("offline", "--min-rating 4.4 drops it: exit 1, priced 1, filtered_out 1", code == 1 and payload.get("filtered_out") == 1 and payload.get("priced") == 1)
    code, payload = samesun_only("--min-stars", "3")
    check("offline", "--min-stars 3 keeps the Samesun (inclusive)", code == 0 and len(payload.get("rows") or []) == 1)
    code, payload = samesun_only("--min-stars", "4")
    check("offline", "--min-stars 4 drops it", code == 1 and payload.get("filtered_out") == 1)
    code, payload = samesun_only("--min-rating", "4.3", "--min-stars", "4")
    check("offline", "the two filters are independent: rating passes, stars fails -> filtered out", code == 1 and payload.get("filtered_out") == 1)
    code, payload = samesun_only("--min-rating", "6")
    check("offline", "--min-rating 6 is a usage error (max 5)", code == 2 and "at most 5" in payload.get("error", ""))
    rated_none = replace(samesun, rating=replace(samesun.rating, score=None))
    with quote_stub(lambda n, ids, stay: aligned(rated_none, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", SAMESUN_FTID) + ["--min-rating", "1", "--json"])
    check("offline", "a hotel with no rating is filtered out by any --min-rating (unknown is not a pass)",
          code == 1 and (as_json(out, err) or {}).get("filtered_out") == 1)

    # -- --sort total ranks on the stay, not the nightly ---------------------------------
    alpha = replace(fairmont, name="Alpha Hotel", sellers=(_row("A-seller", 100.0, 400.0, 11),))   # cheap night, dear stay (fees)
    beta = replace(samesun, name="Beta Hotel", sellers=(_row("B-seller", 150.0, 320.0, 12),))      # dear night, cheap stay

    def two(sort):
        with quote_stub(lambda n, ids, stay: aligned(alpha if n == 0 else beta, stay)):
            code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", sort, "--json"])
        return code, [r["hotel"]["name"] for r in (as_json(out, err) or {}).get("rows") or []]

    check("offline", "--sort total puts Beta (stay 320) before Alpha (stay 400) although Alpha's night is cheaper",
          two("total") == (0, ["Beta Hotel", "Alpha Hotel"]), f"got {two('total')}")
    check("offline", "--sort nightly puts Alpha (100) before Beta (150)", two("nightly") == (0, ["Alpha Hotel", "Beta Hotel"]), f"got {two('nightly')}")
    with quote_stub(lambda n, ids, stay: aligned(alpha if n == 0 else beta, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "total"])
    check("offline", "under --sort total the header names the cheapest STAY: Beta at 320.00 for the stay (150.00/night) via B-seller",
          "Cheapest of the 2 checked: Beta Hotel — 320.00 for the stay (150.00/night) incl. tax via B-seller" in out, f"got {out[:300]!r}")
    with quote_stub(lambda n, ids, stay: aligned(alpha if n == 0 else beta, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "nightly"])
    check("offline", "under --sort nightly the header names the cheapest NIGHT: Alpha at 100.00/night via A-seller",
          "Cheapest of the 2 checked: Alpha Hotel — 100.00/night incl. tax via A-seller" in out, f"got {out[:300]!r}")
    with quote_stub(lambda n, ids, stay: aligned(alpha if n == 0 else beta, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "rating"])
    check("offline", "under --sort rating the header still ranks by stay (Beta), whatever the row order",
          "Cheapest of the 2 checked: Beta Hotel — 320.00 for the stay (150.00/night) incl. tax via B-seller" in out, f"got {out[:300]!r}")

    # -- human shortlist strings after the lead's fixes ----------------------------------
    with quote_stub(lambda n, ids, stay: aligned(fairmont if n == 0 else samesun, stay)):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID) + ["--sort", "rating"])
    lines = out.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if ln.startswith("HOTEL")), None)
    first_row = lines[header_idx + 1] if header_idx is not None else ""
    check("offline", "--sort rating: the table's first row is the Fairmont (4.7) but the 'Cheapest of the 2 checked' line names the Samesun",
          code == 0 and first_row.startswith("Fairmont Banff Springs") and "Cheapest of the 2 checked: Samesun Banff" in out
          and money(out, "246.71") and "via Bluepillow.ca" in out, f"first row {first_row!r}; {out[:300]!r}")
    check("offline", "the priced shortlist header says 'as Google priced it' and names the party",
          "2 hotels checked — 2026-12-31 → 2027-01-02 (2 nights, as Google priced it) — for 2 adults — prices in CAD" in out, f"got {lines[0]!r}")
    fair_row = next((ln for ln in lines if ln.startswith("Fairmont Banff Springs")), "")
    check("offline", "the Fairmont cell carries the tie note '(a single-figure seller ≈ same price)', not the quote's basis label",
          "(a single-figure seller ≈ same price)" in fair_row and "basis not stated" not in fair_row
          and "1,452.38 incl. tax (1,282.75 ex)" in fair_row, f"got {fair_row!r}")
    sam_row = next((ln for ln in lines if ln.startswith("Samesun Banff")), "")
    check("offline", "the Samesun cell has no tie note and reads 'single figure'", "≈" not in sam_row and "246.71 single figure" in sam_row, f"got {sam_row!r}")

    def all_failed(n, ids, stay):
        raise dead

    with quote_stub(all_failed):
        code, out, err = run_cli(_ny("shortlist", "--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID))
    check("offline", "when nothing was fetched the header says 'as requested', never 'as Google priced it'",
          code == 3 and "(2 nights, as requested)" in out and "as Google priced it" not in out and "could not be checked:" in out,
          f"exit {code}: {out[:300]!r}")
    with quote_stub(lambda n, ids, stay: aligned(record(WRAP_EMPTY), stay)):
        code, out, err = run_cli(["shortlist", "--hotel", FAIRMONT_FTID, "--checkin", "2027-06-09", "--checkout", "2027-06-11"])
    check("offline", "a fetched-but-unpriced shortlist (no rows) also says 'as requested' and 'None of the 1 checked has a rate listed'",
          code == 1 and "as requested" in out and "None of the 1 checked has a rate listed for these nights" in out, f"exit {code}: {out[:300]!r}")

    # -- the headline float match is exact (1e-6), not "within a dollar" ------------
    html = fixture(FAIRMONT)
    raw = ds1(html)

    def lead_at(value):
        payload = copy.deepcopy(raw)
        payload[0][6][2][1][2] = value
        return entity_record(with_ds1(html, payload)).headline

    near = lead_at(1282.75 + 0.5)
    check("offline", "p[1][2] = 1283.25 (fifty cents off dealbase's 1282.75) matches NO row: seller None, matched_row False, ex 1283.25",
          near is not None and near.seller is None and near.matched_row is False and near.nightly.ex_tax == 1283.25
          and near.nightly.incl_tax is None, f"got {near}")
    tiny = lead_at(1282.75 + 1e-7)
    check("offline", "a reformatted literal (1282.7500001) still float-matches dealbase within _MATCH_EPS",
          tiny is not None and tiny.seller == "dealbase.com" and tiny.matched_row is True and tiny.nightly.incl_tax == 1452.375, f"got {tiny}")
    check("offline", "and 1e-5 off does not (the epsilon is 1e-6)", lead_at(1282.75 + 1e-5).matched_row is False)

    # -- --ids-from dedupes across files too ---------------------------------------
    full = os.path.join(FIXTURES, GMAPS_FULL)
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as calls:
        code, out, err = run_cli(_ny("shortlist") + ["--ids-from", full, "--ids-from", full, "--limit", "10", "--json"])
    payload = as_json(out, err) or {}
    hotels = payload.get("query", {}).get("hotels") or []
    check("offline", "the same gmaps file twice via --ids-from: 10 quotes, considered 10, available 10, the Samesun once",
          code == 0 and len(calls) == 10 and payload.get("candidates_considered") == 10 and payload.get("candidates_available") == 10
          and sum(1 for h in hotels if h.get("cid") == SAMESUN_CID) == 1 and len({c[0].cid for c in calls}) == 10,
          f"exit {code}, {len(calls)} calls, available {payload.get('candidates_available')}")


def test_launcher() -> None:
    """hotels.py by absolute path from an empty foreign cwd, no HOME, writes nothing (06 §6.7)."""
    print("\nthe launcher shim from a foreign directory")
    launcher = os.path.join(HERE, "hotels.py")
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="ghotels-cwd-") as cwd:
        proc = subprocess.run([sys.executable, launcher, "resolve", FAIRMONT_PLACE_ID, "--json"],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        left = sorted(os.listdir(cwd))
    payload = as_json(proc.stdout, proc.stderr) or {}
    found = payload.get("hotels") or payload.get("candidates") or []
    check("offline", "hotels.py resolve runs by absolute path with no HOME and exits 0",
          proc.returncode == 0 and len(found) == 1 and found[0].get("cid") == FAIRMONT_CID,
          f"exit {proc.returncode}: {(proc.stdout + proc.stderr)[:300]!r}")
    check("offline", "and leaves the foreign cwd empty", left == [], f"left {left}")
    proc = subprocess.run([sys.executable, "-m", "ghotels", "resolve", str(FAIRMONT_CID), "--json"], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=60)
    found = (as_json(proc.stdout, proc.stderr) or {}).get("hotels") or []
    check("offline", "python3 -m ghotels is the same entry point: exit 0 and the CID resolves to its token",
          proc.returncode == 0 and len(found) == 1 and found[0].get("cid") == FAIRMONT_CID
          and found[0].get("token") == FAIRMONT_TOKEN and found[0].get("ftid") is None,
          f"exit {proc.returncode}: {(proc.stdout + proc.stderr)[:300]!r}")


# ---------------------------------------------------------------------------
# [offline] §3.12 documented fields
# ---------------------------------------------------------------------------


def _documented_keys() -> tuple[str, dict[str, list[tuple[str, list[str]]]]]:
    """(source path, {command: [(key, [subkeys])]}) from SKILL.md's key table."""
    source = SKILL_MD
    if not os.path.exists(source):
        return source, {}
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    table: dict[str, list[tuple[str, list[str]]]] = {}
    for m in re.finditer(r"^\| `(\w+)` \| (.+?) \|\s*$", text, re.M):
        command, cell = m.group(1), m.group(2)
        if command not in ("resolve", "doctor", "quote", "shortlist", "cheapest", "watch"):
            continue  # the exit-code table has the same shape
        entries: list[tuple[str, list[str]]] = []
        depth, item, items = 0, "", []
        for ch in cell:
            depth += ch in "({"
            depth -= ch in ")}"
            if ch == "," and depth == 0:
                items.append(item)
                item = ""
            else:
                item += ch
        items.append(item)
        for it in items:
            km = re.match(r"\s*`([^`]+)`", it)
            if not km:
                continue
            key = km.group(1).replace("[]", "")
            sub: list[str] = []
            pm = re.search(r"\((.*)\)", it)
            if pm and (pm.group(1).lstrip().startswith("{") or it.strip().startswith("`" + key + "[]`")):
                inner = pm.group(1).strip().strip("{}")
                if "`" in inner and "each with" in inner:
                    sub = re.findall(r"`([^`]+)`", inner)
                elif "`" not in inner:
                    sub = [t.strip() for t in inner.split(",") if re.fullmatch(r"\s*\w+\s*", t)]
            entries.append((key, sub))
        table[command] = entries
    return source, table


def test_documented_fields() -> None:
    print("\ndocumented JSON keys are present in the default --json")
    source, table = _documented_keys()
    check("offline", f"the key table was found in {os.path.basename(source)}",
          bool(table) and {"quote", "shortlist", "cheapest", "watch", "resolve", "doctor"} <= set(table),
          f"got commands {sorted(table)} from {source}")
    if not table:
        return
    fairmont = record(FAIRMONT)
    ci, co = "+40", "+42"
    runs = {
        "resolve": (None, ["resolve", "--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--json"]),
        "quote": (lambda n, ids, stay: aligned(fairmont, stay), ["quote", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--json"]),
        "shortlist": (lambda n, ids, stay: aligned(fairmont, stay),
                      ["shortlist", "--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--limit", "2", "--amenity", "wifi", "--checkin", ci, "--checkout", co, "--json"]),
        "cheapest": (lambda n, ids, stay: aligned(fairmont, stay), ["cheapest", FAIRMONT_FTID, "--checkin", ci, "--nights", "2", "--days", "2", "--json"]),
        "watch": (lambda n, ids, stay: aligned(fairmont, stay), ["watch", FAIRMONT_FTID, "--checkin", ci, "--checkout", co, "--under", "100", "--json"]),
    }
    payloads = {}
    for command, (stub, argv) in runs.items():
        if stub is None:
            code, out, err = run_cli(argv)
        else:
            with quote_stub(stub):
                code, out, err = run_cli(argv)
        payloads[command] = (code, as_json(out, err))
    # doctor prices the Samesun +45 days for one night; the fixture's echo is
    # rewritten to that stay so the run reaches an ok object.
    raw = ds1(fixture(SAMESUN))
    d0 = PROBE_DAY + timedelta(days=cli.DOCTOR_DAYS_AHEAD)
    raw[0][6][1][4] = [[d0.year, d0.month, d0.day], [(d0 + timedelta(days=1)).year, (d0 + timedelta(days=1)).month,
                                                      (d0 + timedelta(days=1)).day], 1, None, 0]
    with get_stub(lambda n, p, q: with_ds1(fixture(SAMESUN), raw)):
        code, out, err = run_cli(["doctor", "--json"])
    payloads["doctor"] = (code, as_json(out, err))

    for command, entries in table.items():
        code, payload = payloads.get(command, (None, None))
        if payload is None or payload.get("ok") is not True:
            check("offline", f"{command}: a fixture-driven --json run produced an ok object", False,
                  f"exit {code}: {str(payload)[:200]}")
            continue
        missing = [k for k, _ in entries if k not in payload]
        check("offline", f"{command}: every documented top-level key is present ({len(entries)} keys)",
              not missing and len(entries) > 0, f"missing {missing}")
        for key, sub in entries:
            if not sub or key not in payload:
                continue
            value = payload[key]
            first = value[0] if isinstance(value, list) and value else value if isinstance(value, dict) else None
            if first is None:
                check("offline", f"{command}.{key}: has an element to check its documented fields on", False,
                      f"got {value!r}")
                continue
            lacking = [s for s in sub if s not in first]
            check("offline", f"{command}.{key}[]: documented fields {sub}", not lacking, f"missing {lacking} in {first}")


# ---------------------------------------------------------------------------
# [offline] §3.13 display width
# ---------------------------------------------------------------------------


def test_display() -> None:
    print("\ndisplay width")
    # The seller table clips its SELLER column. A 60-char seller name in
    # ASCII and in CJK must be cut to the same display width, so the CJK one
    # keeps roughly half as many characters — a character-count clip keeps
    # the same number and the column spills.
    fairmont = record(FAIRMONT)
    outputs = {}
    for label, name in (("ascii", "A" * 60), ("cjk", "東京駅前旅館" * 10)):
        sellers = tuple(replace(s, seller=name) if s.partner_id == 1930230291 else s for s in fairmont.sellers)
        with quote_stub(lambda n, ids, stay, sellers=sellers: aligned(replace(fairmont, sellers=sellers), stay)):
            code, out, err = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
        outputs[label] = (code, out)
    check("offline", "a 60-character seller name renders without error", all(c == 0 for c, _ in outputs.values()),
          f"got {[c for c, _ in outputs.values()]}")

    def clipped(out, chars):
        m = re.search(r"^([%s]+)…" % re.escape(chars), out, re.M)
        return m.group(1) if m else None

    a, c = clipped(outputs["ascii"][1], "A"), clipped(outputs["cjk"][1], "東京駅前旅館")
    check("offline", "a long seller name is clipped in the table with '…'", a is not None and c is not None,
          f"ascii clipped: {a is not None}, cjk clipped: {c is not None}")
    check("offline", "a CJK name is clipped by display width, not by character count",
          a is not None and c is not None and len(c) < len(a) and abs(width(c) - width(a)) <= 2,
          f"kept {len(a) if a else None} ASCII chars ({a and width(a)} cols) vs {len(c) if c else None} CJK chars ({c and width(c)} cols)")
    table_lines = [ln for ln in outputs["cjk"][1].splitlines() if re.search(r"incl\. tax \([\d,.]+ ex\)", ln)]
    check("offline", "every two-basis table row keeps 'incl. tax (… ex)' beside both figures, and the lead keeps 'before tax'",
          len(table_lines) == 9 and "incl. tax (1,282.75 before tax)" in outputs["cjk"][1]
          and all(ln.count("incl. tax (") == 2 for ln in table_lines), f"got {len(table_lines)} rows: {table_lines[:2]}")


# ---------------------------------------------------------------------------
# [offline] §3.14 highlights, the amenity table, the filter
# ---------------------------------------------------------------------------


def _walk_items(node, out):
    """Every [has, id, qualifier?, …] item reachable under `node` (04 §3.2.1)."""
    if isinstance(node, list):
        if (len(node) >= 2 and node[0] in (0, 1) and not isinstance(node[0], bool)
                and isinstance(node[1], int) and not isinstance(node[1], bool)
                and (len(node) == 2 or node[2] is None or (isinstance(node[2], int) and not isinstance(node[2], bool)))):
            out.append(node)
            return
        for x in node:
            _walk_items(x, out)


def test_amenities() -> None:
    print("\nhighlights, the amenity table and --amenity")
    table = json.load(open(os.path.join(FIXTURES, "amenity-entity-table.json")))
    from ghotels.parse import AMENITY_GROUPS, AMENITY_NAMES  # generated constants
    check("offline", "AMENITY_NAMES equals amenity-entity-table.json (the build-time check)",
          AMENITY_NAMES == {int(k): v["name"] for k, v in table.items()},
          f"{len(AMENITY_NAMES)} constants vs {len(table)} in the JSON; differ on "
          f"{sorted(set(AMENITY_NAMES) ^ {int(k) for k in table})[:10]}")
    check("offline", "AMENITY_GROUPS covers every group heading in the JSON",
          {v["group"]: v["heading"] for v in table.values()}.items() <= AMENITY_GROUPS.items(),
          f"got {AMENITY_GROUPS}")
    check("offline", "HIGHLIGHT_NAMES carries the seven chip ids",
          {28: "Wi-Fi", 54: "Breakfast", 15: "Parking", 19: "Pool", 26: "Spa", 10: "Hot tub", 23: "Restaurant"}.items()
          <= HIGHLIGHT_NAMES.items())

    expected_chips = {FAIRMONT: ["Pool", "Spa", "Hot tub", "Wi-Fi"], PLUS270: ["Pool", "Spa", "Hot tub", "Wi-Fi"],
                      CHILD5: ["Pool", "Spa", "Hot tub", "Wi-Fi"], SAMESUN: ["Breakfast", "Wi-Fi", "Parking", "Restaurant"]}
    for name, want in expected_chips.items():
        rec, html = record(name), fixture(name)
        labels = spans(html)
        chips = rec.highlights or ()
        check("offline", f"{name}: four chips, named in order, equal to the first four rendered labels",
              len(chips) == 4 and [c.name for c in chips] == want and labels[:4] == want
              and all(c.id in HIGHLIGHT_NAMES for c in chips), f"got {[c.name for c in chips]} vs {labels[:4]}")
        quals = dict(qualified_spans(html))
        for c in chips:
            if c.qualifier is not None:
                check("offline", f"{name}: chip {c.name} qualifier {c.qualifier} matches the AdLXZd sibling",
                      quals.get(c.name) == QUALIFIER_LABEL[c.qualifier], f"span says {quals.get(c.name)!r}")
        raw_chips = ds1(html)[0][10][6][1]
        check("offline", f"{name}: chips carry the raw qualifier ints from e[10][6][1]",
              [(c.id, c.qualifier_raw) for c in chips] == [(i[1], i[2] if len(i) > 2 else None) for i in raw_chips],
              f"got {[(c.id, c.qualifier_raw) for c in chips]} vs {raw_chips}")
    fairmont, samesun = record(FAIRMONT), record(SAMESUN)
    check("offline", "Fairmont chips: Wi-Fi free, the other three unqualified",
          [(c.id, c.qualifier) for c in fairmont.highlights] == [(19, None), (26, None), (10, None), (28, "free")])
    check("offline", "Samesun chips: Breakfast free, Wi-Fi free, Parking extra charge, Restaurant unqualified",
          [(c.id, c.qualifier) for c in samesun.highlights] == [(54, "free"), (28, "free"), (15, "extra_charge"), (23, None)])
    check("offline", "the child-5 page's chips equal the 2-adult page's",
          [c.to_dict() for c in record(CHILD5).highlights] == [c.to_dict() for c in fairmont.highlights])
    check("offline", "the rental: highlights None, zero LtjZ2d spans, named amenities from e[10][1]",
          record(RENTAL).highlights is None and spans(fixture(RENTAL)) == []
          and {"Air conditioning", "Balcony", "Crib"} <= {a.name for a in record(RENTAL).amenities}
          and all(a.group is None and a.id is None for a in record(RENTAL).amenities)
          and any(a.has is False and "pet" in (a.name or "").lower() for a in record(RENTAL).amenities),
          f"got {[(a.name, a.has) for a in record(RENTAL).amenities][:5]}")

    # The grouped walk, verified against the raw JSON rather than a number.
    for name, rec, distinct_zero_only, literal, literal_has1 in ((FAIRMONT, fairmont, 58, 94, 66), (SAMESUN, samesun, 34, 48, 27)):
        raw = ds1(fixture(name))[0][10][6]
        items: list = []
        for slot, node in enumerate(raw):
            if slot != 1:
                _walk_items(node, items)
        want_ids = {i[1] for i in items}
        got_ids = {a.id for a in rec.amenities}
        check("offline", f"{name}: amenities[] holds every id the recursive walk finds ({len(want_ids)}), none extra",
              got_ids == want_ids and len(rec.amenities) == len(want_ids), f"missing {sorted(want_ids - got_ids)[:10]}, extra {sorted(got_ids - want_ids)[:10]}")
        check("offline", f"{name}: and that is the literal {literal} distinct ids (not whatever this test's own walk found)",
              len(rec.amenities) == literal and len(want_ids) == literal, f"got {len(rec.amenities)} parsed, walk {len(want_ids)}")
        zero_only: list = []
        _walk_items(raw[0], zero_only)
        check("offline", f"{name}: a [0]-only walk would find {distinct_zero_only}, fewer than the {len(want_ids)} parsed",
              len({i[1] for i in zero_only}) == distinct_zero_only < len(got_ids))
        want_has = {i[1]: bool(i[0]) for i in items}
        want_qual = {i[1]: (i[2] if len(i) > 2 else None) for i in items}
        by_id = {a.id: a for a in rec.amenities}
        check("offline", f"{name}: every has flag and raw qualifier matches the JSON",
              all(by_id[i].has == want_has[i] and by_id[i].qualifier_raw == want_qual[i] for i in want_ids) and len(want_ids) > 40,
              f"differs on {[i for i in want_ids if by_id[i].has != want_has[i] or by_id[i].qualifier_raw != want_qual[i]][:10]}")
        named = {i for i in want_ids if str(i) in table}
        check("offline", f"{name}: names and groups come from the table; amenities_unnamed == {len(want_ids - named)}",
              all(by_id[i].name == table[str(i)]["name"] and by_id[i].group == table[str(i)]["heading"] for i in named)
              and all(by_id[i].name is None for i in want_ids - named)
              and rec.amenities_unnamed == len(want_ids - named),
              f"amenities_unnamed {rec.amenities_unnamed}")
        # B8: every has=1 named id's label is rendered on the page; has=0 count
        # equals the negated spans. One documented exception: Fairmont's id 193
        # is [1, 193] but its rendered label is "No single-use plastic straws" —
        # the table names it by the positive form, so its span matches with the
        # "No " prefix and is not counted as a negation.
        labels = spans(fixture(name))
        label_set = set(labels)
        has1_named = [a for a in rec.amenities if a.has and a.name]
        unrendered = [a.name for a in has1_named if a.name not in label_set and "No " + a.name not in label_set]
        check("offline", f"{name}: all {literal_has1} has=1 named amenities have their exact label on the page",
              not unrendered and len(has1_named) == literal_has1, f"{len(has1_named)} has=1 named; not rendered: {unrendered}")
        negated = [s for s in labels if s.startswith("No ") or s.startswith("Not ")]
        prefixed_positive = [s for s in negated if s.startswith("No ") and s[3:] in {a.name for a in has1_named}]
        has0 = [a for a in rec.amenities if not a.has]
        check("offline", f"{name}: has=0 entries ({len(has0)}) equal the 'No …'/'Not …' spans ({len(negated) - len(prefixed_positive)})",
              len(has0) == len(negated) - len(prefixed_positive), f"has0 {[a.name for a in has0]}, negated {negated}")
    check("offline", "Samesun: the seven negated spans are exactly its seven has=0 amenities",
          sorted(a.name for a in samesun.amenities if not a.has)
          == sorted(["Pool", "Hot tub", "Fitness center", "Spa", "accessible", "Pet-friendly", "air conditioning"]),
          f"got {sorted(a.name or '?' for a in samesun.amenities if not a.has)}")

    for name, rec, gate in ((FAIRMONT, fairmont, GATE_FAIRMONT), (SAMESUN, samesun, GATE_SAMESUN)):
        by_id = {a.id: a for a in rec.amenities}
        bad = {i: (by_id.get(i) and (by_id[i].name, by_id[i].has, by_id[i].qualifier)) for i, want in gate.items()
               if by_id.get(i) is None or (by_id[i].name, by_id[i].has, by_id[i].qualifier) != want}
        check("offline", f"{name}: the gate ids 28/15/54/19 carry the recorded name, has and qualifier", not bad, f"got {bad}")
    check("offline", "Samesun id 33 (Front desk) carries qualifier 24h and its span says '24 hour'",
          next(a for a in samesun.amenities if a.id == 33).qualifier == "24h"
          and ("Front desk", "24 hour") in qualified_spans(fixture(SAMESUN)))
    raw = ds1(fixture(FAIRMONT))
    raw[0][10][6][0][0][1][0] = [1, 28, 9]          # Internet group, first item: Wi-Fi with qualifier 9
    odd = next(a for a in entity_record(with_ds1(fixture(FAIRMONT), raw)).amenities if a.id == 28)
    check("offline", "an unknown qualifier value is carried raw (qualifier None, qualifier_raw 9), not dropped",
          odd.qualifier is None and odd.qualifier_raw == 9 and odd.has is True, f"got {odd}")

    # Human rendering (B7): negated as "listed as not having: Pool", unnamed collapsed.
    with quote_stub(lambda n, ids, stay: aligned(samesun, stay)):
        code, out, _ = run_cli(["quote", SAMESUN_FTID, "--checkin", "+40", "--checkout", "+42"])
    pool_lines = [ln for ln in out.splitlines() if "Pool" in ln]
    check("offline", "Samesun human output prints 'listed as not having: Pool' and never a bare Pool or 'No Pool'",
          code == 0 and any("listed as not having:" in ln and "Pool" in ln for ln in pool_lines)
          and all("not having" in ln for ln in pool_lines) and "No Pool" not in out,
          f"got {pool_lines}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)):
        code, out, _ = run_cli(["quote", FAIRMONT_FTID, "--checkin", "+40", "--checkout", "+42"])
    unnamed_lines = re.findall(r"\+(\d+) unnamed", out)
    check("offline", "Fairmont human output collapses unnamed ids into '+N unnamed' per group, summing to amenities_unnamed",
          unnamed_lines and sum(int(n) for n in unnamed_lines) == fairmont.amenities_unnamed
          and out.count("unnamed") == len(unnamed_lines),
          f"got {unnamed_lines} vs {fairmont.amenities_unnamed}")

    # --amenity through shortlist over [Fairmont, Samesun, rental-with-no-wifi-row].
    rental = record(RENTAL)
    rental_no_wifi = replace(rental, amenities=tuple(a for a in rental.amenities if "wi-fi" not in (a.name or "").lower()))
    trio = ["--hotel", FAIRMONT_FTID, "--hotel", SAMESUN_FTID, "--hotel", MOUNT_ROYAL_FTID]
    picks = [fairmont, samesun, rental_no_wifi]

    def shortlist(*extra):
        with quote_stub(lambda n, ids, stay: aligned(picks[n], stay)):
            code, out, err = run_cli(["shortlist", *trio, "--checkin", "+40", "--checkout", "+42", *extra, "--json"])
        with quote_stub(lambda n, ids, stay: aligned(picks[n], stay)):
            human = run_cli(["shortlist", *trio, "--checkin", "+40", "--checkout", "+42", *extra])[1]
        return code, as_json(out, err) or {}, human

    def counts(payload):
        """The four documented counts (the CLI may add a per-amenity breakdown)."""
        c = payload.get("amenity_counts") or {}
        return {k: c.get(k) for k in ("has", "has_other_terms", "lacks", "not_listed")}

    code, payload, human = shortlist("--amenity", "wifi:free")
    check("offline", "--amenity wifi:free keeps both hotels (3 priced, 1 filtered out); counts {has 2, other 0, lacks 0, not_listed 1}",
          code == 0 and payload.get("priced") == 3 and payload.get("filtered_out") == 1 and len(payload.get("rows") or []) == 2
          and counts(payload) == {"has": 2, "has_other_terms": 0, "lacks": 0, "not_listed": 1},
          f"exit {code}: {payload.get('amenity_counts')}, rows {len(payload.get('rows') or [])}")
    check("offline", "the rental is never described as lacking Wi-Fi",
          "does not list" in human and not re.search(r"rental.*lack|lack.*rental", human, re.I | re.S)
          and all(r.get("amenity_match") in ("has", None) for r in payload.get("rows") or []), f"got {human[:400]!r}")
    code, payload, human = shortlist("--amenity", "pool")
    check("offline", "--amenity pool keeps the Fairmont; counts {has 1, other 0, lacks 1, not_listed 1}",
          code == 0 and counts(payload) == {"has": 1, "has_other_terms": 0, "lacks": 1, "not_listed": 1}
          and len(payload.get("rows") or []) == 1, f"exit {code}: {payload.get('amenity_counts')}")
    check("offline", "and the header says '1 is listed as not having it, 1 does not list it'",
          "1 is listed as not having it" in human and "1 does not list it" in human, f"got {human[:400]!r}")
    code, payload, human = shortlist("--amenity", "parking:free")
    check("offline", "--amenity parking:free keeps neither; counts {has 0, other 2, lacks 0, not_listed 1}; exit 1",
          code == 1 and counts(payload) == {"has": 0, "has_other_terms": 2, "lacks": 0, "not_listed": 1},
          f"exit {code}: {payload.get('amenity_counts')}")
    check("offline", "and the header says '2 have it on other terms'", "2 have it on other terms" in human, f"got {human[:400]!r}")
    code, payload, human = shortlist()
    check("offline", "without --amenity, amenity_counts is null and all three are priced",
          code == 0 and payload.get("amenity_counts") is None and payload.get("priced") == 3, f"exit {code}")


# ---------------------------------------------------------------------------
# [offline] §3.15 limits and budgets
# ---------------------------------------------------------------------------


def test_limits_and_budget() -> None:
    print("\nshortlist and cheapest limits, the request budget")
    transport = Transport(max_requests=5)
    check("offline", "an over-budget plan is refused before any request",
          raises(RequestBudgetError, transport.plan, 30, "a 30-day sweep") and transport.requests_made == 0)
    check("offline", "a within-budget plan is allowed", not raises(RequestBudgetError, transport.plan, 5, "a 5-day sweep"))
    check("offline", "the default budgets are the documented ones",
          cli.DEFAULT_MAX_REQUESTS == {"resolve": 0, "quote": 2, "watch": 2, "shortlist": 11, "doctor": 1}
          and cli.RETRY_HEADROOM == 3 and cli.MAX_MAX_REQUESTS == 40, f"got {cli.DEFAULT_MAX_REQUESTS}")
    fairmont = record(FAIRMONT)
    # Ten DISTINCT ids (the CLI de-duplicates repeats of one hotel).
    ftids = [r["ftid"] for r in json.load(open(os.path.join(FIXTURES, GMAPS_FULL)))["results"]]
    ten = [x for f in ftids[:10] for x in ("--hotel", f)]
    stay = ["--checkin", "+40", "--checkout", "+42"]

    def shortlist_plan(argv):
        with get_stub(lambda n, p, q: fixture(FAIRMONT)) as gets:
            with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as quotes:
                code, out, err = run_cli(argv + ["--json"])
        return code, len(gets), len(quotes)

    # 06 §3.15: N fetches must fit the budget (the default budget of 11 leaves
    # one spare for a retry; the spare is not mandatory). 04 §7's "refused if
    # limit > budget - 1" would make ×10 with --max-requests 10 a refusal;
    # the build follows 06 and the plan floor is asserted at N == budget.
    code, gets, quotes = shortlist_plan(["shortlist", *ten, *stay, "--limit", "10", "--max-requests", "9"])
    check("offline", "shortlist of 10 with --max-requests 9 is refused before any fetch",
          code == 2 and gets == 0 and quotes == 0, f"exit {code}, {gets} fetches, {quotes} quotes")
    code, gets, quotes = shortlist_plan(["shortlist", *ten, *stay, "--limit", "10", "--max-requests", "10"])
    check("offline", "and with --max-requests 10 the plan is accepted and all 10 are quoted",
          code == 0 and quotes == 10, f"exit {code}, {quotes} quotes")
    code, gets, quotes = shortlist_plan(["shortlist", *ten, *stay, "--limit", "3", "--max-requests", "3"])
    check("offline", "--limit 3 bounds the candidates fetched (3 quotes), budget 3 suffices",
          code == 0 and quotes == 3, f"exit {code}, {quotes} quotes")
    code, gets, quotes = shortlist_plan(["shortlist", *ten, *stay, "--limit", "3", "--max-requests", "2"])
    check("offline", "--limit 3 with --max-requests 2 is refused up front", code == 2 and quotes == 0, f"exit {code}, {quotes}")
    code, gets, quotes = shortlist_plan(["shortlist", *ten, *stay])
    check("offline", "the default shortlist limit is 6 within the default budget of 11", code == 0 and quotes == 6,
          f"exit {code}, {quotes} quotes")

    def sweep(argv):
        with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as quotes:
            code, out, err = run_cli(argv + ["--json"])
        return code, len(quotes), quotes[0][2] if quotes else None

    code, n, budget = sweep(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "21", "--step", "1"])
    check("offline", "cheapest --days 21 --step 1 visits 21 dates with budget 24", code == 0 and n == 21 and budget == 24,
          f"exit {code}, {n} dates, budget {budget}")
    code, n, budget = sweep(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "21", "--step", "7"])
    check("offline", "cheapest --days 21 --step 7 visits 3 dates", code == 0 and n == 3 and budget == 6,
          f"exit {code}, {n} dates, budget {budget}")
    code, n, budget = sweep(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2"])
    check("offline", "the default sweep is 14 dates with budget 17", code == 0 and n == 14 and budget == 17,
          f"exit {code}, {n} dates, budget {budget}")
    code, n, _ = sweep(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "22"])
    check("offline", "--days 22 is refused with zero fetches", code == 2 and n == 0, f"exit {code}, {n}")
    code, n, _ = sweep(["cheapest", FAIRMONT_FTID, "--checkin", "+30", "--nights", "2", "--days", "5", "--max-requests", "4"])
    check("offline", "a sweep over its --max-requests is refused before the first fetch", code == 2 and n == 0,
          f"exit {code}, {n}")
    with quote_stub(lambda n, ids, stay: aligned(fairmont, stay)) as quotes:
        code, out, err = run_cli(["quote", FAIRMONT_FTID, *stay, "--json"])
    check("offline", "quote's default budget is 2 (one page plus a spare)", quotes and quotes[0][2] == 2, f"got {quotes and quotes[0][2]}")


# ---------------------------------------------------------------------------
# [network]
# ---------------------------------------------------------------------------


def test_live() -> None:
    print(f"\nlive calls to {HOST} (7 requests)")
    checkin = date.today() + timedelta(days=45)
    stay = Stay(checkin, checkin + timedelta(days=2), 2, (), "CAD")
    fairmont = parse_hotel_id(FAIRMONT_FTID)
    pages: list[str] = []
    original_get = Transport.get

    def spy(self, path, params):
        page = original_get(self, path, params)
        pages.append(page)
        return page

    Transport.get = spy
    try:
        try:
            rec = client(2).quote(fairmont, stay)
        except (HotelsHTTPError, PayloadError, QueryError, UnknownEntity) as e:
            check("network", "quote the Fairmont, 2 adults, +45 days, 2 nights", False, f"{HOST} unreachable or blocking: {e}")
            return
        check("network", "a healthy live page still carries ds:2 (the block marker's validity)",
              pages and RESULTS_MARKER in pages[-1], "the RESULTS_MARKER is no longer on a healthy page — re-probe")
        check("network", "the live echo equals the requested stay", rec.echo.matches(stay), f"got {rec.echo}")
        check("network", "at least three sellers list the stay", len(rec.sellers) >= 3, f"got {len(rec.sellers)}")
        cheapest, _ = cheapest_seller(rec.sellers, "incl")
        figure = comparable(cheapest.nightly, "incl") if cheapest else None
        check("network", "the cheapest comparable nightly figure is a positive float",
              isinstance(figure, float) and figure > 0, f"got {figure}")
        both = [s for s in rec.sellers if s.basis == "both"]
        check("network", f"every two-basis row ({len(both)}) has incl_tax > ex_tax",
              len(both) >= 1 and all(s.nightly.incl_tax > s.nightly.ex_tax for s in both),
              f"got {[(s.seller, s.nightly.ex_tax, s.nightly.incl_tax) for s in both][:5]}")
        check("network", "breakdown[3] equals the headline row's stay incl-tax (±0.01)",
              rec.breakdown is not None and rec.headline is not None and rec.headline.stay is not None
              and abs(rec.breakdown.total - (rec.headline.stay.incl_tax or rec.headline.stay.amount or 0)) < 0.01,
              f"got {rec.breakdown} vs {rec.headline and rec.headline.stay}")
        cad = comparable(cheapest.nightly, "incl") if cheapest else None

        try:
            child = client(2).quote(fairmont, Stay(checkin, checkin + timedelta(days=2), 2, (5,), "CAD"))
            check("network", "with --child-age 5 the echo carries [5] and the seller count does not grow",
                  child.echo.child_ages == (5,) and len(child.sellers) <= len(rec.sellers),
                  f"echo {child.echo.child_ages}, {len(child.sellers)} vs {len(rec.sellers)} sellers")
        except (HotelsHTTPError, PayloadError, QueryError, UnknownEntity) as e:
            check("network", "quote with a child", False, f"{HOST} unreachable or blocking: {e}")

        try:
            usd = client(2).quote(fairmont, Stay(checkin, checkin + timedelta(days=2), 2, (), "USD"))
            usd_cheapest, _ = cheapest_seller(usd.sellers, "incl")
            usd_figure = comparable(usd_cheapest.nightly, "incl") if usd_cheapest else None
            check("network", "with --currency USD the page prices in USD and the number is strictly smaller than CAD",
                  usd.currency == "USD" and usd.echo.currency == "USD" and cad and usd_figure and usd_figure < cad,
                  f"CAD {cad}, USD {usd_figure}, currency {usd.currency}")
        except (HotelsHTTPError, PayloadError, QueryError, UnknownEntity) as e:
            check("network", "quote in USD", False, f"{HOST} unreachable or blocking: {e}")

        try:
            samesun = client(2).quote(parse_hotel_id(SAMESUN_TOKEN), Stay(checkin, checkin + timedelta(days=1), 2, (), "CAD"))
            check("network", "the Samesun via its CID-only token resolves to the Samesun", "Samesun" in samesun.name, f"got {samesun.name!r}")
        except (HotelsHTTPError, PayloadError, QueryError, UnknownEntity) as e:
            check("network", "quote via a CID-only token", False, f"{HOST} unreachable or blocking: {e}")
    finally:
        Transport.get = original_get

    code, out, err = run_cli(["shortlist", "--ids-from", os.path.join(FIXTURES, GMAPS_FULL), "--limit", "2",
                              "--checkin", checkin.isoformat(), "--checkout", (checkin + timedelta(days=1)).isoformat(), "--json"],
                             pin=False)
    payload = as_json(out, err) or {}
    check("network", "shortlist --ids-from the gmaps capture prices two hotels, source ids-from",
          code == 0 and payload.get("priced") == 2 and payload.get("source") == "ids-from",
          f"exit {code}: {HOST} unreachable or blocking: {payload.get('error') or str(payload)[:200]}")

    code, out, err = run_cli(["doctor", "--json"], pin=False)
    payload = as_json(out, err) or {}
    check("network", "doctor exits 0, reports a healthy page and a matched echo",
          code == 0 and payload.get("healthy_page") is True and payload.get("echo_matched") is True
          and payload.get("transport") in ("requests", "curl", "urllib"),
          f"exit {code}: {HOST} unreachable or blocking: {payload.get('error') or str(payload)[:300]}")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip every test that opens a socket")
    args = parser.parse_args()

    print("google-hotels self-check")
    groups = [
        test_encoders, test_parser_values, test_headline_and_minimum, test_seller_union, test_echo,
        test_validators, test_block_and_unknown, test_guards, test_parser_hardening, test_gmaps_reader, test_commands,
        test_exit_invariants, test_degraded_sandbox, test_transport_contract, test_review_gaps, test_review_d_gaps, test_launcher,
        test_documented_fields, test_display, test_amenities, test_limits_and_budget,
    ]
    for group in groups:
        try:
            group()
        except Exception as e:  # noqa: BLE001 - a crashed group is a failure, not the end of the run
            check("offline", f"{group.__name__} ran to completion", False, f"crashed with {type(e).__name__}: {e}")
    if not args.offline:
        try:
            test_live()
        except Exception as e:  # noqa: BLE001
            check("network", "test_live ran to completion", False, f"crashed with {type(e).__name__}: {e}")
    else:
        print("\n[network] skipped (--offline)")

    print(f"\n{_passed} passed, {len(_failures)} failed")
    for failure in _failures:
        print(f"  FAIL {failure}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
