#!/usr/bin/env python3
"""Self-check for the uber-eats skill.

    python3 test_ubereats.py            # everything
    python3 test_ubereats.py --offline  # no sockets; safe in any sandbox

Two labelled groups:

    [offline]  pure logic against saved real API responses — location and
               cookie, the transport (allowlist, Cloudflare, the 200-failure
               envelope, redirects, retries, budget), feed parsing, the deal
               grammar, the menu parser's traps, item options, every command
               through cli.main with a stubbed transport, the hostile
               environment, and "the docs agree with the code". No network.
    [network]  six live requests to www.ubereats.com, 1 s apart. A failure
               here names the host, so "Cloudflare is challenging this IP"
               stays distinguishable from "the skill is broken".

What these tests are for (design 04): the dangerous bug is not a crash — it is
the answer that looks plausible and is wrong. In order of harm: a sale price
reported as the list price, or `was` backwards; exit 1 on an error (a watch
that polls a Cloudflare challenge all night); a non-allowlisted endpoint
reachable; a dish counted three times; a BOGO ranked at half; a partial feed
reported as the whole market. So the offline group asserts *exact values from
known captures*, mutates real payloads to prove the guards fire, and drives
every exit code through the CLI.

Rules this file keeps:

* Every `all(...)` states the count it expected, so a comprehension over an
  empty list cannot pass vacuously.
* Stubs sit one layer BELOW the unit under test: `Transport.call` to test the
  client and the CLI; the session / subprocess / urllib to test the transport.
  A test that patches the function it names cannot fail.
* No `"error" in stderr` — every failure asserts the documented code and the
  documented message.

Exits non-zero on any failure, so it can gate an install.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from ueats import cli  # noqa: E402
from ueats import client as client_mod  # noqa: E402
from ueats import http as uehttp  # noqa: E402
from ueats import ids  # noqa: E402
from ueats import location as locmod  # noqa: E402
from ueats import parse_feed, parse_item, parse_store  # noqa: E402
from ueats.client import Ambiguous, Client, QueryError  # noqa: E402
from ueats.http import (  # noqa: E402
    ALLOWED_ENDPOINTS, HOST, Blocked, LookupFailure, NotAllowed, PayloadError, RequestBudgetError,
    Transport, UEHTTPError, looks_blocked,
)
from ueats.location import LocationCache, from_token, is_token, token  # noqa: E402
from ueats.model import (  # noqa: E402
    Deal, Dish, Location, Money, Option, OptionGroup, PlaceCandidate, Store, StoreRow, fold, from_price,
)

HERE = os.path.dirname(os.path.realpath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
RECIPES_MD = os.path.join(SKILL_DIR, "recipes.md")

# -- the captures (fixtures/README.txt says how each was trimmed) -------------
FEED = "feed_union_station.json"                 # feed2.json.gz trimmed: 40 items, 69 store refs
FEED_EARLIER = "feed_union_station_earlier.json"  # feed.json.gz, 17 min earlier, same trim
FEED_PAGE2 = "feed_page2.json"                   # g4_pageInfo.json: 8 stores + meta
HIMALAYAN = "store_himalayan_delivery.json"      # 109 entries → 98 dishes, Samosa Chaat note
HIMALAYAN_PICKUP = "store_himalayan_pickup.json"
ALBERTS = "store_alberts_pctoff.json"            # 25% off, fractional cents, line-through was
TIKKA = "store_tikka_bogo.json"                  # BOGO markers, triple-listed bowls
BLONDIES = "store_blondies.json"                 # pizza menu
ITEM_PIZZA = "item_blondies_pizza.json"          # 8 option groups
ITEM_CHILI = "item_himalayan_chili.json"         # one free group
MAPS = "maps_union_station.json"                 # mapsSearchV1: 5 candidates
DELIVERYLOC = "deliveryloc_union_station.json"   # getDeliveryLocationV1: the cookie object
INVALID_STORE = "invalid_store.json"             # 200 + status failure / invalid_store_uuid
CHALLENGE = "cloudflare_challenge.html"          # the 403 challenge page
SEARCH_IGNORED = "search_ignored.json"           # documents that search is NOT used

TIM_UUID = "80a1f654-f869-40b2-80bc-f15ba251c4ad"
TIM_URL_ID = "gKH2VPhpQLKAvPFbolHErQ"
HIMALAYAN_UUID = "29da06d5-d49f-5708-9202-04614828ab60"
HIMALAYAN_URL_ID = "KdoG1dSfVwiSAgRhSCirYA"
ALBERTS_UUID = "0fa335e9-18ac-4411-a744-2ee747f02e69"
TIKKA_UUID = "b7988039-377b-5c9d-920b-72bf9898ad4f"
BLONDIES_UUID = "76c06f45-e7c2-5310-a460-d5d5d932f67a"
BLONDIES_URL_ID = "dsBvRefCUxCkYNXV2TL2eg"
PIZZA_UUID = "ec47ba3e-6f0d-5668-a13b-7268aef72d00"        # Blondies Custom Pizza - 16" Large
CHILI_UUID = "240b2240-d54a-436c-bbc0-12e7a6a1e41a"        # Honey Chicken Chili
JERK_UUID = "0b946636-d1d8-4466-a544-bac3d4daef91"         # Jerk Chicken Special, 1087.5
BUTTER_BOWL_UUID = "53f1d076-4e72-4c5f-841c-bb473de92720"  # Butter Chicken Hefty Bowl, listed 3x
SAMOSA_UUID = "457128a3-9246-4539-b0c2-416603e63e08"       # Samosa Chaat, "Earn $7 Uber Cash"
RANDOM_UUID = "11111111-2222-4333-8444-555555555555"

#: Counted by hand over the trimmed feed (fixtures/README.txt): REGULAR_STORE +
#: carousel + FEATURED_STORES rows, then distinct storeUuids.
FEED_REFS, FEED_DISTINCT = 69, 57
FEED_EARLIER_REFS, FEED_EARLIER_DISTINCT = 69, 63

#: The pinned clock: the capture day, a Sunday, 14:02 local (Himalayan is
#: open 12:00–23:30 on Sundays; Albert's 11:00–23:30).
PROBE_AT = datetime(2026, 9, 13, 14, 2).astimezone()

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


def fixture_text(name: str) -> str:
    path = os.path.join(FIXTURES, name)
    if os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_cache: dict[str, dict] = {}


def envelope(name: str) -> dict:
    """The whole {"status", "data"} body of a JSON fixture (a fresh copy)."""
    if name not in _cache:
        _cache[name] = json.loads(fixture_text(name))
    return copy.deepcopy(_cache[name])


def data(name: str):
    return envelope(name)["data"]


def union_station() -> Location:
    d = data(DELIVERYLOC)
    a = d["address"]
    return Location(line1=a["address1"], line2=a["address2"], reference=d["reference"],
                    reference_type=d["referenceType"], latitude=d["latitude"], longitude=d["longitude"],
                    formatted=a.get("eaterFormattedAddress", ""))


UNION = union_station()
UNION_TOKEN = token(UNION)


def walk_feed_stores(feed: dict) -> list[dict]:
    """Every raw store dict a feed carries, in feed order — written here, not
    with parse_feed, so the dedupe literals are independent of the code."""
    out = []
    for fi in feed["feedItems"]:
        if fi["type"] == "REGULAR_STORE":
            out.append(fi["store"])
        elif fi["type"] == "REGULAR_CAROUSEL":
            out.extend(fi["carousel"]["stores"])
        elif fi["type"] == "FEATURED_STORES":
            out.extend(fi["payload"]["stores"])
    return out


def catalog_entries(store: dict) -> list[dict]:
    out = []
    for blocks in store["catalogSectionsMap"].values():
        for b in blocks:
            out.extend(b["payload"]["standardItemsPayload"].get("catalogItems") or [])
    return out


# -- stubs -------------------------------------------------------------------

STORE_FIXTURES = {HIMALAYAN_UUID: HIMALAYAN, ALBERTS_UUID: ALBERTS, TIKKA_UUID: TIKKA, BLONDIES_UUID: BLONDIES}
ITEM_FIXTURES = {PIZZA_UUID: ITEM_PIZZA, CHILI_UUID: ITEM_CHILI}


def default_responder(n: int, endpoint: str, body: dict, location):
    """What the live API answered for each call, from the captures."""
    if endpoint == "mapsSearchV1":
        return data(MAPS)
    if endpoint == "getDeliveryLocationV1":
        return data(DELIVERYLOC)
    if endpoint == "getFeedV1":
        return data(FEED_PAGE2) if body.get("pageInfo") else data(FEED)
    if endpoint == "getStoreV1":
        uuid = body.get("storeUuid")
        if uuid == HIMALAYAN_UUID and body.get("diningMode") == "PICKUP":
            return data(HIMALAYAN_PICKUP)
        if uuid in STORE_FIXTURES:
            return data(STORE_FIXTURES[uuid])
        raise LookupFailure("Uber Eats does not know that store id (invalid_store_uuid)")
    if endpoint == "getMenuItemV1":
        uuid = body.get("menuItemUuid")
        if uuid in ITEM_FIXTURES:
            return data(ITEM_FIXTURES[uuid])
        return {"uuid": uuid, "customizationsList": [], "priceTagline": None, "itemPromotion": None}
    raise AssertionError(f"unexpected endpoint {endpoint}")


@contextlib.contextmanager
def call_stub(responder=None):
    """Swap Transport.call for `responder(n, endpoint, body, location)`; yields the call list.

    One layer below Client: everything in client.py and cli.py runs for real —
    planning, budget accounting (the stub charges the budget through the real
    `_spend`, with the throttle off), parsing, rendering, exit codes — and
    only the socket is replaced. `responder` may raise.
    """
    calls: list[tuple] = []
    original = Transport.call
    responder = responder or default_responder

    def stub(self, endpoint, body, location=None):
        calls.append((endpoint, copy.deepcopy(body), location))
        self.throttle = 0
        self._spend()
        return responder(len(calls) - 1, endpoint, body, location)

    Transport.call = stub
    try:
        yield calls
    finally:
        Transport.call = original


def responder_with(**overrides):
    """A responder that answers `endpoint=fn_or_value` from overrides, else the default."""
    def respond(n, endpoint, body, location):
        if endpoint in overrides:
            value = overrides[endpoint]
            if callable(value):
                return value(n, body, location)
            if isinstance(value, BaseException):
                raise value
            return copy.deepcopy(value)
        return default_responder(n, endpoint, body, location)
    return respond


@contextlib.contextmanager
def pinned_clock(at: datetime = PROBE_AT):
    saved = client_mod.now
    client_mod.now = lambda: at
    try:
        yield
    finally:
        client_mod.now = saved


_SCRATCH = tempfile.mkdtemp(prefix="ueats-selfcheck-")


@contextlib.contextmanager
def cache_dir(path: str | None = None):
    """Point the skill's cache at a fresh directory (or `path`) for one run."""
    saved = os.environ.get("UBEREATS_CACHE_DIR")
    os.environ["UBEREATS_CACHE_DIR"] = path or tempfile.mkdtemp(prefix="cache-", dir=_SCRATCH)
    try:
        yield os.environ["UBEREATS_CACHE_DIR"]
    finally:
        if saved is None:
            os.environ.pop("UBEREATS_CACHE_DIR", None)
        else:
            os.environ["UBEREATS_CACHE_DIR"] = saved


def run_cli(argv: list[str], pin: bool = True, cache: str | None = None) -> tuple[int, str, str]:
    """cli.main with stdout/stderr captured, the clock pinned and a fresh cache dir."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            (pinned_clock() if pin else contextlib.nullcontext()), cache_dir(cache):
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


def run_json(argv: list[str], responder=None, **kw):
    """(exit code, payload or None, calls, stderr) for one stubbed --json run."""
    with call_stub(responder) as calls:
        code, out, err = run_cli(argv + ["--json"], **kw)
    return code, as_json(out, err), calls, err


def transport(max_requests: int = 25) -> Transport:
    return Transport(max_requests=max_requests, throttle=0)


def client(max_requests: int = 25, cache: LocationCache | None = None) -> Client:
    return Client(transport(max_requests), cache)


# -- synthetic fixtures ------------------------------------------------------
# Labelled synthetic_*: built from the captures, never observed as such.


def synthetic_closed_store() -> dict:
    """Himalayan, closed until 16:30 today (a Sunday), with hours the client can read."""
    d = data(HIMALAYAN)
    d["isOpen"], d["isOrderable"], d["closedMessage"] = False, False, "Currently closed"
    d["hours"] = [{"dayRange": "Sunday", "sectionHours": [{"startTime": 990, "endTime": 1410}]},
                  {"dayRange": "Monday - Saturday", "sectionHours": [{"startTime": 720, "endTime": 1410}]}]
    return d


def synthetic_unclear_item() -> dict:
    """An Albert's discounted item whose markup has two amounts and no line-through."""
    d = data(ALBERTS)
    for it in catalog_entries(d):
        if it["uuid"] == JERK_UUID:
            it["priceTagline"]["textFormat"] = '<span><span style="color:#05944F">$10.88 </span><span style="color:#757575">$14.50</span></span>'
    return d


def synthetic_required_paid_group() -> dict:
    """The pizza item with a required group (min 2) whose two cheapest in-stock options are +1.50 and
    +3.00, a cheaper sold-out option (+0.50) that must be skipped, and a nested childCustomizationList."""
    d = data(ITEM_PIZZA)
    d["customizationsList"] = [
        {"uuid": "g-size", "title": "Choose Two Sides", "minPermitted": 2, "maxPermitted": 2, "options": [
            {"uuid": "o-soldout", "title": "Day-old (sold out)", "price": 50, "isSoldOut": True,
             "minPermitted": 0, "maxPermitted": 1, "defaultQuantity": 0, "childCustomizationList": []},
            {"uuid": "o-cheap", "title": "Small", "price": 150, "isSoldOut": False,
             "minPermitted": 0, "maxPermitted": 1, "defaultQuantity": 0, "childCustomizationList": [
                 {"uuid": "g-nested", "title": "Crust", "minPermitted": 0, "maxPermitted": 1, "options": [
                     {"uuid": "o-thin", "title": "Thin", "price": 0, "isSoldOut": False,
                      "minPermitted": 0, "maxPermitted": 1, "defaultQuantity": 0, "childCustomizationList": []}]}]},
            {"uuid": "o-mid", "title": "Medium", "price": 300, "isSoldOut": False,
             "minPermitted": 0, "maxPermitted": 1, "defaultQuantity": 0, "childCustomizationList": []},
            {"uuid": "o-dear", "title": "Large", "price": 400, "isSoldOut": False,
             "minPermitted": 0, "maxPermitted": 1, "defaultQuantity": 0, "childCustomizationList": []},
        ]},
        {"uuid": "g-opt", "title": "Add Extra Meat Toppings", "minPermitted": 0, "maxPermitted": 7, "options": [
            {"uuid": "o-bacon", "title": "Bacon", "price": 575, "isSoldOut": False,
             "minPermitted": 0, "maxPermitted": 99, "defaultQuantity": 0, "childCustomizationList": []}]},
    ]
    return d


def synthetic_out_of_area() -> dict:
    d = data(FEED)
    d["isInServiceArea"] = False
    return d


# ---------------------------------------------------------------------------
# [offline] §2.1 location and cookie
# ---------------------------------------------------------------------------


def test_location() -> None:
    print("\nlocation: candidates, the cookie, the token, the cache")
    t = transport()
    with call_stub() as calls:
        cands = locmod.search(t, "Union Station Toronto")
    check("offline", "locate parses all 5 candidates, uber_places and google_places alike",
          len(cands) == 5 and cands[0].provider == "uber_places" and cands[2].provider == "google_places"
          and cands[0].id == "180933fc-e611-398d-9512-2a86a6ecca45" and cands[2].id.startswith("ChIJ")
          and cands[0].line1 == "Toronto Union Station Train Station"
          and cands[0].line2 == "65 Front St W, Toronto, ON M5J 1E6",
          f"got {[(c.provider, c.id[:8], c.line1) for c in cands]}")
    check("offline", "search sent mapsSearchV1 with {query} and nothing else",
          calls == [("mapsSearchV1", {"query": "Union Station Toronto"}, None)], f"got {calls}")
    with call_stub() as calls:
        loc = locmod.resolve(t, cands[0])
    check("offline", "resolve sends getDeliveryLocationV1 with placeReferenceType/placeId/provider",
          calls and calls[0][0] == "getDeliveryLocationV1"
          and calls[0][1] == {"placeReferenceType": "uber_places", "placeId": cands[0].id, "provider": "uber_places"},
          f"got {calls}")
    check("offline", "the Location carries Uber's coordinates (43.6452223, -79.3806428) and the address lines",
          loc.latitude == 43.6452223 and loc.longitude == -79.3806428
          and loc.line1 == "Toronto Union Station Train Station" and loc.line2 == "65 Front St W, Toronto, ON M5J 1E6"
          and loc.reference == cands[0].id and loc.reference_type == "uber_places", f"got {loc}")
    d = data(DELIVERYLOC)
    d["latitude"] = None
    with call_stub(responder_with(getDeliveryLocationV1=d)):
        err = raised(locmod.resolve, transport(), cands[0])
    check("offline", "a location without coordinates is refused (PayloadError), never built (01 §3.2: 2,263 mi)",
          isinstance(err, PayloadError), f"got {err!r}")
    with call_stub(responder_with(mapsSearchV1=[])):
        none = locmod.search(transport(), "zzzz")
    check("offline", "search with no candidates returns [] (locate turns that into exit 2)", none == [])
    check("offline", "search refuses blank text without a request", raises(ValueError, locmod.search, transport(), "  "))

    # the cookie: the exact object, no spaces, URL-encoded
    cookie = loc.cookie()
    expected = {"address": {"address1": loc.line1, "address2": loc.line2, "aptOrSuite": "",
                            "eaterFormattedAddress": "65 Front St W, Toronto, ON M5J 1E6, CA",
                            "subtitle": loc.line2, "title": loc.line1, "uuid": ""},
                "latitude": 43.6452223, "longitude": -79.3806428, "reference": loc.reference,
                "referenceType": "uber_places", "type": "uber_places", "source": "manual_auto_complete"}
    check("offline", "cookie() is the uev2.loc object of 01 §3.2, key for key", cookie == expected,
          f"got {cookie}")
    header = uehttp.cookie_header(loc)
    check("offline", "the Cookie header is uev2.loc=<url-encoded compact JSON> with no spaces",
          header.startswith("uev2.loc=%7B%22address%22%3A%7B%22address1%22%3A") and " " not in header
          and "%20" in header and "+" not in header.split("=", 1)[1].replace("%2B", ""),
          f"got {header[:80]}")
    odd = Location(line1="Chez L'Ami & Co", line2="12 Rue Émile, Montréal", reference="r", reference_type="uber_places",
                   latitude=45.5, longitude=-73.6)
    h = uehttp.cookie_header(odd)
    check("offline", "URL-encoding: ' → %27, & → %26, é → %C3%A9, and the decoded value round-trips",
          "%27" in h and "%26" in h and "%C3%A9" in h and "&" not in h.split("=", 1)[1]
          and json.loads(__import__("urllib.parse").parse.unquote(h.split("=", 1)[1])) == odd.cookie(), f"got {h}")

    # the token
    tok = token(loc)
    check("offline", "token is unpadded base64url of the compact cookie JSON",
          re.fullmatch(r"[A-Za-z0-9_-]+", tok) is not None and "=" not in tok
          and __import__("base64").urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
          == json.dumps(loc.cookie(), separators=(",", ":"), ensure_ascii=False), f"got {tok[:40]}")
    check("offline", "from_token(token(loc)) == loc, and is_token says so", from_token(tok) == loc and is_token(tok))
    check("offline", "an address is never mistaken for a token",
          not is_token("65 Front St W, Toronto") and not is_token("UnionStation") and not is_token("")
          and raises(ValueError, from_token, "65 Front St W") and raises(ValueError, from_token, "e30"))

    # the cache
    d1 = tempfile.mkdtemp(prefix="loc-", dir=_SCRATCH)
    cache = LocationCache(d1)
    cache.put("Union Station Toronto", loc)
    check("offline", "cache write → read under a temp dir, keyed by folded text",
          LocationCache(d1).get("union  station, TORONTO") == loc and os.path.exists(os.path.join(d1, "locations.json")),
          f"files {os.listdir(d1)}")
    with open(os.path.join(d1, "locations.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    for entry in doc["locations"].values():
        entry["saved_at"] = time.time() - 31 * 86400
    with open(os.path.join(d1, "locations.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    check("offline", "an entry older than 30 days is ignored", LocationCache(d1).get("Union Station Toronto") is None)
    ro = tempfile.mkdtemp(prefix="ro-", dir=_SCRATCH)
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
    try:
        mem = LocationCache(os.path.join(ro, "cache"))
        mem.put("x", loc)
        got = mem.get("x")
        left = os.listdir(ro)
    finally:
        os.chmod(ro, stat.S_IRWXU)
    check("offline", "no writable directory → in-memory cache, nothing written", got == loc and left == [] and mem.directory is None,
          f"got {got}, left {left}")
    check("offline", "the cache stores Locations only", raises(TypeError, cache.put, "y", {"feedItems": []}))
    check("offline", "default_cache_dir honours UBEREATS_CACHE_DIR",
          (lambda p: (os.environ.__setitem__("UBEREATS_CACHE_DIR", p), locmod.default_cache_dir() == p,
                      os.environ.pop("UBEREATS_CACHE_DIR"))[1])(tempfile.mkdtemp(prefix="env-", dir=_SCRATCH)))


# ---------------------------------------------------------------------------
# [offline] §2.2 transport — stubbed at the session / subprocess / urllib
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched(**attrs):
    """Swap module attributes on ueats.http, and always put them back."""
    saved = {name: getattr(uehttp, name) for name in attrs}
    for name, value in attrs.items():
        setattr(uehttp, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(uehttp, name, value)


def _response(status, text="", headers=None):
    return types.SimpleNamespace(status_code=status, headers=headers or {}, text=text)


def _ok_body(payload) -> str:
    return json.dumps({"status": "success", "data": payload})


def _no_sleep():
    import time as _time
    slept: list[float] = []
    return types.SimpleNamespace(monotonic=_time.monotonic, sleep=lambda s: slept.append(s), time=_time.time), slept


def _requests_call(responder, endpoint="getStoreV1", body=None, location=None, max_requests=25):
    """Transport.call on the `requests` path with session.post stubbed by `responder(url, **kw)`.

    Returns (data, error, transport, posts, sleeps). The transport's own code —
    allowlist, budget, headers, classification, retries — runs for real.
    """
    t = transport(max_requests)
    posts: list[tuple] = []

    def post(url, **kw):
        posts.append((url, kw))
        return responder(url, **kw)

    if t._session is None:
        return None, RuntimeError("requests is not installed here"), t, posts, []
    t._session.post = post
    clock, slept = _no_sleep()
    out = error = None
    with _patched(time=clock):
        try:
            out = t.call(endpoint, body if body is not None else {"storeUuid": HIMALAYAN_UUID}, location)
        except Exception as e:  # noqa: BLE001 - the outcome under test
            error = e
    return out, error, t, posts, slept


def _fake_shutil(curl_path):
    return types.SimpleNamespace(which=lambda name: curl_path if name == "curl" else None)


def _fake_subprocess(responses, raises_oserror=False):
    """A `subprocess` whose run() answers from `responses` (a list of (returncode, stdout, stderr)) in turn."""
    seen: list[tuple[list, bytes]] = []
    queue = list(responses)

    def run(argv, **kwargs):
        seen.append((argv, kwargs.get("input", b"")))
        if raises_oserror:
            raise OSError(8, "Exec format error")
        rc, out, err = queue.pop(0) if len(queue) > 1 else queue[0]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err, args=argv)

    return types.SimpleNamespace(run=run), seen


def _curl_stdout(status: int, body: str, headers: str = "") -> bytes:
    """What curl -i -w '\\n%{http_code}' prints for one response."""
    return (f"HTTP/2 {status}\r\n{headers}\r\n\r\n{body}\n{status}").encode()


def _curl_call(responses, endpoint="getStoreV1", body=None, location=None, max_requests=25, curl="/usr/bin/curl",
               oserror=False, urllib_answer=None):
    """Transport.call with no `requests`, curl stubbed: (data, error, transport, argvs, urllib calls, sleeps)."""
    t = transport(max_requests)
    t._session = None
    fetched: list[tuple] = []

    def urllib_stub(url, payload, headers):
        fetched.append((url, payload, headers))
        if urllib_answer is None:
            return data(HIMALAYAN)
        if isinstance(urllib_answer, BaseException):
            raise urllib_answer
        return urllib_answer

    t._post_urllib = urllib_stub
    fake, argvs = _fake_subprocess(responses, oserror)
    clock, slept = _no_sleep()
    out = error = None
    with _patched(shutil=_fake_shutil(curl), subprocess=fake, time=clock):
        try:
            out = t.call(endpoint, body if body is not None else {"storeUuid": HIMALAYAN_UUID}, location)
        except Exception as e:  # noqa: BLE001
            error = e
    return out, error, t, argvs, fetched, slept


def test_transport() -> None:
    print("\ntransport: allowlist, Cloudflare, the 200-failure envelope, redirects, retries, budget, headers")
    check("offline", "ALLOWED_ENDPOINTS is exactly the five read endpoints",
          ALLOWED_ENDPOINTS == {"mapsSearchV1", "getDeliveryLocationV1", "getFeedV1", "getStoreV1", "getMenuItemV1"},
          f"got {sorted(ALLOWED_ENDPOINTS)}")
    check("offline", "THROTTLE_SECONDS is 1.0 and Transport() defaults to it; MAX_ATTEMPTS 3; hard cap 25",
          uehttp.THROTTLE_SECONDS == 1.0 and Transport().throttle == 1.0 and uehttp.MAX_ATTEMPTS == 3
          and uehttp.HARD_CAP_REQUESTS == 25 and cli.HARD_CAP == 25,
          f"got {uehttp.THROTTLE_SECONDS}, {Transport().throttle}, {uehttp.MAX_ATTEMPTS}")

    # -- allowlist: refused before any socket, on every path -------------------
    for bad in ("getCartV1", "addItemsToCartV1", "createDraftOrderV2", "setDeliveryLocationV1", "getFeedV2", ""):
        _, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({})), endpoint=bad)
        check("offline", f"{bad or '<empty>'!r} raises NotAllowed with zero session calls and nothing charged",
              isinstance(err, NotAllowed) and posts == [] and t.requests_made == 0, f"got {err!r}, {len(posts)} posts")
    _, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(200, _ok_body({})), b"")], endpoint="getCartV1")
    check("offline", "…and on the curl/urllib path too (no subprocess spawned, no urllib call)",
          isinstance(err, NotAllowed) and argvs == [] and fetched == [] and t.requests_made == 0, f"got {err!r}")
    for good in sorted(ALLOWED_ENDPOINTS):
        out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({"k": 1})), endpoint=good,
                                               body={"q": 1})
        check("offline", f"{good} passes the allowlist and returns the data object",
              err is None and out == {"k": 1} and len(posts) == 1 and posts[0][0].endswith(f"/_p/api/{good}?localeCode=ca"),
              f"got {err!r}, {posts and posts[0][0]}")

    # -- Cloudflare -----------------------------------------------------------
    page = fixture_text(CHALLENGE)
    check("offline", "the challenge fixture carries both markers", "Just a moment" in page and "_cf_chl_opt" in page)
    check("offline", "looks_blocked: 403 + challenge body → True; 403 + plain body → False; 200 + challenge body → True",
          looks_blocked(403, page) and not looks_blocked(403, "Forbidden") and looks_blocked(200, page))
    check("offline", "looks_blocked: the header cf-mitigated: challenge decides on its own; a JSON body never blocks",
          looks_blocked(403, "", {"cf-mitigated": "challenge"}) and looks_blocked(200, "{}", {"CF-Mitigated": "challenge"})
          and not looks_blocked(200, '{"status":"success","data":{"title":"Just a moment"}}')
          and not looks_blocked(200, '{"status":"success","data":{"x":"_cf_chl_opt"}}'))
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(403, page, {"cf-mitigated": "challenge"}))
    check("offline", "403 + challenge → Blocked (a UEHTTPError) whose message names the bot protection, exactly 1 call",
          isinstance(err, Blocked) and isinstance(err, UEHTTPError) and "bot protection" in str(err)
          and "Cloudflare" in str(err) and len(posts) == 1 and t.requests_made == 1, f"got {err!r}, {len(posts)} posts")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(403, "Forbidden"))
    check("offline", "403 without the challenge is UEHTTPError (not Blocked) saying 403, never retried (exactly 1 call)",
          isinstance(err, UEHTTPError) and not isinstance(err, Blocked) and "403" in str(err) and len(posts) == 1,
          f"got {err!r}, {len(posts)} posts")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, page))
    check("offline", "a 200 carrying the challenge page is Blocked (defensive)", isinstance(err, Blocked), f"got {err!r}")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(429, "", {"Retry-After": "120"}))
    check("offline", "429 is never retried and says wait, not retry", isinstance(err, UEHTTPError) and len(posts) == 1
          and "429" in str(err) and "120" in str(err) and "not 'nothing found'" in str(err).lower().replace("‘", "'"),
          f"got {err!r}")

    # -- the 200 envelope -----------------------------------------------------
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, fixture_text(INVALID_STORE)))
    check("offline", "200 + status failure / invalid_store_uuid → LookupFailure (exit 2), one call",
          isinstance(err, LookupFailure) and not isinstance(err, UEHTTPError) and "invalid_store_uuid" in str(err)
          and len(posts) == 1, f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"status":"failure","data":{"message":"rate_limited"}}'))
    check("offline", "any other failure status → PayloadError (exit 3), naming the message",
          isinstance(err, PayloadError) and "rate_limited" in str(err), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, "<html>ok</html>"))
    check("offline", "a 200 that is not JSON → PayloadError", isinstance(err, PayloadError), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"data": {}}'))
    check("offline", "JSON without the {status, data} envelope → PayloadError", isinstance(err, PayloadError), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, _ok_body([1, 2])), endpoint="mapsSearchV1")
    check("offline", "mapsSearchV1's data is a list and comes back as one", err is None and out == [1, 2], f"got {err!r}")

    # -- redirects: never followed, on all three paths -----------------------
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(302, "", {"Location": "https://www.ubereats.com/ca"}))
    check("offline", "requests: a 302 is raised naming the Location, never followed, one call",
          isinstance(err, UEHTTPError) and "302" in str(err) and "https://www.ubereats.com/ca" in str(err) and len(posts) == 1
          and posts[0][1].get("allow_redirects") is False, f"got {err!r}, kwargs {posts and posts[0][1].keys()}")
    out, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(200, _ok_body(data(HIMALAYAN))), b"")])
    argv = argvs[0][0] if argvs else []
    check("offline", "curl argv: no -L, --max-redirs 0, -X POST, body on stdin, -i for headers",
          err is None and "-L" not in argv and "--location" not in argv and "--max-redirs" in argv
          and argv[argv.index("--max-redirs") + 1] == "0" and "POST" in argv and "-i" in argv
          and argvs[0][1] == json.dumps({"storeUuid": HIMALAYAN_UUID}, separators=(",", ":")).encode()
          and not any(a in ("-k", "--insecure") for a in argv), f"got {err!r}, argv {argv}")
    check("offline", "curl path: data comes back parsed, transport_used curl, nothing reached urllib",
          isinstance(out, dict) and out.get("uuid") == HIMALAYAN_UUID and t.transport_used == "curl" and fetched == [])
    out, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(301, "", "location: https://www.ubereats.com/x"), b"")])
    check("offline", "curl: a 301 is raised with its Location (headers read from -i output), one spawn",
          isinstance(err, UEHTTPError) and "301" in str(err) and "https://www.ubereats.com/x" in str(err) and len(argvs) == 1,
          f"got {err!r}")
    out, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(403, page, "cf-mitigated: challenge"), b"")])
    check("offline", "curl: a challenged 403 is Blocked, one spawn", isinstance(err, Blocked) and len(argvs) == 1, f"got {err!r}")
    check("offline", "urllib installs a redirect handler that refuses every redirect",
          uehttp._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x") is None)

    # -- retries: 5xx up to 3 attempts, within the budget; charged before each --
    seq = iter([500, 502, 503, 200])
    out, err, t, posts, slept = _requests_call(lambda url, **kw: (lambda s: _response(s, _ok_body({"k": 1}) if s == 200 else "down"))(next(seq)))
    check("offline", "three 5xx in a row: exactly 3 attempts, then the 5xx error (no 4th)",
          isinstance(err, UEHTTPError) and "503" in str(err) and len(posts) == 3 and t.requests_made == 3,
          f"got {err!r}, {len(posts)} posts")
    seq = iter([500, 200])
    out, err, t, posts, slept = _requests_call(lambda url, **kw: (lambda s: _response(s, _ok_body({"k": 1}) if s == 200 else "down"))(next(seq)))
    check("offline", "a 5xx then a 200: 2 attempts, data returned, one backoff sleep", err is None and out == {"k": 1}
          and len(posts) == 2 and len(slept) == 1 and slept[0] == uehttp.BACKOFF_SECONDS, f"got {err!r}, slept {slept}")
    seq = iter([500, 500, 500])
    out, err, t, posts, slept = _requests_call(lambda url, **kw: _response(next(seq), "down"), max_requests=2)
    check("offline", "retries stop at the budget: max_requests 2 → 2 attempts, both charged",
          isinstance(err, UEHTTPError) and len(posts) == 2 and t.requests_made == 2, f"got {err!r}, {len(posts)}")
    t = transport(25)
    t._session.post = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    raised(t.call, "getStoreV1", {})
    check("offline", "a failed request is still charged (metered before it leaves)", t.requests_made == 1)
    t = transport(0)
    posts = []
    t._session.post = lambda url, **kw: posts.append(url) or _response(200, _ok_body({}))
    check("offline", "a spent budget raises RequestBudgetError before the socket",
          raises(RequestBudgetError, t.call, "getStoreV1", {}) and posts == [])
    check("offline", "plan() refuses up front when count exceeds what is left, naming --max-requests",
          (lambda e: isinstance(e, RequestBudgetError) and "--max-requests" in str(e))(raised(transport(3).plan, 4, "compare"))
          and raised(transport(3).plan, 3, "x") is None)

    # -- throttle on every path ------------------------------------------------
    for path in ("requests", "curl"):
        t = transport(25)
        t.throttle = 1.0
        if path == "requests":
            t._session.post = lambda url, **kw: _response(200, _ok_body({}))
            fake = None
        else:
            t._session = None
            fake, _ = _fake_subprocess([(0, _curl_stdout(200, _ok_body({})), b"")])
        clock, slept = _no_sleep()
        patches = {"time": clock}
        if fake is not None:
            patches.update(shutil=_fake_shutil("/usr/bin/curl"), subprocess=fake)
        with _patched(**patches):
            t.call("getStoreV1", {})
            t.call("getStoreV1", {})
        check("offline", f"{path} path: the second call waits out the 1.0 s throttle",
              len(slept) == 1 and 0 < slept[0] <= 1.0, f"slept {slept}")

    # -- headers on every path ------------------------------------------------
    def has_headers(h: dict, with_cookie: bool) -> bool:
        low = {str(k).lower(): v for k, v in h.items()}
        ok = low.get("x-csrf-token") == "x" and "application/json" in str(low.get("content-type")) \
            and "Mozilla" in str(low.get("user-agent"))
        if with_cookie:
            return ok and str(low.get("cookie", "")).startswith("uev2.loc=%7B")
        return ok and "cookie" not in low
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({})), location=UNION)
    check("offline", "requests: x-csrf-token, JSON content type, UA and the uev2.loc cookie are sent",
          err is None and has_headers(posts[0][1]["headers"], True), f"got {posts and posts[0][1].get('headers')}")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({})))
    check("offline", "requests: no cookie without a Location", err is None and has_headers(posts[0][1]["headers"], False))
    out, err, t, argvs, _, _ = _curl_call([(0, _curl_stdout(200, _ok_body({})), b"")], location=UNION)
    argv = argvs[0][0]
    hdrs = {a.split(":", 1)[0]: a.split(":", 1)[1].strip() for i, a in enumerate(argv) if i and argv[i - 1] == "-H" and ":" in a}
    check("offline", "curl: the same headers and cookie as -H arguments", err is None and has_headers(hdrs, True), f"got {hdrs}")
    check("offline", "curl: the cookie is a header, not a jar file, and the URL carries localeCode",
          "-b" not in argv and "--cookie" not in argv and argv[-1].endswith("/_p/api/getStoreV1?localeCode=ca"))
    t = transport(25)
    t._session = None
    seen: list[tuple] = []
    original_opener = uehttp.urllib.request.build_opener

    class _Resp:
        status, headers = 200, {"content-type": "application/json"}

        def read(self):
            return _ok_body({"u": 1}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            seen.append((req.full_url, req.data, dict(req.header_items()), req.get_method()))
            return _Resp()

    uehttp.urllib.request.build_opener = lambda *handlers: _Opener()
    try:
        with _patched(shutil=_fake_shutil(None)):
            out = t.call("getStoreV1", {"storeUuid": "s"}, UNION)
    finally:
        uehttp.urllib.request.build_opener = original_opener
    check("offline", "urllib (no requests, no curl): POST with the same headers and cookie, data parsed",
          out == {"u": 1} and len(seen) == 1 and seen[0][3] == "POST" and has_headers(seen[0][2], True)
          and seen[0][1] == b'{"storeUuid":"s"}' and t.transport_used == "urllib", f"got {seen}")
    check("offline", "Transport(locale='us') puts localeCode=us in the URL",
          Transport(max_requests=1, throttle=0, locale="us")._url("getFeedV1").endswith("?localeCode=us"))


# ---------------------------------------------------------------------------
# [offline] review A gaps: transport, location, ids
# ---------------------------------------------------------------------------


def _tok(obj) -> str:
    import base64
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def _http_error(code: int, body: str, headers: dict | None = None):
    import email.message
    import urllib.error
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "err", msg, io.BytesIO(body.encode()))


def _urllib_call(raise_exc=None, body: str | None = None, location=None):
    """Transport.call on the urllib path (no requests, no curl) with the opener stubbed."""
    t = transport(25)
    t._session = None
    seen: list = []

    class _Resp:
        status, headers = 200, {"content-type": "application/json"}

        def read(self):
            return (body or "").encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req)
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    original = uehttp.urllib.request.build_opener
    uehttp.urllib.request.build_opener = lambda *h: _Opener()
    clock, slept = _no_sleep()
    out = error = None
    try:
        with _patched(shutil=_fake_shutil(None), time=clock):
            try:
                out = t.call("getStoreV1", {"storeUuid": "s"}, location)
            except Exception as e:  # noqa: BLE001
                error = e
    finally:
        uehttp.urllib.request.build_opener = original
    return out, error, seen, t


def test_review_a_gaps() -> None:
    print("\nreview A gaps: URL, throttle arithmetic, block edge cases, envelope wording, TLS recovery, urllib, cookies, cache, ids")
    if uehttp.requests is None:
        check("offline", "requests is installed here (the requests-path checks below need it)", False, "requests missing")
        return
    req = uehttp.requests
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({})), endpoint="getFeedV1")
    check("offline", "h05 the exact URL: https://www.ubereats.com/_p/api/getFeedV1?localeCode=ca",
          posts and posts[0][0] == "https://www.ubereats.com/_p/api/getFeedV1?localeCode=ca", f"got {posts and posts[0][0]}")
    # h17 throttle arithmetic with a pinned monotonic clock
    ticks = [100.0, 100.0, 100.3, 100.3]
    slept: list[float] = []
    clock = types.SimpleNamespace(monotonic=lambda: ticks.pop(0) if ticks else 100.3, sleep=lambda s: slept.append(s), time=time.time)
    t = transport(25)
    t.throttle = 1.0
    t._session.post = lambda url, **kw: _response(200, _ok_body({}))
    with _patched(time=clock):
        t.call("getStoreV1", {})
        t.call("getStoreV1", {})
    check("offline", "h17 the second call 0.3 s after the first sleeps exactly the 0.7 s remainder", slept and abs(slept[0] - 0.7) < 1e-9 and len(slept) == 1,
          f"slept {slept}")
    # h22 / h25 block edge cases
    out, err, *_ = _requests_call(lambda url, **kw: _response(403, "<html><head><title>Just a moment...</title></head></html>"))
    check("offline", "h22 a 403 whose body holds only the 'Just a moment' title is Blocked", isinstance(err, Blocked), f"got {err!r}")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(403, "x" * (16 * 1024 + 100) + "_cf_chl_opt"))
    check("offline", "h25 a marker beyond the first 16 KB is not scanned: plain 403 UEHTTPError, not Blocked, one call",
          isinstance(err, UEHTTPError) and not isinstance(err, Blocked) and len(posts) == 1, f"got {err!r}")
    # h27 / h29 envelope wording
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"status":"failure","data":{"message":"We had an issue finding this store. Please try another restaurant","code":"404"}}'))
    check("offline", "h27a failure with code '404' and other wording → LookupFailure (Lane A: the live body for a well-formed unknown UUID)",
          isinstance(err, LookupFailure), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"status":"failure","data":{"message":"invalid_store_uuid"}}'))
    check("offline", "h27b invalid_store_uuid without a code → LookupFailure", isinstance(err, LookupFailure), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"status":"weird"}'))
    check("offline", "h29 {\"status\":\"weird\"} → PayloadError", isinstance(err, PayloadError), f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(200, '{"data": {"x": 1}}'))
    check("offline", "h30 JSON without a status key → PayloadError that names the missing envelope",
          isinstance(err, PayloadError) and "envelope" in str(err), f"got {err!r}")
    out, err, t, posts, _ = _requests_call(lambda url, **kw: _response(300, "", {"Location": "https://www.ubereats.com/choose"}))
    check("offline", "h18 a 300 is a redirect too: raised naming the Location, one call",
          isinstance(err, UEHTTPError) and "300" in str(err) and "https://www.ubereats.com/choose" in str(err) and len(posts) == 1, f"got {err!r}")
    for bad in (" getStoreV1 ", "getstorev1", "GETSTOREV1"):
        _, err, t, posts, _ = _requests_call(lambda url, **kw: _response(200, _ok_body({})), endpoint=bad)
        check("offline", f"h01/h03 the allowlist is exact: {bad!r} → NotAllowed, no call",
              isinstance(err, NotAllowed) and posts == [], f"got {err!r}")
    # h34 curl body on stdin only
    body = {"storeUuid": HIMALAYAN_UUID, "diningMode": "PICKUP"}
    out, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(200, _ok_body({})), b"")], body=body)
    argv, stdin = argvs[0]
    compact = json.dumps(body, separators=(",", ":")).encode()
    check("offline", "h34 curl: the body travels on stdin (-d @-) and never appears in argv",
          err is None and stdin == compact and "-d" in argv and argv[argv.index("-d") + 1] == "@-"
          and not any(HIMALAYAN_UUID in a for a in argv), f"argv {argv}")
    # h38 / h40 / h41 TLS recovery
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    saved_env = {k: os.environ.pop(k, None) for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")}
    try:
        seq = iter([req.exceptions.SSLError("certificate verify failed: expired"), None])
        t = transport(25)
        verifies: list = []

        def post(url, **kw):
            verifies.append(t._session.verify)
            e = next(seq)
            if e is not None:
                raise e
            return _response(200, _ok_body({"k": 1}))
        t._session.post = post
        clock, _ = _no_sleep()
        with _patched(_os_ca_bundle=lambda: bundle, time=clock):
            out = t.call("getStoreV1", {})
        check("offline", "h38 after an SSLError the second attempt verifies with the OS bundle path (never False) and succeeds",
              out == {"k": 1} and verifies == [True, bundle] and t.requests_made == 2, f"got {verifies}, {out}")
        t = transport(25)
        posts: list = []
        t._session.post = lambda url, **kw: posts.append(url) or (_ for _ in ()).throw(req.exceptions.SSLError("certificate verify failed"))
        fake, argvs = _fake_subprocess([(0, _curl_stdout(200, _ok_body({"c": 1})), b"")])
        with _patched(_os_ca_bundle=lambda: bundle, shutil=_fake_shutil("/usr/bin/curl"), subprocess=fake, time=clock):
            out = t.call("getStoreV1", {})
        check("offline", "h40 after two SSLErrors the third attempt goes to curl (subprocess) and returns data; 3 charged",
              out == {"c": 1} and len(posts) == 2 and len(argvs) == 1 and t.requests_made == 3 and t.transport_used == "curl",
              f"posts {len(posts)}, curl {len(argvs)}, {out}")
        t = transport(25)
        posts = []
        t._session.post = lambda url, **kw: posts.append(url) or (_ for _ in ()).throw(req.exceptions.SSLError("certificate verify failed"))
        with _patched(_os_ca_bundle=lambda: bundle, shutil=_fake_shutil(None), time=clock):
            err = raised(t.call, "getStoreV1", {})
        check("offline", "h41 SSLError forever with no curl ends after exactly 2 requests-path calls with a TLS message naming the fix",
              isinstance(err, UEHTTPError) and len(posts) == 2 and "TLS" in str(err) and "truststore" in str(err), f"got {err!r}, {len(posts)}")
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
    # h42 / h43 settled failures: one call
    for label, exc in (("ProxyError", req.exceptions.ProxyError("tunnel refused")),
                       ("NameResolutionError", req.exceptions.ConnectionError("NameResolutionError: Failed to resolve"))):
        out, err, t, posts, _ = _requests_call(lambda url, **kw: (_ for _ in ()).throw(exc))
        check("offline", f"h42/43 {label} is final: exactly 1 call, UEHTTPError naming the host or the proxy",
              isinstance(err, UEHTTPError) and len(posts) == 1 and (HOST in str(err) or "proxy" in str(err)), f"got {err!r}, {len(posts)}")
    # h44 / h45 curl retries
    for code_ in (28, 56):
        out, err, t, argvs, fetched, _ = _curl_call([(code_, b"\n000", b"curl: timeout")])
        check("offline", f"h44 curl exit {code_} is retried to 3 spawns, then UEHTTPError", isinstance(err, UEHTTPError) and len(argvs) == 3,
              f"got {err!r}, {len(argvs)}")
    out, err, t, argvs, fetched, _ = _curl_call([(18, _curl_stdout(200, _ok_body({"partial": 1})), b"partial"), (0, _curl_stdout(200, _ok_body({"ok": 1})), b"")])
    check("offline", "h45 curl exit 18 with a 200 body is not trusted: retried, the second (clean) answer returned",
          out == {"ok": 1} and len(argvs) == 2, f"got {out}, {len(argvs)}, {err!r}")
    # h46 / h47 urllib
    out, err, seen, t = _urllib_call(raise_exc=_http_error(403, fixture_text(CHALLENGE)))
    check("offline", "h46 urllib HTTPError 403 whose body (not headers) carries the challenge → Blocked, one call",
          isinstance(err, Blocked) and len(seen) == 1, f"got {err!r}")
    out, err, seen, t = _urllib_call(raise_exc=_http_error(302, "", {"Location": "https://www.ubereats.com/ca/login"}))
    check("offline", "h47 urllib 302 → UEHTTPError naming the Location, never followed",
          isinstance(err, UEHTTPError) and "https://www.ubereats.com/ca/login" in str(err) and len(seen) == 1, f"got {err!r}")
    out, err, seen, t = _urllib_call(body=_ok_body({"u": 2}), location=UNION)
    check("offline", "urllib: the request is a POST with the cookie", out == {"u": 2} and seen[0].get_method() == "POST"
          and seen[0].get_header("Cookie", "").startswith("uev2.loc="))
    # h48 / h49 session hygiene
    s = transport(25)._session
    from http.cookiejar import Cookie
    cookie = Cookie(0, "sid", "1", None, False, "www.ubereats.com", True, False, "/", True, True, None, False, None, None, {})
    check("offline", "h48 the session's cookie policy rejects every Set-Cookie", isinstance(s.cookies.get_policy(), uehttp._RejectAll)
          and s.cookies.get_policy().set_ok(cookie, None) is False)
    check("offline", "h49 the https adapter has max_retries.total == 0 (no hidden replays)", s.get_adapter("https://www.ubereats.com/").max_retries.total == 0)
    # tokens and cookies (l01–l03, l18/l19, l21)
    check("offline", "l01 a token with padding, and a plain word, are refused",
          raises(ValueError, from_token, UNION_TOKEN + "=") and raises(ValueError, from_token, "Toronto"))
    base = UNION.cookie()
    empty = copy.deepcopy(base)
    empty["address"]["address1"] = ""
    empty["address"]["title"] = ""
    check("offline", "l02 a cookie with an empty address1/title is not a location", raises(ValueError, from_token, _tok(empty)))
    strung = copy.deepcopy(base)
    strung["latitude"] = "43"
    check("offline", "l03 a string latitude is refused", raises(ValueError, from_token, _tok(strung)))
    nulled = copy.deepcopy(base)
    nulled["latitude"] = None
    check("offline", "a token with a null latitude is refused (a cookie without coordinates answers from the wrong place)",
          raises(ValueError, from_token, _tok(nulled)))
    for label, value in (("bool", True), ("nan", float("nan")), ("inf", float("inf"))):
        d = data(DELIVERYLOC)
        d["latitude"] = value
        with call_stub(responder_with(getDeliveryLocationV1=d)):
            err = raised(locmod.resolve, transport(), PlaceCandidate("id", "uber_places", "a", "b"))
        check("offline", f"l18/19 a {label} coordinate from getDeliveryLocationV1 → PayloadError", isinstance(err, PayloadError), f"got {err!r}")
    with call_stub() as calls:
        locmod.search(transport(), "  Union   Station  ")
    check("offline", "l21 the query text is stripped before it is sent", calls[0][1] == {"query": "Union   Station"}, f"got {calls[0][1]}")
    seven = data(MAPS) * 2
    with call_stub(responder_with(mapsSearchV1=seven)):
        cands = locmod.search(transport(), "x")
    check("offline", "l15 search() caps the candidate list at 5", len(cands) == 5, f"got {len(cands)}")
    # cache (l05, l07, l08, l11)
    d1 = tempfile.mkdtemp(prefix="cache-a-", dir=_SCRATCH)
    c = LocationCache(d1)
    c.put("fresh", UNION)
    path = os.path.join(d1, "locations.json")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["locations"]["future"] = {"saved_at": time.time() + 86400, "label": "f", "location": UNION.cookie()}
    doc["locations"]["stale"] = {"saved_at": time.time() - 40 * 86400, "label": "s", "location": UNION.cookie()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    check("offline", "l05 an entry saved in the future is treated as stale", LocationCache(d1).get("future") is None)
    LocationCache(d1).put("newer", UNION)
    with open(path, encoding="utf-8") as fh:
        keys = set(json.load(fh)["locations"])
    check("offline", "l07 a put prunes stale entries from the file (and keeps fresh ones)", keys == {"fresh", "newer"}, f"got {keys}")
    replaced: list = []
    original_replace = locmod.os.replace

    def spy(src, dst):
        replaced.append((src, dst))
        return original_replace(src, dst)
    locmod.os.replace = spy
    try:
        LocationCache(d1).put("atomic", UNION)
    finally:
        locmod.os.replace = original_replace
    check("offline", "l08 the write is atomic: a temp file in the same directory is os.replace'd onto locations.json",
          len(replaced) == 1 and replaced[0][1] == path and replaced[0][0] != path and os.path.dirname(replaced[0][0]) == d1, f"got {replaced}")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 2, "locations": {"fresh": {"saved_at": time.time(), "location": UNION.cookie()}}}, fh)
    check("offline", "l11 a cache file with schema 2 is a miss, not a crash", LocationCache(d1).get("fresh") is None)
    # ids (i04, i06, variant)
    check("offline", "i04 a /store/ URL on notubereats.com is not an id", ids.store_uuid("https://notubereats.com/ca/store/x/" + TIM_URL_ID) is None)
    check("offline", "a 22-char id with non-zero trailing bits (same 16 bytes, last char 'R' for 'Q') does not round-trip and is not an id",
          ids.store_uuid(TIM_URL_ID[:-1] + "R") is None and ids.store_uuid(TIM_URL_ID) == TIM_UUID)
    check("offline", "i09 a 23-char word is not an id", ids.store_uuid(TIM_URL_ID + "A") is None)
    check("offline", "i06 surrounding whitespace is stripped", ids.store_uuid(f"  {TIM_UUID}  ") == TIM_UUID and ids.store_uuid(f" {TIM_URL_ID} ") == TIM_UUID)
    import base64 as _b64
    bad_variant = _b64.urlsafe_b64encode(bytes.fromhex("80a1f654f86940b2" + "00" + "bcf15ba251c4ad")).decode().rstrip("=")
    check("offline", "a 22-char id whose UUID variant is not RFC 4122 is not an id",
          len(bad_variant) == 22 and ids.store_uuid(bad_variant) is None, f"got {ids.store_uuid(bad_variant)}")
    # curl output that starts with a CONNECT block
    connect = b"HTTP/1.1 200 Connection established\r\n\r\n" + _curl_stdout(200, _ok_body({"via": "proxy"}))
    out, err, t, argvs, fetched, _ = _curl_call([(0, connect, b"")])
    check("offline", "curl -i output led by a proxy CONNECT block still parses the real response", out == {"via": "proxy"}, f"got {out}, {err!r}")


# ---------------------------------------------------------------------------
# [offline] §2.3 feed parsing
# ---------------------------------------------------------------------------


def test_feed() -> None:
    print("\nfeed parsing: dedupe, rating, ETA, distance, deals, meta")
    raw = data(FEED)
    refs = walk_feed_stores(raw)
    distinct = list(dict.fromkeys(s["storeUuid"] for s in refs))
    check("offline", f"the fixture holds {FEED_REFS} store refs and {FEED_DISTINCT} distinct storeUuids (independent walk)",
          len(refs) == FEED_REFS and len(distinct) == FEED_DISTINCT, f"got {len(refs)}, {len(distinct)}")
    rows, meta = parse_feed.stores(raw, UNION)
    by_uuid = {r.uuid: r for r in rows}
    check("offline", f"stores(): exactly {FEED_DISTINCT} rows, one per storeUuid, in first-seen feed order",
          len(rows) == FEED_DISTINCT and [r.uuid for r in rows] == distinct and len(by_uuid) == FEED_DISTINCT,
          f"got {len(rows)} rows")
    check("offline", "Tim Hortons appears as a REGULAR_STORE and in a carousel, and once in the rows",
          sum(1 for s in refs if s["storeUuid"] == TIM_UUID) == 2 and sum(1 for r in rows if r.uuid == TIM_UUID) == 1)
    check("offline", "FeedMeta: CAD, in service area, stores_returned 57, offset 112, hasMore True",
          meta.currency == "CAD" and meta.in_service_area is True and meta.stores_returned == FEED_DISTINCT
          and meta.offset == 112 and meta.has_more is True, f"got {meta}")
    check("offline", "storesMap is empty in the capture and that is not an error", raw["storesMap"] == {})
    check("offline", "the first five rows are Himalayan, Albert's, Koh Lipe, Nganda, Blondies (feed order kept)",
          [r.name for r in rows[:5]] == ["Himalayan Kitchen & Bar", "Albert's Real Jamaican Foods", "Koh Lipe",
                                         "Nganda African Street Food", "Blondies Pizza"], f"got {[r.name for r in rows[:5]]}")

    tim = by_uuid[TIM_UUID]
    check("offline", "Tim Hortons: rating 4.6 from rating.text, count '700+' from the accessibility text",
          tim.rating == 4.6 and tim.rating_count_text == "700+", f"got {tim.rating}, {tim.rating_count_text}")
    check("offline", "Tim Hortons: 'Delivered in 10 to 20 min' → eta 10/20; url_id from actionUrl; coordinates from mapMarker",
          (tim.eta_min, tim.eta_max) == (10, 20) and tim.url_id == TIM_URL_ID and (tim.latitude, tim.longitude) == (43.6461, -79.3827),
          f"got {tim}")
    hima = by_uuid[HIMALAYAN_UUID]
    check("offline", "Himalayan is shown as 'New': rating None, count None, row kept, ETA 52/73",
          hima.rating is None and hima.rating_count_text is None and (hima.eta_min, hima.eta_max) == (52, 73), f"got {hima}")
    check("offline", "Himalayan's map marker says 'New' — that is not a deal (deals == ())",
          hima.deals == (), f"got {[d.to_dict() for d in hima.deals]}")
    walmart = next(r for r in rows if r.name == "Walmart")
    check("offline", "a FEATURED_STORES row (Walmart: plain-string title, no rating, no mapMarker, ETA badge '3:00PM') parses: rating None, ETA 70/107 from its tracking range, distance None, 'New' not a deal, kept",
          walmart.rating is None and (walmart.eta_min, walmart.eta_max) == (70, 107) and walmart.distance_km is None and walmart.deals == ()
          and walmart.url_id == "QkVuyt1BVKa8xeoDl2ZVnw", f"got {walmart}")
    noeta = data(FEED)
    for s in walk_feed_stores(noeta):
        if s["storeUuid"] == TIM_UUID:
            s["meta"] = [m for m in s["meta"] if m.get("badgeType") != "ETD"]
            s.pop("tracking", None)
    tim_noeta = next(r for r in parse_feed.stores(noeta, UNION)[0] if r.uuid == TIM_UUID)
    check("offline", "a store with no ETD badge at all: eta_min/eta_max None, row kept",
          tim_noeta.eta_min is None and tim_noeta.eta_max is None and tim_noeta.rating == 4.6, f"got {tim_noeta}")
    basket = next(r for r in rows if r.name == "Little Basket")
    check("offline", "a scheduled store whose badge reads '2:15PM' falls back to the tracking range 25/47",
          (basket.eta_min, basket.eta_max) == (25, 47), f"got {basket.eta_min}, {basket.eta_max}")
    tikka = by_uuid[TIKKA_UUID]
    check("offline", "Tikka: signpost 'Buy 1, get 1' and the identical map-marker text → ONE bogo deal",
          len(tikka.deals) == 1 and tikka.deals[0].type == "bogo" and tikka.deals[0].text == "Buy 1, get 1",
          f"got {[d.to_dict() for d in tikka.deals]}")
    alberts = by_uuid[ALBERTS_UUID]
    check("offline", "Albert's: '25% off select items' → percent 25, select_items; 'Only on Uber Eats' → exclusive",
          len(alberts.deals) == 1 and alberts.deals[0].to_dict() == {"text": "25% off select items", "type": "percent", "percent": 25,
                                                                      "amount": None, "min_spend": None, "select_items": True,
                                                                      "raw_type": None}
          and alberts.exclusive is True, f"got {[d.to_dict() for d in alberts.deals]}, exclusive {alberts.exclusive}")
    gift = next(r for r in rows if r.name == "Gift Baskets And Flowers")
    check("offline", "the one FARE badge in the feed → delivery_fee_text '$0 Delivery Fee'; everyone else None",
          gift.delivery_fee_text == "$0 Delivery Fee" and sum(1 for r in rows if r.delivery_fee_text) == 1,
          f"got {gift.delivery_fee_text}, {sum(1 for r in rows if r.delivery_fee_text)} with a fee text")
    caesars = next(r for r in rows if r.name == "Little Caesars")
    check("offline", "a signpost with a leading space (' Buy 1, get 1') is stripped and deduped against the marker",
          [d.text for d in caesars.deals] == ["Buy 1, get 1"], f"got {[d.text for d in caesars.deals]}")
    signposts = [(s["storeUuid"], sp["text"].strip()) for s in refs for sp in s.get("signposts") or [] if sp["text"].strip() != "New"]
    check("offline", f"deal texts are never dropped: all {len(signposts)} signpost texts (bar 'New') are on their rows",
          len(signposts) >= 40 and all(any(d.text == t for d in by_uuid[u].deals) for u, t in signposts),
          f"missing {[(u, t) for u, t in signposts if not any(d.text == t for d in by_uuid[u].deals)][:5]}")
    eleven = next((r for r in rows if any(d.text == "$11 off $40+" for d in r.deals)), None)
    check("offline", "a map marker that truncates its signpost ('$11 off' beside '$11 off $40+') is the same deal, not a second one",
          eleven is not None and [d.text for d in eleven.deals] == ["$11 off $40+"], f"got {eleven and [d.text for d in eleven.deals]}")
    for name, expect_km in (("Himalayan Kitchen & Bar", 4.617), ("Albert's Real Jamaican Foods", 5.285)):
        row = next(r for r in rows if r.name == name)
        km = parse_feed.haversine_km(UNION.latitude, UNION.longitude, row.latitude, row.longitude)
        check("offline", f"haversine to {name} is {expect_km} km ±0.01 (Uber's own badge says {round(expect_km, 1)})",
              abs(km - expect_km) < 0.01 and row.distance_km == round(expect_km, 1), f"got {km}, row {row.distance_km}")
    check("offline", "Tim Hortons is 0.2 km from Union Station (rounded to 0.1)", tim.distance_km == 0.2, f"got {tim.distance_km}")
    rows_nowhere, _ = parse_feed.stores(data(FEED), None)
    check("offline", "without an origin every distance_km is None and the rows are otherwise identical",
          len(rows_nowhere) == FEED_DISTINCT and all(r.distance_km is None for r in rows_nowhere)
          and [r.uuid for r in rows_nowhere] == distinct)
    check("offline", "the earlier capture: 63 distinct of 69 refs — membership and order differ from the later one",
          (lambda r2: len(r2) == FEED_EARLIER_DISTINCT and [r.uuid for r in r2[:6]] != [r.uuid for r in rows[:6]]
           and {r.uuid for r in r2} != {r.uuid for r in rows})(parse_feed.stores(data(FEED_EARLIER), UNION)[0]))
    page2, meta2 = parse_feed.stores(data(FEED_PAGE2), UNION)
    check("offline", "page 2 (pageInfo) parses 8 stores, offset 192, hasMore True, in_service_area defaults True",
          len(page2) == 8 and meta2.offset == 192 and meta2.has_more is True and meta2.in_service_area is True, f"got {meta2}")
    bad = data(FEED)
    del bad["feedItems"]
    check("offline", "feedItems missing → PayloadError, not an empty list", raises(PayloadError, parse_feed.stores, bad, UNION))
    empty = data(FEED)
    empty["feedItems"] = []
    check("offline", "an empty feed parses to 0 rows (the CLI turns that into exit 1)",
          parse_feed.stores(empty, UNION)[0] == [])
    out = synthetic_out_of_area()
    _, m = parse_feed.stores(out, UNION)
    check("offline", "isInServiceArea: false is carried on FeedMeta", m.in_service_area is False)
    with call_stub(responder_with(getFeedV1=out)):
        err = raised(client().feed, UNION)
    check("offline", "Client.feed raises OutsideServiceArea (a QueryError, exit 2) naming the address",
          isinstance(err, client_mod.OutsideServiceArea) and isinstance(err, QueryError) and UNION.line1 in str(err),
          f"got {err!r}")


# ---------------------------------------------------------------------------
# [offline] §2.4 deal grammar
# ---------------------------------------------------------------------------

DEAL_TABLE = [
    # text, type, percent, amount, min_spend, select_items
    ("Buy 1, get 1", "bogo", None, None, None, False),
    (" Buy 1, get 1", "bogo", None, None, None, False),
    ("Buy 1, get 1 free", "bogo", None, None, None, False),
    ("20% off", "percent", 20, None, None, False),
    ("20% off select items", "percent", 20, None, None, True),
    ("20% off $70+", "percent", 20, None, "70.00", False),
    ("$5 off", "dollar", None, "5.00", None, False),
    ("$5 off $20+", "dollar", None, "5.00", "20.00", False),
    ("$0 Delivery Fee", "free_delivery", None, None, None, False),
    ("$0 Delivery Fee on $15+", "free_delivery", None, None, "15.00", False),
    ("25% off select items", "percent", 25, None, None, True),
    ("10% off $120+", "percent", 10, None, "120.00", False),
    ("$11 off $40+", "dollar", None, "11.00", "40.00", False),
]


def test_deal_grammar() -> None:
    print("\nthe deal grammar (01 §4.2), every string")
    for text, kind, percent, amount, min_spend, select in DEAL_TABLE:
        d = parse_feed.parse_deal(text)
        want = {"text": text.strip(), "type": kind, "percent": percent, "amount": amount, "min_spend": min_spend,
                "select_items": select, "raw_type": None}
        check("offline", f"{text!r} → {kind}" + (f" {percent}%" if percent else "") + (f" min {min_spend}" if min_spend else ""),
              d.to_dict() == want, f"got {d.to_dict()}")
    for text in ("Free item on $30+", "Items on sale", "100+ items on sale", "Buy 1, get a free item", "Spend $30, save $5"):
        d = parse_feed.parse_deal(text)
        check("offline", f"unknown text {text!r} → type other, verbatim, not dropped",
              d.type == "other" and d.text == text and d.percent is None and d.amount is None, f"got {d.to_dict()}")
    check("offline", "raw_type is carried when given", parse_feed.parse_deal("Buy 1, get 1", "BOGO").raw_type == "BOGO")
    check("offline", "$ amounts with cents parse ('$2.50 off $10+')",
          parse_feed.parse_deal("$2.50 off $10+").to_dict() == {"text": "$2.50 off $10+", "type": "dollar", "percent": None,
                                                                "amount": "2.50", "min_spend": "10.00", "select_items": False,
                                                                "raw_type": None})
    # The whole fixture, by grammar: every string in the feed classifies as its family
    families = {"bogo": 0, "percent": 0, "dollar": 0, "free_delivery": 0, "other": 0}
    for s in walk_feed_stores(data(FEED)):
        texts = [sp["text"] for sp in s.get("signposts") or []]
        mm = ((s.get("mapMarker") or {}).get("secondaryMarkerContent") or {}).get("text")
        for t in texts + ([mm] if mm else []):
            families[parse_feed.parse_deal(t).type] += 1
    check("offline", "the fixture's deal strings cover every family with ≥ 5 (bogo, percent, dollar, free_delivery, other)",
          all(families[k] >= 5 for k in families), f"got {families}")


# ---------------------------------------------------------------------------
# [offline] §2.5 menu parsing — the silent-failure core
# ---------------------------------------------------------------------------


def test_menu_parsing() -> None:
    print("\nmenu parsing: duplicates, sale vs was, rounding, deal markers, notes, hours")
    raw = data(HIMALAYAN)
    entries = catalog_entries(raw)
    check("offline", "Himalayan capture: 109 catalog entries, 98 distinct uuids (independent walk)",
          len(entries) == 109 and len({e["uuid"] for e in entries}) == 98, f"got {len(entries)}, {len({e['uuid'] for e in entries})}")
    s = parse_store.store(raw)
    check("offline", "store(): 98 dishes, entries_total 109, duplicates_removed 11",
          len(s.dishes) == 98 and s.entries_total == 109 and s.duplicates_removed == 11 and len({d.uuid for d in s.dishes}) == 98,
          f"got {len(s.dishes)}, {s.entries_total}, {s.duplicates_removed}")
    chili = next(d for d in s.dishes if d.uuid == CHILI_UUID)
    check("offline", "a dish first listed under 'Featured items' takes its first real section ('Chilis')",
          chili.section == "Chilis" and chili.title == "Honey Chicken Chili" and chili.price.cents == 1919
          and chili.price.amount == "19.19" and chili.has_options is True and chili.was is None and chili.deal is None,
          f"got {chili}")
    check("offline", "no dish keeps 'Featured items' as its section",
          not any((d.section or "").lower() == "featured items" for d in s.dishes) and len(s.dishes) == 98)
    check("offline", "section_uuid/subsection_uuid are carried (needed for getMenuItemV1)",
          chili.section_uuid == "a07329f7-ea62-465a-a415-f74373fb2502" and chili.subsection_uuid is not None)
    samosa = next(d for d in s.dishes if d.uuid == SAMOSA_UUID)
    check("offline", "Samosa Chaat: 'Earn $7 Uber Cash for photo' is a note, NOT a deal and NOT a was price",
          samosa.note == "Earn $7 Uber Cash for photo" and samosa.deal is None and samosa.was is None
          and samosa.price.amount == "13.19", f"got {samosa}")
    check("offline", "store.deals is empty for Himalayan (no dish carries a deal); has_store_promotion False",
          s.deals == () and s.has_store_promotion is False)
    check("offline", "header fields: title with branch, address, phone, cuisines, currency, coordinates",
          s.title == "Himalayan Kitchen & Bar ( 1439 Queen Street West )" and s.address == "1439 Queen Street West, Toronto, NAMER M6R 1A1"
          and s.phone == "+16476083853" and s.cuisines == ("Indian", "Vegetarian", "Asian") and s.currency == "CAD"
          and s.latitude == 43.6402 and s.longitude == -79.43756 and s.slug == "himalayan-kitchen-&-bar-1439-queen-street-west",
          f"got {s.title!r}, {s.address!r}, {s.cuisines}")
    check("offline", "located: eta '52–73 Min', pickup eta '18 min', distance 4.6 km, within range, rating None (no rating block)",
          s.eta_text == "52–73 Min" and s.pickup_eta_text == "18 min" and s.distance_km == 4.6 and s.within_range is True
          and s.rating is None and s.rating_count_text is None, f"got {s.eta_text}, {s.pickup_eta_text}, {s.distance_km}, {s.within_range}")
    s_nowhere = parse_store.store(data(HIMALAYAN), located=False)
    check("offline", "not located: eta, pickup_eta, distance_km and within_range are None; the menu is the same",
          s_nowhere.eta_text is None and s_nowhere.pickup_eta_text is None and s_nowhere.distance_km is None
          and s_nowhere.within_range is None and len(s_nowhere.dishes) == 98)
    today = client_mod.hours_today(s, PROBE_AT)
    check("offline", "hours: 'Sunday' 720–1410 → 12:00–23:30 in the JSON; Sunday 2026-09-13 14:02 picks that span; 'Monday - Thursday' covers Wednesday",
          s.hours["Sunday"][0].to_dict() == {"start": "12:00", "end": "23:30"} and today is not None and len(today) == 1
          and today[0].to_dict() == {"start": "12:00", "end": "23:30"}
          and [x.to_dict() for x in client_mod.hours_today(s, datetime(2026, 9, 16, 12, 0).astimezone())] == [{"start": "12:00", "end": "22:00"}],
          f"got {s.hours.get('Sunday')}, today {today}")
    check("offline", "is_open / is_orderable True, closed_message None on the open capture",
          s.is_open is True and s.is_orderable is True and s.closed_message is None)

    # Albert's: the sale-price traps
    a = parse_store.store(data(ALBERTS))
    jerk = next(d for d in a.dishes if d.uuid == JERK_UUID)
    check("offline", "Albert's: 94 entries → 71 dishes, 23 duplicates",
          len(a.dishes) == 71 and a.entries_total == 94 and a.duplicates_removed == 23, f"got {len(a.dishes)}, {a.duplicates_removed}")
    check("offline", "Jerk Chicken Special: price.cents 1087.5 (raw), amount '10.88' (the SALE price), was '14.50' (from the line-through span)",
          jerk.price.cents == 1087.5 and jerk.price.amount == "10.88" and jerk.was is not None and jerk.was.amount == "14.50"
          and jerk.was.cents == 1450 and jerk.price_unclear is False, f"got {jerk.price.to_dict()}, was {jerk.was and jerk.was.to_dict()}")
    check("offline", "…and its deal is '25% off' (percent 25); its section is the real one ('Lunch Specials'), not 'Featured items' nor the promo block 'Save on Select Items'",
          jerk.deal is not None and jerk.deal.type == "percent" and jerk.deal.percent == 25 and jerk.deal.text == "25% off"
          and jerk.section == "Lunch Specials", f"got {jerk.deal and jerk.deal.to_dict()}, {jerk.section}")
    check("offline", "was is the struck figure, never the first figure: was > price on every discounted dish (7 of them)",
          sum(1 for d in a.dishes if d.was) == 7 and all(d.was.cents > d.price.cents for d in a.dishes if d.was)
          and {d.was.amount for d in a.dishes if d.was} == {"14.50", "13.50", "15.75"},
          f"got {[(d.title, d.price.amount, d.was and d.was.amount) for d in a.dishes if d.was]}")
    check("offline", "every discounted dish carries the 25% deal and every 25% dish carries a was — the two markers agree (7 = 7)",
          sum(1 for d in a.dishes if d.deal) == 7 and all(bool(d.deal) == bool(d.was) for d in a.dishes) and len(a.dishes) == 71)
    check("offline", "store.deals lists the distinct dish deal once: ['25% off']; has_store_promotion True while promotion is null",
          [d.text for d in a.deals] == ["25% off"] and a.has_store_promotion is True and data(ALBERTS)["promotion"] is None)
    veggie = next(d for d in a.dishes if d.title == "Veggie Special")
    boneless = next(d for d in a.dishes if d.title.startswith("Boneless Jerk"))
    check("offline", "fractional cents keep their raw value in cents: 1012.5 and 1181.25",
          veggie.price.cents == 1012.5 and boneless.price.cents == 1181.25 and boneless.price.amount == "11.81")
    check("offline", "rounding follows Uber's taglines (half-even): 1087.5 → '10.88', 1012.5 → '10.12', 1181.25 → '11.81', 1919 → '19.19', 1000.49 → '10.00'",
          Money(1087.5, "CAD").amount == "10.88" and Money(1012.5, "CAD").amount == "10.12" and Money(1181.25, "CAD").amount == "11.81"
          and Money(1919, "CAD").amount == "19.19" and Money(1000.49, "CAD").amount == "10.00" and Money(1000.5, "CAD").amount == "10.00"
          and Money(1087.5, "CAD").display() == "$10.88" and Money(2300, "GBP").display() == "£23.00",
          f"got {Money(1087.5, 'CAD').amount}, {Money(1012.5, 'CAD').amount}, {Money(1181.25, 'CAD').amount}")
    taglines = {e["uuid"]: e["priceTagline"]["text"] for e in catalog_entries(data(ALBERTS))}
    check("offline", "every one of Albert's 71 dishes displays exactly what Uber's own tagline shows (the three fractional-cent dishes included)",
          all(taglines[d.uuid].startswith("$" + d.price.amount) for d in a.dishes) and len(a.dishes) == 71,
          f"mismatch {[(d.title, d.price.amount, taglines[d.uuid]) for d in a.dishes if not taglines[d.uuid].startswith('$' + d.price.amount)][:3]}")
    check("offline", "hours with an empty day (Monday) → (), and a span past midnight (Friday - Saturday 660–30) → 11:00–00:30",
          a.hours["Monday"] == () and a.hours["Friday - Saturday"][0].to_dict() == {"start": "11:00", "end": "00:30"}
          and [x.to_dict() for x in client_mod.hours_today(a, PROBE_AT)] == [{"start": "11:00", "end": "23:30"}],
          f"got {a.hours}")
    check("offline", "rating 4.6 with count '6000+' (a string with +), eta '40–60 Min', 5.3 km",
          a.rating == 4.6 and a.rating_count_text == "6000+" and a.eta_text == "40–60 Min" and a.distance_km == 5.3)
    u = parse_store.store(synthetic_unclear_item())
    ujerk = next(d for d in u.dishes if d.uuid == JERK_UUID)
    check("offline", "synthetic: two amounts and no line-through → was None, price_unclear True, price still 1087.5",
          ujerk.was is None and ujerk.price_unclear is True and ujerk.price.cents == 1087.5, f"got {ujerk}")
    check("offline", "was_price(): struck span → (14.50, False); two plain amounts → (None, True); one amount → (None, False)",
          (lambda w: w[0] is not None and w[0].amount == "14.50" and w[1] is False)(
              parse_store.was_price('<span><span style="color:#05944F">$10.88 </span><span style="text-decoration:line-through;color:#757575">$14.50</span></span>', "CAD"))
          and parse_store.was_price("<span>$10.88 $14.50</span>", "CAD") == (None, True)
          and parse_store.was_price("<span>$19.19</span>", "CAD") == (None, False))

    # Tikka: BOGO markers, the triple-listed bowl
    t = parse_store.store(data(TIKKA))
    tentries = catalog_entries(data(TIKKA))
    check("offline", "Tikka capture: 23 entries, 13 distinct; the Butter Chicken Hefty Bowl is listed 3 times",
          len(tentries) == 23 and len({e["uuid"] for e in tentries}) == 13
          and sum(1 for e in tentries if e["uuid"] == BUTTER_BOWL_UUID) == 3)
    marked = sum(1 for e in tentries if (e.get("itemPromotion") or {}).get("type") == "buyXGetYItemPromotion"
                 or ((e.get("promoInfo") or {}).get("promoBadge") or {}).get("accessibilityText") == "Buy 1, get 1 free")
    check("offline", "6 entries carry a BOGO marker (badge on 4, itemPromotion on all 6) — two dishes",
          marked == 6, f"got {marked}")
    bowls = [d for d in t.dishes if d.uuid == BUTTER_BOWL_UUID]
    check("offline", "store(): 13 dishes, 10 duplicates removed, the bowl appears once",
          len(t.dishes) == 13 and t.duplicates_removed == 10 and len(bowls) == 1, f"got {len(t.dishes)}, {t.duplicates_removed}, {len(bowls)}")
    bowl = bowls[0]
    check("offline", "the bowl is flagged 'Buy 1, get 1 free' (bogo) at its FULL price 23.49 — the featured copy has no badge, the later copies do",
          bowl.deal is not None and bowl.deal.type == "bogo" and bowl.deal.text == "Buy 1, get 1 free"
          and bowl.price.amount == "23.49" and bowl.was is None, f"got {bowl.deal and bowl.deal.to_dict()}, {bowl.price.amount}")
    check("offline", "exactly 2 dishes carry the BOGO deal; store.deals lists it once",
          sum(1 for d in t.dishes if d.deal and d.deal.type == "bogo") == 2 and [d.text for d in t.deals] == ["Buy 1, get 1 free"],
          f"got {[(d.title, d.deal and d.deal.text) for d in t.dishes if d.deal]}")
    check("offline", "Tikka's rating count is a plain '67'", t.rating == 4.6 and t.rating_count_text == "67")

    # pickup vs delivery
    p = parse_store.store(data(HIMALAYAN_PICKUP))
    prices_d = {d.uuid: d.price.cents for d in s.dishes}
    check("offline", "pickup capture: 98 dishes with prices equal to delivery for every uuid; eta '18–28 Min'",
          len(p.dishes) == 98 and all(prices_d.get(d.uuid) == d.price.cents for d in p.dishes) and p.eta_text == "18–28 Min",
          f"got {len(p.dishes)}, {p.eta_text}")

    # closed store
    c = parse_store.store(synthetic_closed_store())
    check("offline", "synthetic closed store: is_open False, is_orderable False, closed_message, and next_opening 16:30 at 14:02",
          c.is_open is False and c.is_orderable is False and c.closed_message == "Currently closed"
          and client_mod.next_opening(client_mod.hours_today(c, PROBE_AT), PROBE_AT) == "16:30", f"got {c.is_open}, {c.closed_message}")

    # drift
    bad = data(HIMALAYAN)
    del bad["catalogSectionsMap"]
    check("offline", "missing catalogSectionsMap → PayloadError (exit 3), never an empty menu",
          raises(PayloadError, parse_store.store, bad))
    empty = data(HIMALAYAN)
    empty["catalogSectionsMap"] = {}
    e = parse_store.store(empty)
    check("offline", "an empty catalogSectionsMap → 0 dishes, no crash", e.dishes == () and e.entries_total == 0)
    empty["catalogSectionsMap"] = {"m": []}
    check("offline", "an empty block list → 0 dishes, no crash", parse_store.store(empty).dishes == ())
    bad = data(HIMALAYAN)
    del bad["title"]
    check("offline", "missing title → PayloadError", raises(PayloadError, parse_store.store, bad))
    check("offline", "the whole {status, data} envelope is accepted too", len(parse_store.store(envelope(HIMALAYAN)).dishes) == 98)


# ---------------------------------------------------------------------------
# [offline] §2.6 item options
# ---------------------------------------------------------------------------


def _pizza_dish() -> Dish:
    b = parse_store.store(data(BLONDIES))
    return next(d for d in b.dishes if d.uuid == PIZZA_UUID)


def test_item_options() -> None:
    print("\nitem options: groups, add-on prices, from_price, nesting")
    b = parse_store.store(data(BLONDIES))
    check("offline", "Blondies: 55 entries → 34 dishes; title 'Blondies Pizza (Bay)'; hours 'Every Day' 12:00–20:00",
          len(b.dishes) == 34 and b.entries_total == 55 and b.title == "Blondies Pizza (Bay)"
          and b.hours["Every Day"][0].to_dict() == {"start": "12:00", "end": "20:00"}
          and [x.to_dict() for x in client_mod.hours_today(b, PROBE_AT)] == [{"start": "12:00", "end": "20:00"}],
          f"got {len(b.dishes)}, {b.title!r}, {b.hours}")
    pizza = _pizza_dish()
    it = parse_item.item(data(ITEM_PIZZA), pizza, "CAD")
    groups = it.groups
    want = [("Choose Base Sauce", 1, 1, 2), ("Add Extra Meat Toppings", 0, 7, 6), ("Add Extra Cheese Toppings", 0, 6, 5),
            ("Add Extra Vegetable Toppings", 0, 12, 11), ("Add Finishing Touches", 0, 8, 7), ("Add Dipping Sauces", 0, 4, 4),
            ("Add Salads", 0, 2, 2), ("Add Drinks", 0, 8, 8)]
    got = [(g.title, g.min_permitted, g.max_permitted, len(g.options)) for g in groups]
    check("offline", "the 16\" pizza has exactly 8 groups with these titles, min–max and option counts", got == want, f"got {got}")
    sauce = groups[0]
    check("offline", "'Choose Base Sauce' is required (min 1), both options free",
          sauce.required is True and all(o.price.cents == 0 for o in sauce.options) and len(sauce.options) == 2
          and sum(1 for g in groups if g.required) == 1 and it.required_groups == 1)
    bacon = next(o for g in groups for o in g.options if o.title == "Bacon")
    check("offline", "Bacon is +5.75 (575 cents), not sold out, max 99", bacon.price.amount == "5.75" and bacon.price.cents == 575
          and bacon.sold_out is False and bacon.max_qty == 99, f"got {bacon}")
    check("offline", "from_price is 23.00: base 2300 + the free required sauce; price/uuid/title from the item",
          it.from_price.cents == 2300 and it.from_price.amount == "23.00" and it.price.cents == 2300 and it.uuid == PIZZA_UUID
          and it.title == 'Blondies Custom Pizza - 16" Large' and it.was is None and it.deal is None and it.sold_out is False,
          f"got {it.from_price.to_dict()}")
    j = it.to_dict()
    check("offline", "to_dict: groups[] carry uuid/title/required/min/max/options[]; options carry price/sold_out/min/max/default/groups",
          set(j["groups"][0]) == {"uuid", "title", "required", "min", "max", "options"}
          and set(j["groups"][0]["options"][0]) == {"uuid", "title", "price", "sold_out", "min", "max", "default", "groups"})
    chili_dish = next(d for d in parse_store.store(data(HIMALAYAN)).dishes if d.uuid == CHILI_UUID)
    ci = parse_item.item(data(ITEM_CHILI), chili_dish, "CAD")
    check("offline", "Honey Chicken Chili: one group 'Choice of spice' 0–1, 3 free options, from_price 19.19, 0 required",
          [(g.title, g.min_permitted, g.max_permitted, len(g.options)) for g in ci.groups] == [("Choice of spice", 0, 1, 3)]
          and ci.from_price.amount == "19.19" and ci.required_groups == 0 and all(o.price.cents == 0 for o in ci.groups[0].options),
          f"got {[(g.title, g.min_permitted, g.max_permitted) for g in ci.groups]}, {ci.from_price.amount}")
    syn = parse_item.item(synthetic_required_paid_group(), pizza, "CAD")
    check("offline", "synthetic: required group min 2 → the two cheapest in-stock options (+1.50, +3.00): from_price 23.00 + 4.50 = 27.50; the sold-out +0.50 is skipped",
          syn.from_price.cents == 2750 and syn.from_price.amount == "27.50" and syn.required_groups == 1, f"got {syn.from_price.to_dict()}")
    nested = syn.groups[0].options[1].groups
    check("offline", "synthetic: the nested childCustomizationList is parsed and present in the JSON",
          len(nested) == 1 and nested[0].title == "Crust" and nested[0].options[0].title == "Thin"
          and syn.to_dict()["groups"][0]["options"][1]["groups"][0]["title"] == "Crust", f"got {nested}")
    check("offline", "from_price() recurses into the chosen option's own required groups",
          from_price(Money(1000, "CAD"), (OptionGroup("g", "G", 1, 1, (
              Option("o", "O", Money(100, "CAD"), False, 0, 1, 0, groups=(OptionGroup("n", "N", 1, 1, (
                  Option("x", "X", Money(50, "CAD"), False, 0, 1, 0),)),)),)),)).cents == 1150)
    check("offline", "from_price(): min 2 with one in-stock option picks it once (never invents a second)",
          from_price(Money(0, "CAD"), (OptionGroup("g", "G", 2, 2, (Option("o", "O", Money(100, "CAD"), False, 0, 2, 0),
                                                                     Option("s", "S", Money(1, "CAD"), True, 0, 2, 0))),)).cents == 100)
    # was/deal fall back to the Dish when the item omits them — only at the same price
    jerk = next(d for d in parse_store.store(data(ALBERTS)).dishes if d.uuid == JERK_UUID)
    d = data(ITEM_CHILI)
    d["price"] = 1087.5
    carried = parse_item.item(d, jerk, "CAD")
    d["price"] = 1919
    dropped = parse_item.item(d, jerk, "CAD")
    check("offline", "an item answer without a tagline inherits the dish's was/deal at the same price, and not at a different price",
          carried.was is not None and carried.was.amount == "14.50" and carried.deal is not None and carried.deal.text == "25% off"
          and dropped.was is None, f"got {carried.was}, {dropped.was}")
    bad = data(ITEM_PIZZA)
    bad["customizationsList"] = "nope"
    check("offline", "a customizationsList that is not a list → PayloadError", raises(PayloadError, parse_item.item, bad, pizza, "CAD"))


# ---------------------------------------------------------------------------
# [offline] §2.7 commands through main() — clock pinned, transport stubbed
# ---------------------------------------------------------------------------

AT = ["--at", UNION_TOKEN]


def _only_error_line(err: str) -> bool:
    """One `error: …` line — or argparse's own usage block ending in its error line."""
    lines = [ln for ln in err.splitlines() if ln.strip()]
    if "Traceback" in err or not lines:
        return False
    if len(lines) == 1:
        return lines[0].startswith("error: ")
    return lines[0].startswith("usage:") and ": error: " in lines[-1]


def test_ids() -> None:
    print("\nstore ids: UUID, URL, URL id (G6)")
    check("offline", "url_id(uuid) is the 22-char base64url id from the feed's actionUrl (Tim Hortons, Blondies, Himalayan)",
          ids.url_id(TIM_UUID) == TIM_URL_ID and ids.url_id(BLONDIES_UUID) == BLONDIES_URL_ID and ids.url_id(HIMALAYAN_UUID) == HIMALAYAN_URL_ID)
    check("offline", "store_uuid: the bare id, the store URL with a query, the %26 slug, and an upper-case UUID all → the UUID",
          ids.store_uuid(TIM_URL_ID) == TIM_UUID
          and ids.store_uuid("https://www.ubereats.com/ca/store/tim-hortons-55-york-street/gKH2VPhpQLKAvPFbolHErQ?diningMode=DELIVERY") == TIM_UUID
          and ids.store_uuid("https://www.ubereats.com/ca/store/himalayan-kitchen-%26-bar-1439-queen-street-west/KdoG1dSfVwiSAgRhSCirYA") == HIMALAYAN_UUID
          and ids.store_uuid(TIM_UUID.upper()) == TIM_UUID and ids.store_uuid("/store/x/" + BLONDIES_URL_ID) == BLONDIES_UUID)
    check("offline", "a name is not an id: None for 'Tim Hortons', 'pizza', a 22-letter word, another site's URL",
          ids.store_uuid("Tim Hortons") is None and ids.store_uuid("pizza") is None
          and ids.store_uuid("abcdefghijklmnopqrstuv") is None and ids.store_uuid("https://example.com/store/x/" + TIM_URL_ID) is None)
    check("offline", "every store in the feed round-trips actionUrl id ↔ storeUuid",
          (lambda rows: len(rows) == FEED_DISTINCT and all(r.url_id is None or ids.store_uuid(r.url_id) == r.uuid for r in rows)
           and sum(1 for r in rows if r.url_id) >= 50)(parse_feed.stores(data(FEED), UNION)[0]))


def test_locate_cmd() -> None:
    print("\nlocate")
    code, j, calls, err = run_json(["locate", "Union Station Toronto"])
    check("offline", "locate: exit 0, 5 candidates, a token that decodes to the resolved location, 2 requests",
          code == 0 and j and j["ok"] and len(j["candidates"]) == 5 and from_token(j["token"]) == UNION
          and j["location"]["latitude"] == 43.6452223 and j["requests_used"] == 2 and j["query"] == "Union Station Toronto"
          and [c[0] for c in calls] == ["mapsSearchV1", "getDeliveryLocationV1"], f"exit {code}: {err or str(j)[:200]}")
    code, j, calls, err = run_json(["locate", "Union Station Toronto", "--pick", "3"])
    check("offline", "--pick 3 resolves the google_places candidate",
          code == 0 and calls[1][1]["placeId"].startswith("ChIJ") and calls[1][1]["provider"] == "google_places", f"got {calls[1:]}")
    code, j, calls, err = run_json(["locate", "zzzz"], responder_with(mapsSearchV1=[]))
    check("offline", "no candidates → exit 2, kind usage, one request", code == 2 and j and j["ok"] is False and j["kind"] == "usage"
          and len(calls) == 1, f"exit {code}: {j}")
    code, j, calls, err = run_json(["locate", "Union Station Toronto", "--max-requests", "1"])
    check("offline", "--max-requests 1 refuses before any request (plan needs 2)", code == 2 and calls == [] and "--max-requests" in j["error"])
    with call_stub():
        code, out, err = run_cli(["locate", "Union Station Toronto"])
    check("offline", "human mode prints the resolved label and the token", code == 0 and "65 Front St W" in out and UNION_TOKEN in out)


def test_nearby_cmd() -> None:
    print("\nnearby")
    code, j, calls, err = run_json(["nearby", *AT])
    check("offline", "nearby --at <token>: exit 0, 25 rows (default limit) of 57, exhaustive false, 1 request, address named",
          code == 0 and j and j["ok"] and len(j["rows"]) == 25 and j["stores_returned"] == FEED_DISTINCT and j["exhaustive"] is False
          and j["filtered_out"] == 0 and j["requests_used"] == 1 and j["mode"] == "delivery"
          and j["address"]["line1"] == "Toronto Union Station Train Station" and j["address"]["latitude"] == 43.6452223
          and calls == [("getFeedV1", client_mod.FEED_BODY, UNION)], f"exit {code}: {err or str(j)[:300]}")
    check("offline", "a row carries the documented store fields",
          set(j["rows"][0]) >= {"uuid", "name", "rating", "rating_count_text", "eta_min", "eta_max", "distance_km", "deals",
                                "exclusive", "delivery_fee_text", "url_id"}, f"got {sorted(j['rows'][0])}")
    with call_stub():
        code, out, err = run_cli(["nearby", *AT])
    check("offline", "human header: '25 of the 57 stores Uber returned near <address>' — Uber's list, not the whole market",
          code == 0 and f"25 of the {FEED_DISTINCT} stores Uber returned near Toronto Union Station Train Station" in out
          and "not the whole market" in out, f"got {out[:200]!r}")
    code, j, *_ = run_json(["nearby", *AT, "--deals", "--limit", "100"])
    check("offline", "--deals keeps only rows with a deal, and filtered_out + kept == 57",
          code == 0 and len(j["rows"]) >= 40 and all(r["deals"] for r in j["rows"]) and j["filtered_out"] + len(j["rows"]) == FEED_DISTINCT,
          f"got {len(j['rows'])}, filtered {j['filtered_out']}")
    code, j, *_ = run_json(["nearby", *AT, "--deal-type", "bogo", "--limit", "100"])
    check("offline", "--deal-type bogo: exactly 19 rows, every one with a bogo deal",
          code == 0 and len(j["rows"]) == 19 and all(any(d["type"] == "bogo" for d in r["deals"]) for r in j["rows"]), f"got {len(j['rows'])}")
    code, j, *_ = run_json(["nearby", *AT, "--min-rating", "4.5", "--limit", "100"])
    check("offline", "--min-rating 4.5 is inclusive: 21 rows, 6 rated exactly 4.5, none below, none unrated",
          code == 0 and len(j["rows"]) == 21 and sum(1 for r in j["rows"] if r["rating"] == 4.5) == 6
          and all(r["rating"] is not None and r["rating"] >= 4.5 for r in j["rows"]), f"got {len(j['rows'])}")
    code, j, *_ = run_json(["nearby", *AT, "--max-eta", "30", "--limit", "100"])
    check("offline", "--max-eta 30 is inclusive on eta_max: 6 rows, one at exactly 30, none above, none without an ETA",
          code == 0 and len(j["rows"]) == 6 and any(r["eta_max"] == 30 for r in j["rows"])
          and all(r["eta_max"] is not None and r["eta_max"] <= 30 for r in j["rows"]), f"got {len(j['rows'])}")
    code, j, *_ = run_json(["nearby", *AT, "--max-km", "1", "--limit", "100"])
    check("offline", "--max-km 1: 13 rows, all within 1.0 km", code == 0 and len(j["rows"]) == 13
          and all(r["distance_km"] is not None and r["distance_km"] <= 1.0 for r in j["rows"]), f"got {len(j['rows'])}")
    code, j, *_ = run_json(["nearby", *AT, "--name", "pizza", "--limit", "100"])
    check("offline", "--name pizza: 6 rows, folded substring match", code == 0 and len(j["rows"]) == 6
          and all("pizza" in fold(r["name"]) for r in j["rows"]), f"got {[r['name'] for r in j['rows']]}")
    code, j, *_ = run_json(["nearby", *AT, "--sort", "rating", "--limit", "100"])
    ratings = [r["rating"] for r in j["rows"]]
    rated = [r for r in ratings if r is not None]
    check("offline", "--sort rating: non-increasing, unrated last", code == 0 and rated == sorted(rated, reverse=True)
          and ratings[: len(rated)] == rated, f"got {ratings[:8]}…{ratings[-3:]}")
    code, j, *_ = run_json(["nearby", *AT, "--sort", "distance", "--limit", "100"])
    kms = [r["distance_km"] for r in j["rows"] if r["distance_km"] is not None]
    check("offline", "--sort distance: non-decreasing, Pizza Pizza (0.1 km) first", kms == sorted(kms) and j["rows"][0]["name"] == "Pizza Pizza")
    code, j, *_ = run_json(["nearby", *AT, "--sort", "eta", "--limit", "100"])
    etas = [r["eta_max"] for r in j["rows"] if r["eta_max"] is not None]
    check("offline", "--sort eta: non-decreasing on eta_max", etas == sorted(etas))
    code, j, *_ = run_json(["nearby", *AT, "--limit", "3"])
    with call_stub():
        _, out, _ = run_cli(["nearby", *AT, "--limit", "3"])
    check("offline", "--limit 3: 3 rows, header says '3 of the 57'", code == 0 and len(j["rows"]) == 3 and f"3 of the {FEED_DISTINCT}" in out)
    code, j, calls, err = run_json(["nearby", *AT, "--min-rating", "5", "--max-eta", "5"])
    check("offline", "filters that exclude everything → exit 1 with an ok object and rows []",
          code == 1 and j and j["ok"] is True and j["rows"] == [] and j["filtered_out"] == FEED_DISTINCT, f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["nearby", *AT, "--pages", "2", "--limit", "100"])
    check("offline", "--pages 2: two getFeedV1 calls, the second with pageInfo {offset 112, pageSize 80}; 65 stores; 2 requests",
          code == 0 and len(calls) == 2 and calls[1][1].get("pageInfo") == {"offset": 112, "pageSize": 80}
          and "pageInfo" not in calls[0][1] and j["stores_returned"] == FEED_DISTINCT + 8 and j["requests_used"] == 2,
          f"exit {code}: {len(calls)} calls, {j and j.get('stores_returned')}")
    code, j, calls, err = run_json(["nearby", *AT, "--pages", "2", "--max-requests", "1"])
    check("offline", "--pages 2 with --max-requests 1 is refused before any request", code == 2 and calls == [])
    code, j, calls, err = run_json(["nearby", *AT], responder_with(getFeedV1=synthetic_out_of_area()))
    check("offline", "isInServiceArea: false → exit 2, kind lookup, the error names the address",
          code == 2 and j and j["ok"] is False and j["kind"] == "lookup" and "Toronto Union Station Train Station" in j["error"]
          and "deliver" in j["error"], f"exit {code}: {j}")
    cache = tempfile.mkdtemp(prefix="addr-", dir=_SCRATCH)
    code, j, calls, err = run_json(["nearby", "--at", "Union Station Toronto"], cache=cache)
    check("offline", "--at <address text>: locate (2) + feed (1) = 3 requests on a cold cache",
          code == 0 and [c[0] for c in calls] == ["mapsSearchV1", "getDeliveryLocationV1", "getFeedV1"] and j["requests_used"] == 3,
          f"got {[c[0] for c in calls]}")
    code, j, calls, err = run_json(["nearby", "--at", "union station toronto"], cache=cache)
    check("offline", "…and the same address again (any case) costs only the feed: the location came from the cache",
          code == 0 and [c[0] for c in calls] == ["getFeedV1"] and j["requests_used"] == 1, f"got {[c[0] for c in calls]}")
    check("offline", "the cache file holds only the location (no feed, menu or item response)",
          os.listdir(cache) == ["locations.json"] and not any(k in open(os.path.join(cache, "locations.json")).read()
                                                              for k in ("feedItems", "catalogSectionsMap", "customizationsList")),
          f"files {os.listdir(cache)}")
    code, j, calls, err = run_json(["nearby", *AT], responder_with(getFeedV1={"storesMap": {}, "feedItems": [], "isInServiceArea": True}))
    check("offline", "an empty feed → exit 1, not an error", code == 1 and j["ok"] is True and j["stores_returned"] == 0)


def test_find_cmd() -> None:
    print("\nfind")
    code, j, calls, err = run_json(["find", "tim hortons", *AT])
    check("offline", "find 'tim hortons' → exit 0, one exact candidate: Tim Hortons, its UUID and url_id, match 'exact'",
          code == 0 and j and len(j["candidates"]) == 1 and j["candidates"][0]["uuid"] == TIM_UUID and j["candidates"][0]["match"] == "exact"
          and j["candidates"][0]["url_id"] == TIM_URL_ID and j["exhaustive"] is False and j["stores_returned"] == FEED_DISTINCT
          and j["query"] == "tim hortons", f"exit {code}: {err or str(j)[:300]}")
    code, j, *_ = run_json(["find", "Pizza Pizza", *AT])
    check("offline", "exact beats partial: 'Pizza Pizza' lists the exact match first, then the all-token matches",
          code == 0 and j["candidates"][0]["name"] == "Pizza Pizza" and j["candidates"][0]["match"] == "exact"
          and len(j["candidates"]) > 1 and all(c["match"] != "exact" for c in j["candidates"][1:]),
          f"got {[(c['name'], c['match']) for c in j['candidates']]}")
    code, j, *_ = run_json(["find", "HIMALAYAN KITCHEN & BAR", *AT])
    check("offline", "case and '&' are folded: 'HIMALAYAN KITCHEN & BAR' is an exact match",
          code == 0 and j["candidates"][0]["uuid"] == HIMALAYAN_UUID and j["candidates"][0]["match"] == "exact", f"got {j['candidates'][:1]}")
    code, j, *_ = run_json(["find", "Himalayán kitchen", *AT])
    check("offline", "accents are folded: 'Himalayán kitchen' matches on all tokens",
          code == 0 and j["candidates"][0]["uuid"] == HIMALAYAN_UUID and j["candidates"][0]["match"] == "all_tokens", f"got {j['candidates'][:1]}")
    code, j, *_ = run_json(["find", "Himal", *AT])
    check("offline", "a prefix matches with match 'prefix'", code == 0 and j["candidates"][0]["uuid"] == HIMALAYAN_UUID
          and j["candidates"][0]["match"] == "prefix", f"got {j['candidates'][:1]}")
    code, j, calls, err = run_json(["find", "Schwartz's Deli", *AT])
    with call_stub():
        _, out, herr = run_cli(["find", "Schwartz's Deli", *AT])
    check("offline", "no match → exit 1 (not 2, not 0) with the 'does not mean it isn't on Uber Eats' sentence, in JSON and human mode",
          code == 1 and j and j["ok"] is True and j["candidates"] == [] and client_mod.NOT_IN_FEED in j.get("message", "")
          and f"not among the {FEED_DISTINCT} stores" in j["message"] and client_mod.NOT_IN_FEED in out and herr == "",
          f"exit {code}: {str(j)[:300]}")


def test_menu_cmd() -> None:
    print("\nmenu")
    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID, *AT])
    check("offline", "menu <uuid> --at: exit 0, 98 dishes, 11 duplicates removed, one getStoreV1 (DELIVERY) with the cookie location",
          code == 0 and j and j["ok"] and j["dishes_total"] == 98 and j["duplicates_removed"] == 11 and j["dishes_sold_out"] == 0
          and calls == [("getStoreV1", {"storeUuid": HIMALAYAN_UUID, "diningMode": "DELIVERY", "time": {"asap": True},
                                        "cbType": "EATER_ENDORSED"}, UNION)] and j["requests_used"] == 1,
          f"exit {code}: {err or str(j)[:300]}")
    st = j["store"]
    check("offline", "store block: title, address, is_open, hours (Sunday 12:00–23:30), eta, pickup_eta, distance 4.6, within_range, cuisines, phone, currency",
          st["title"].startswith("Himalayan Kitchen & Bar") and st["is_open"] is True and st["hours"]["Sunday"] == [{"start": "12:00", "end": "23:30"}]
          and st["eta"] == "52–73 Min" and st["pickup_eta"] == "18 min" and st["distance_km"] == 4.6 and st["within_range"] is True
          and st["cuisines"] == ["Indian", "Vegetarian", "Asian"] and st["phone"] == "+16476083853" and st["currency"] == "CAD"
          and st["rating"] is None and j["hours_today"] == [{"start": "12:00", "end": "23:30"}], f"got {st}")
    sections = {s["title"]: s["dishes"] for s in j["sections"]}
    check("offline", "sections[] group dishes by their first non-featured section; 98 dishes across them; 'Featured items' absent",
          sum(len(d) for d in sections.values()) == 98 and "Featured items" not in sections and "Chilis" in sections
          and len(sections["Chilis"]) == 9, f"got {[(t, len(d)) for t, d in sections.items()]}")
    samosa = next(d for ds in sections.values() for d in ds if d["uuid"] == SAMOSA_UUID)
    check("offline", "a dish carries uuid/title/description/section/price/was/deal/sold_out/has_options/note/price_unclear; Samosa's note, no deal",
          set(samosa) == {"uuid", "title", "description", "section", "price", "was", "deal", "sold_out", "has_options", "note", "price_unclear"}
          and samosa["note"] == "Earn $7 Uber Cash for photo" and samosa["deal"] is None and samosa["was"] is None
          and samosa["price"] == {"cents": 1319, "amount": "13.19", "currency": "CAD"}, f"got {samosa}")
    with call_stub():
        code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT])
    check("offline", "human header names the store, 'open now', today's hours, the delivery/pickup ETAs and the address",
          code == 0 and "Himalayan Kitchen & Bar" in out and "open now" in out and "12:00–23:30" in out and "delivery 52–73 Min" in out
          and "pickup 18 min" in out and "Delivery to Toronto Union Station" in out and "11 duplicate listings removed" in out
          and "$19.19  Honey Chicken Chili" in out, f"got {out[:400]!r}")

    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID])
    with call_stub():
        _, out, _ = run_cli(["menu", HIMALAYAN_UUID])
    check("offline", "menu without --at (G3): exit 0, address null, eta/pickup_eta/distance/within_range null, header says 'no address given', no cookie sent",
          code == 0 and j["address"] is None and j["store"]["eta"] is None and j["store"]["pickup_eta"] is None
          and j["store"]["distance_km"] is None and j["store"]["within_range"] is None and j["dishes_total"] == 98
          and calls[0][2] is None and cli.NO_ADDRESS in out, f"exit {code}: {str(j)[:200]}")

    # D3: a name resolves through the feed
    code, j, calls, err = run_json(["menu", "Tikka by LTH", *AT])
    check("offline", "menu '<name>' (D3): one exact match → feed then getStoreV1 on its UUID; matched_by_name says which store",
          code == 0 and [c[0] for c in calls] == ["getFeedV1", "getStoreV1"] and calls[1][1]["storeUuid"] == TIKKA_UUID
          and j["matched_by_name"] == "Tikka by LTH" and j["store"]["uuid"] == TIKKA_UUID and j["requests_used"] == 2,
          f"exit {code}: {[c[0] for c in calls]}, {err or str(j)[:200]}")
    with call_stub():
        _, out, _ = run_cli(["menu", "Tikka by LTH", *AT])
    check("offline", "…and the human output says 'Matched by name'", "Matched by name" in out and TIKKA_UUID in out)
    code, j, calls, err = run_json(["menu", "pizza", *AT])
    check("offline", "a name matching 6 stores → exit 2 (kind lookup) listing every candidate with its UUID; no menu request",
          code == 2 and j and j["ok"] is False and j["kind"] == "lookup" and len(j.get("candidates", [])) == 6
          and all(c["uuid"] in j["error"] for c in j["candidates"]) and [c[0] for c in calls] == ["getFeedV1"],
          f"exit {code}: {str(j)[:300]}")
    code, j, calls, err = run_json(["menu", "Schwartz's Deli", *AT])
    check("offline", "a name matching nothing → exit 2 (not 1: the menu request was never possible), with the not-in-feed sentence",
          code == 2 and j["kind"] == "usage" and client_mod.NOT_IN_FEED in j["error"] and [c[0] for c in calls] == ["getFeedV1"],
          f"exit {code}: {str(j)[:300]}")
    code, j, calls, err = run_json(["menu", "Tikka by LTH"])
    check("offline", "a name without --at → exit 2 before any request, explaining that a name needs an address",
          code == 2 and calls == [] and "--at" in j["error"], f"exit {code}: {j}")

    # id forms
    code, j, calls, err = run_json(["menu", "https://www.ubereats.com/ca/store/blondies-pizza-bay/" + BLONDIES_URL_ID + "?diningMode=DELIVERY", *AT])
    check("offline", "a pasted store URL → getStoreV1 on its UUID, no feed request (G6)",
          code == 0 and [c[0] for c in calls] == ["getStoreV1"] and calls[0][1]["storeUuid"] == BLONDIES_UUID and j["dishes_total"] == 34,
          f"exit {code}: {[c[0] for c in calls]}")
    code, j, calls, err = run_json(["menu", TIM_URL_ID, *AT])
    check("offline", "the bare 22-char id → the Tim Hortons UUID (the pair from the feed), no feed request",
          calls and calls[0][0] == "getStoreV1" and calls[0][1]["storeUuid"] == TIM_UUID and len(calls) == 1, f"got {calls}")
    code, j, calls, err = run_json(["menu", RANDOM_UUID, *AT])
    check("offline", "an unknown UUID (Uber: invalid_store_uuid) → exit 2, kind lookup, after exactly one request",
          code == 2 and j["ok"] is False and j["kind"] == "lookup" and "invalid_store_uuid" in j["error"] and len(calls) == 1,
          f"exit {code}: {j}")

    # filters
    code, j, *_ = run_json(["menu", ALBERTS_UUID, *AT, "--under", "11"])
    titles = [d["title"] for s in j["sections"] for d in s["dishes"]]
    check("offline", "--under 11 keeps the Jerk Chicken Special (sale 10.88, was 14.50): 45 dishes",
          code == 0 and "Jerk Chicken Special" in titles and len(titles) == 45 and j["dishes_shown"] == 45, f"got {len(titles)}")
    code, j, *_ = run_json(["menu", ALBERTS_UUID, *AT, "--under", "10.50"])
    titles = [d["title"] for s in j["sections"] for d in s["dishes"]]
    check("offline", "--under 10.50 drops it (the sale price is what is compared): 40 dishes",
          code == 0 and "Jerk Chicken Special" not in titles and len(titles) == 40, f"got {len(titles)}")
    code, j, *_ = run_json(["menu", ALBERTS_UUID, *AT, "--deals"])
    deal_dishes = [d for s in j["sections"] for d in s["dishes"]]
    check("offline", "--deals: the 7 discounted dishes, each with price, was and the 25% deal; store deals[] lists '25% off'",
          code == 0 and len(deal_dishes) == 7 and all(d["deal"]["percent"] == 25 and d["was"]["amount"] > d["price"]["amount"] for d in deal_dishes)
          and [d["text"] for d in j["deals"]] == ["25% off"] and j["store"]["has_store_promotion"] is True,
          f"got {len(deal_dishes)}")
    jerk = next(d for d in deal_dishes if d["uuid"] == JERK_UUID)
    check("offline", "the Jerk Chicken Special JSON: price {1087.5, '10.88'}, was {1450, '14.50'}",
          jerk["price"] == {"cents": 1087.5, "amount": "10.88", "currency": "CAD"} and jerk["was"] == {"cents": 1450, "amount": "14.50", "currency": "CAD"},
          f"got {jerk['price']}, {jerk['was']}")
    with call_stub():
        _, out, _ = run_cli(["menu", ALBERTS_UUID, *AT, "--deals"])
    check("offline", "human: '$10.88 (was $14.50, 25% off)  Jerk Chicken Special' and 'Deals: 25% off' in the header",
          "$10.88 (was $14.50, 25% off)  Jerk Chicken Special" in out and "Deals: 25% off" in out, f"got {out[:600]!r}")
    code, j, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--match", "samosa"])
    check("offline", "--match samosa: 1 dish, the note shown", code == 0 and j["dishes_shown"] == 1
          and j["sections"][0]["dishes"][0]["note"].startswith("Earn"))
    code, j, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--section", "chilis"])
    check("offline", "--section chilis: 9 dishes, one section", code == 0 and j["dishes_shown"] == 9 and len(j["sections"]) == 1)
    code, j, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--match", "zzzzz"])
    check("offline", "a --match that excludes everything → exit 1, dishes_total still 98", code == 1 and j["ok"] is True and j["dishes_total"] == 98)
    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID, *AT, "--pickup"])
    check("offline", "--pickup sends diningMode PICKUP and reports mode pickup with the pickup ETA '18–28 Min'",
          code == 0 and calls[0][1]["diningMode"] == "PICKUP" and j["mode"] == "pickup" and j["store"]["eta"] == "18–28 Min",
          f"exit {code}: {calls[0][1]}, {j and j['store']['eta']}")
    soldout = data(HIMALAYAN)
    for e in catalog_entries(soldout):
        if e["uuid"] == SAMOSA_UUID:
            e["isSoldOut"] = True
    code, j, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--match", "samosa"], responder_with(getStoreV1=soldout))
    code2, j2, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--match", "samosa", "--sold-out"], responder_with(getStoreV1=soldout))
    check("offline", "a sold-out dish is hidden (exit 1, dishes_sold_out 1, sold_out_hidden 1) unless --sold-out (exit 0, sold_out true)",
          code == 1 and j["dishes_sold_out"] == 1 and j["sold_out_hidden"] == 1 and code2 == 0
          and j2["sections"][0]["dishes"][0]["sold_out"] is True, f"got {code}/{code2}")

    # closed, empty, drift
    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID, *AT], responder_with(getStoreV1=synthetic_closed_store()))
    with call_stub(responder_with(getStoreV1=synthetic_closed_store())):
        _, out, _ = run_cli(["menu", HIMALAYAN_UUID, *AT])
    check("offline", "a closed store is still exit 0 with is_open false, opens '16:30', and CLOSED loud in the header",
          code == 0 and j["store"]["is_open"] is False and j["store"]["closed_message"] == "Currently closed" and j["opens"] == "16:30"
          and "CLOSED" in out and "16:30" in out, f"exit {code}: {out[:200]!r}")
    empty = data(HIMALAYAN)
    empty["catalogSectionsMap"] = {}
    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID, *AT], responder_with(getStoreV1=empty))
    check("offline", "an empty section map → exit 1, 0 dishes, not a crash", code == 1 and j["ok"] is True and j["dishes_total"] == 0,
          f"exit {code}: {str(j)[:200]}")
    missing = data(HIMALAYAN)
    del missing["catalogSectionsMap"]
    code, j, calls, err = run_json(["menu", HIMALAYAN_UUID, *AT], responder_with(getStoreV1=missing))
    check("offline", "a missing catalogSectionsMap → exit 3, kind payload", code == 3 and j["ok"] is False and j["kind"] == "payload",
          f"exit {code}: {j}")
    with call_stub() as calls:
        run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
        run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
    check("offline", "menus are never cached: two menu runs are two getStoreV1 calls", [c[0] for c in calls] == ["getStoreV1", "getStoreV1"])


def test_item_cmd() -> None:
    print("\nitem")
    code, j, calls, err = run_json(["item", BLONDIES_UUID, "Custom Pizza 16", *AT])
    check("offline", "item: menu then getMenuItemV1 with storeUuid/sectionUuid/subsectionUuid/menuItemUuid; exit 0; 2 requests",
          code == 0 and j and [c[0] for c in calls] == ["getStoreV1", "getMenuItemV1"]
          and calls[1][1] == {"itemRequestType": "ITEM", "storeUuid": BLONDIES_UUID, "sectionUuid": "ce717a04-a3b6-5a0d-90ee-59268bff40ee",
                              "subsectionUuid": "0379a62a-1156-4ddb-a09a-7e1dc45d5dfb", "menuItemUuid": PIZZA_UUID,
                              "cbType": "EATER_ENDORSED", "contextReferences": []} and j["requests_used"] == 2,
          f"exit {code}: {err or str(j)[:300]}; {calls[1:] }")
    check("offline", "JSON: dish {uuid,title,price,was,deal,sold_out}, 8 groups, from_price 23.00, required_groups 1",
          set(j["dish"]) >= {"uuid", "title", "price", "was", "deal", "sold_out"} and j["dish"]["uuid"] == PIZZA_UUID
          and len(j["groups"]) == 8 and j["from_price"]["amount"] == "23.00" and j["required_groups"] == 1
          and j["groups"][0]["required"] is True and j["store"]["uuid"] == BLONDIES_UUID, f"got {str(j)[:300]}")
    with call_stub():
        code, out, err = run_cli(["item", BLONDIES_UUID, "Custom Pizza 16", *AT])
    check("offline", "human: base price, 'From $23.00', groups with required/optional and choose min–max, Bacon +$5.75",
          code == 0 and "$23.00" in out and "From $23.00" in out and "Choose Base Sauce  (required, choose 1)" in out
          and "Add Extra Meat Toppings  (optional, choose 0–7)" in out and "+$5.75" in out and "Bacon" in out, f"got {out[:500]!r}")
    code, j, calls, err = run_json(["item", BLONDIES_UUID, "custom pizza", *AT])
    check("offline", "an ambiguous dish (two custom pizzas) → exit 2 listing both with prices; no item request",
          code == 2 and j["ok"] is False and "$23.00" in j["error"] and "$19.55" in j["error"] and [c[0] for c in calls] == ["getStoreV1"],
          f"exit {code}: {j}")
    code, j, calls, err = run_json(["item", BLONDIES_UUID, "haggis", *AT])
    check("offline", "a dish not on the menu → exit 2, kind usage", code == 2 and j["kind"] == "usage" and "haggis" in j["error"], f"exit {code}: {j}")
    code, j, calls, err = run_json(["item", "Blondies Pizza", "Custom Pizza 16", *AT])
    check("offline", "item '<name>' resolves through the feed: 3 requests, matched_by_name",
          code == 0 and [c[0] for c in calls] == ["getFeedV1", "getStoreV1", "getMenuItemV1"] and j["matched_by_name"] == "Blondies Pizza",
          f"exit {code}: {[c[0] for c in calls]}")
    code, j, calls, err = run_json(["item", HIMALAYAN_UUID, "Honey Chicken Chili"])
    check("offline", "item without --at: exit 0, address null, one free group, from_price 19.19",
          code == 0 and j["address"] is None and len(j["groups"]) == 1 and j["from_price"]["amount"] == "19.19" and j["required_groups"] == 0,
          f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["item", BLONDIES_UUID, "Custom Pizza 16", *AT], responder_with(getMenuItemV1=synthetic_required_paid_group()))
    check("offline", "synthetic required paid group through the CLI: from_price 27.50, nested group in the JSON",
          code == 0 and j["from_price"]["amount"] == "27.50" and j["groups"][0]["options"][1]["groups"][0]["title"] == "Crust",
          f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["item", ALBERTS_UUID, "Jerk Chicken Special", *AT])
    check("offline", "a discounted dish's item view carries was 14.50 and the 25% deal from the menu",
          code == 0 and j["dish"]["price"]["amount"] == "10.88" and j["dish"]["was"]["amount"] == "14.50" and j["dish"]["deal"]["percent"] == 25,
          f"exit {code}: {j and j['dish']}")


def test_deals_cmd() -> None:
    print("\ndeals")
    code, j, calls, err = run_json(["deals", *AT, "--limit", "100"])
    check("offline", "deals: exit 0, public_only true, exhaustive false, by_type has the five families, one feed request",
          code == 0 and j and j["public_only"] is True and j["exhaustive"] is False
          and set(j["by_type"]) == {"bogo", "percent", "dollar", "free_delivery", "other"} and j["requests_used"] == 1
          and j["stores_returned"] == FEED_DISTINCT, f"exit {code}: {err or str(j)[:300]}")
    check("offline", "by_type counts: 19 bogo, 10 percent, 6 dollar, 5 free_delivery stores; each entry names its store",
          len(j["by_type"]["bogo"]) == 19 and len(j["by_type"]["percent"]) == 10 and len(j["by_type"]["dollar"]) == 6
          and len(j["by_type"]["free_delivery"]) == 5 and all("store_uuid" in e and e["type"] == t for t, es in j["by_type"].items() for e in es),
          f"got {{k: len(v) for k, v in j['by_type'].items()}}")
    check("offline", "a store entry carries uuid/name/rating/eta/distance_km/deals[]", set(j["stores"][0]) >= {"uuid", "name", "rating", "eta", "distance_km", "deals"})
    with call_stub():
        code, out, err = run_cli(["deals", *AT])
    check("offline", "human: the public-only line is always printed, with the feed header and grouped sections",
          code == 0 and cli.PUBLIC_ONLY in out and "stores Uber returned near" in out and "Buy one, get one" in out and "% off" in out,
          f"got {out[:300]!r}")
    with call_stub():
        _, out, _ = run_cli(["deals", *AT, "--type", "free-delivery", "--min-rating", "4.99"])
    check("offline", "…even when nothing matches (exit 1 path)", cli.PUBLIC_ONLY in out)
    code, j, *_ = run_json(["deals", *AT, "--type", "dollar", "--min-spend-at-most", "20", "--limit", "100"])
    texts = [e["text"] for e in j["by_type"]["dollar"]]
    check("offline", "--type dollar --min-spend-at-most 20 keeps '$5 off $20+' and '$3 off $15+', drops '$5 off $50+'; other families empty",
          code == 0 and "$5 off $20+" in texts and "$3 off $15+" in texts and "$5 off $50+" not in texts and "$11 off $40+" not in texts
          and all(e["min_spend"] is None or float(e["min_spend"]) <= 20 for e in j["by_type"]["dollar"])
          and all(not j["by_type"][k] for k in ("bogo", "percent", "free_delivery", "other")), f"got {texts}")
    code, j, *_ = run_json(["deals", *AT, "--min-spend-at-most", "20", "--limit", "100"])
    check("offline", "--min-spend-at-most 20 alone keeps no-minimum deals (bogo) too",
          code == 0 and len(j["by_type"]["bogo"]) == 19 and all(e["min_spend"] is None or float(e["min_spend"]) <= 20 for es in j["by_type"].values() for e in es))
    code, j, *_ = run_json(["deals", *AT, "--type", "bogo", "--min-rating", "4.5", "--limit", "100"])
    check("offline", "--type bogo --min-rating 4.5: every store rated ≥ 4.5 and carries a bogo",
          code == 0 and j["stores"] and all(s["rating"] >= 4.5 and any(d["type"] == "bogo" for d in s["deals"]) for s in j["stores"]))
    code, j, *_ = run_json(["deals", *AT, "--limit", "2"])
    check("offline", "--limit 2 shows two stores", code == 0 and len(j["stores"]) == 2)
    quiet = data(FEED)
    for s in walk_feed_stores(quiet):
        s["signposts"] = None
        (s.get("mapMarker") or {}).pop("secondaryMarkerContent", None)
    code, j, *_ = run_json(["deals", *AT], responder_with(getFeedV1=quiet))
    check("offline", "a feed with no deal badges → exit 1, ok object, stores []", code == 1 and j["ok"] is True and j["stores"] == [])
    code, j, calls, err = run_json(["deals", *AT], responder_with(getFeedV1=synthetic_out_of_area()))
    check("offline", "outside the service area → exit 2", code == 2 and j["kind"] == "lookup")
    # --items
    code, j, calls, err = run_json(["deals", *AT, "--type", "percent", "--items", "--limit", "3"])
    alberts = next((s for s in j["stores"] if s["uuid"] == ALBERTS_UUID), None) if j else None
    check("offline", "--items --limit 3: feed + 3 menus; Albert's lists its 7 discounted dishes with was prices; the two unknown stores land in failed[]; exit 0",
          code == 0 and [c[0] for c in calls] == ["getFeedV1", "getStoreV1", "getStoreV1", "getStoreV1"] and alberts is not None
          and len(alberts["dishes"]) == 7 and all(d["was"] and d["deal"] for d in alberts["dishes"]) and len(j["failed"]) == 2
          and all(set(f) == {"uuid", "name", "reason"} for f in j["failed"]) and j["requests_used"] == 4,
          f"exit {code}: {[c[0] for c in calls]}, {err or str(j)[:300]}")
    code, j, calls, err = run_json(["deals", *AT, "--items", "--limit", "24", "--max-requests", "5"])
    check("offline", "--items over budget (1 + 24 > 5) is refused with ZERO transport calls, exit 2, naming --max-requests",
          code == 2 and calls == [] and j["ok"] is False and "--max-requests" in j["error"], f"exit {code}: {j}")
    code, j, calls, err = run_json(["deals", *AT, "--type", "bogo", "--items", "--limit", "2"],
                                   responder_with(getStoreV1=UEHTTPError("Uber Eats returned HTTP 502")))
    check("offline", "every menu failing under --items → exit 3, kind network", code == 3 and j["ok"] is False and j["kind"] == "network",
          f"exit {code}: {j}")
    code, j, calls, err = run_json(["deals", *AT, "--items", "--limit", "2"], responder_with(getStoreV1=Blocked(uehttp.BLOCKED_MESSAGE)))
    check("offline", "a Cloudflare challenge inside --items is exit 3 'bot protection', not a per-store failure",
          code == 3 and j["kind"] == "blocked" and "bot protection" in j["error"], f"exit {code}: {j}")


def _compare_responder(nearest: list[str]):
    """Menus for the nearest stores: [0] Tikka (BOGO bowl 23.49), [1] Himalayan (Butter Chicken Momo 19.19),
    [2] a synthetic Himalayan whose momo is on sale 15.00 (was 30.00) and whose Samosa mentions butter chicken
    in its description, [3] a 502; every other store unknown."""
    sale = data(HIMALAYAN)
    sale["uuid"], sale["title"] = nearest[2], "Synthetic Curry House"
    for e in catalog_entries(sale):
        if e["title"] == "Butter Chicken Momo":
            e["price"] = 1500
            e["priceTagline"] = {"text": "$15.00", "textFormat": '<span><span style="color:#05944F">$15.00 </span><span style="text-decoration:line-through">$30.00</span></span>'}
        if e["uuid"] == SAMOSA_UUID:
            e["itemDescription"] = "Crispy samosa, great beside our butter chicken."
    stores = {nearest[0]: data(TIKKA), nearest[1]: data(HIMALAYAN), nearest[2]: sale}

    def respond(n, endpoint, body, location):
        if endpoint == "getStoreV1":
            uuid = body["storeUuid"]
            if uuid in stores:
                return copy.deepcopy(stores[uuid])
            if uuid == nearest[3]:
                raise UEHTTPError("Uber Eats returned HTTP 502 — the API is failing on Uber's side")
            raise LookupFailure("Uber Eats does not know that store id (invalid_store_uuid)")
        return default_responder(n, endpoint, body, location)
    return respond


def test_compare_cmd() -> None:
    print("\ncompare")
    rows, _ = parse_feed.stores(data(FEED), UNION)
    nearest = [r.uuid for r in sorted(rows, key=lambda r: r.distance_km if r.distance_km is not None else 999.0)][:4]
    names = {r.uuid: r.name for r in rows}
    resp = _compare_responder(nearest)
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "4"], resp)
    check("offline", "compare --stores 4: feed + the 4 nearest menus by distance (Pizza Pizza, Taco Bell, Tim Hortons, Firehouse Subs)",
          code == 0 and j and [c[0] for c in calls] == ["getFeedV1"] + ["getStoreV1"] * 4
          and [c[1]["storeUuid"] for c in calls[1:]] == nearest and names[nearest[0]] == "Pizza Pizza" and names[nearest[2]] == "Tim Hortons",
          f"exit {code}: {[c[0] for c in calls]}, {err or str(j)[:300]}")
    prices = [(r["price"]["amount"], r["store"], r["second_free"]) for r in j["rows"]]
    check("offline", "rows[] rank on the SALE price: 15.00 (was 30.00) first, then 19.19, then the BOGO bowl at its full 23.49",
          prices == [("15.00", "Tim Hortons", False), ("19.19", "Taco Bell 65 Front Street West", False), ("23.49", "Pizza Pizza", True)],
          f"got {prices}")
    bogo = j["rows"][2]
    check("offline", "the BOGO row: second_free true, effective_each 1174.5 cents ('11.74'), deal 'Buy 1, get 1 free', price still 23.49",
          bogo["second_free"] is True and bogo["effective_each"]["cents"] == 1174.5 and bogo["effective_each"]["amount"] == "11.74"
          and bogo["deal"]["type"] == "bogo"
          and bogo["price"]["amount"] == "23.49" and bogo["was"] is None, f"got {bogo}")
    check("offline", "the sale row carries was 30.00 and the momo's description-only cousin is in weaker[], never rows[]",
          j["rows"][0]["was"]["amount"] == "30.00" and len(j["weaker"]) == 1 and j["weaker"][0]["dish"] == "Samosa Chaat"
          and not any(r["dish"] == "Samosa Chaat" for r in j["rows"]), f"got weaker {j['weaker']}")
    check("offline", "failed[] names the 502 store; stores_checked 3 of 4 planned; exit 0 despite the failure",
          len(j["failed"]) == 1 and j["failed"][0]["uuid"] == nearest[3] and "502" in j["failed"][0]["reason"]
          and j["stores_checked"] == 3 and j["stores_planned"] == 4 and j["stores_returned"] == FEED_DISTINCT and j["requests_used"] == 5,
          f"got {j['failed']}, checked {j['stores_checked']}")
    check("offline", "a compare row carries the documented fields",
          set(j["rows"][0]) >= {"price", "was", "deal", "second_free", "effective_each", "dish", "dish_uuid", "sold_out", "store", "store_uuid",
                                "rating", "eta", "distance_km"}, f"got {sorted(j['rows'][0])}")
    with call_stub(resp):
        code, out, err = run_cli(["compare", "butter chicken", *AT, "--stores", "4"])
    check("offline", "human: 'Cheapest … among the 4 nearest stores (of 57 …)', $15.00 first, the second-free note, the failed store",
          code == 0 and f"among the 4 nearest stores (of {FEED_DISTINCT} Uber returned near" in out and out.index("$15.00") < out.index("$23.49")
          and "second is free" in out and "Could not read 1 store menu" in out and "Weaker matches" in out, f"got {out[:600]!r}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "24", "--max-requests", "5"], resp)
    check("offline", "over budget (1 + 24 > 5): refused with ZERO transport calls, exit 2", code == 2 and calls == [] and "--max-requests" in j["error"],
          f"exit {code}: {j}")
    code, j, calls, err = run_json(["compare", "haggis", *AT, "--stores", "3"], resp)
    check("offline", "no menu lists the dish → exit 1 with rows [] (3 menus read)", code == 1 and j["ok"] is True and j["rows"] == [] and len(calls) == 4,
          f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "3"],
                                   responder_with(getStoreV1=UEHTTPError("Uber Eats returned HTTP 502")))
    check("offline", "every store failing → exit 3, kind network, failed[] has all 3", code == 3 and j["kind"] == "network" and len(j["failed"]) == 3,
          f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "3"], responder_with(getStoreV1=Blocked(uehttp.BLOCKED_MESSAGE)))
    check("offline", "a Cloudflare challenge on the first menu stops the run: exit 3 'blocked' after 2 requests, not 3 failures",
          code == 3 and j["kind"] == "blocked" and len(calls) == 2, f"exit {code}: {len(calls)} calls, {j}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "2", "--name", "tikka"])
    check("offline", "--name narrows which menus are read: only Tikka's (1 menu), its bowl ranked at full price",
          code == 0 and len(calls) == 2 and calls[1][1]["storeUuid"] == TIKKA_UUID and j["rows"][0]["price"]["amount"] == "23.49",
          f"exit {code}: {[c[0] for c in calls]}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "2", "--deals", "--min-rating", "4.6", "--max-eta", "80", "--name", "tikka"])
    check("offline", "--deals/--min-rating/--max-eta narrow before menus are read (Tikka passes all three)",
          code == 0 and len(calls) == 2 and calls[1][1]["storeUuid"] == TIKKA_UUID, f"exit {code}: {[c[0] for c in calls]}")


def test_watch_cmd() -> None:
    print("\nwatch")
    closed = synthetic_closed_store()
    soldout = data(HIMALAYAN)
    for e in catalog_entries(soldout):
        if e["uuid"] == SAMOSA_UUID:
            e["isSoldOut"] = True
    paused = data(HIMALAYAN)
    paused["isOrderable"] = False          # open but not taking orders (synthetic)
    cases = [
        # argv (after 'watch'), responder, expected exit
        ([HIMALAYAN_UUID, *AT, "--open"], None, 0),
        ([HIMALAYAN_UUID, *AT, "--open"], responder_with(getStoreV1=closed), 1),
        ([HIMALAYAN_UUID, *AT, "--open"], responder_with(getStoreV1=paused), 1),
        ([TIKKA_UUID, *AT, "--deal"], None, 0),
        ([HIMALAYAN_UUID, *AT, "--deal"], None, 1),
        ([TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "24"], None, 0),
        ([TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "23.49"], None, 0),
        ([TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "12"], None, 1),
        ([TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--deal"], None, 0),
        ([HIMALAYAN_UUID, *AT, "--item", "Samosa Chaat", "--deal"], None, 1),
        ([HIMALAYAN_UUID, *AT, "--back-in-stock", "Samosa Chaat"], None, 0),
        ([HIMALAYAN_UUID, *AT, "--back-in-stock", "Samosa Chaat"], responder_with(getStoreV1=soldout), 1),
        ([ALBERTS_UUID, *AT, "--item", "Jerk Chicken Special", "--under", "11"], None, 0),      # sale 10.88 ≤ 11
        ([ALBERTS_UUID, *AT, "--item", "Jerk Chicken Special", "--under", "14"], None, 0),      # not the was price
    ]
    for argv, resp, want in cases:
        code, j, calls, err = run_json(["watch", *argv], resp)
        check("offline", f"watch {' '.join(a for a in argv if a != UNION_TOKEN)} → exit {want}",
              code == want and j is not None and j["ok"] is True and j["fired"] is (want == 0)
              and set(j) >= {"address", "store", "condition", "fired", "observed", "checked_at", "requests_used"}
              and j["checked_at"].startswith("2026-09-13T14:02"), f"exit {code}: {err or str(j)[:300]}")
    with call_stub(responder_with(getStoreV1=closed)):
        code, out, err = run_cli(["watch", HIMALAYAN_UUID, *AT, "--open"])
    check("offline", "human 'not yet' line states the observed value and time: 'Not yet at 14:02: still closed … (opens 16:30)'",
          code == 1 and out.startswith("Not yet at 14:02") and "closed" in out.lower() and "16:30" in out, f"got {out[:200]!r}")
    with call_stub():
        code, out, err = run_cli(["watch", TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "12"])
    check("offline", "…and for a price: the dish's price and the threshold", code == 1 and "$23.49" in out and "12" in out, f"got {out[:200]!r}")
    with call_stub():
        code, out, err = run_cli(["watch", HIMALAYAN_UUID, *AT, "--open"])
    check("offline", "a fired watch prints FIRED and exits 0", code == 0 and out.startswith("FIRED at 14:02"))
    code, j, calls, err = run_json(["watch", HIMALAYAN_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "12"])
    check("offline", "watching a dish that is not on the menu → exit 2 (dead configuration), not 1",
          code == 2 and j["ok"] is False and "Butter Chicken Hefty Bowl" in j["error"], f"exit {code}: {j}")
    code, j, calls, err = run_json(["watch", HIMALAYAN_UUID, *AT, "--item", "momo", "--under", "12"])
    check("offline", "an ambiguous dish → exit 2 listing the candidates", code == 2 and j.get("candidates"), f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["watch", HIMALAYAN_UUID, *AT, "--back-in-stock", "Samosa Chaat"], responder_with(getStoreV1=closed))
    with call_stub(responder_with(getStoreV1=closed)):
        _, out, _ = run_cli(["watch", HIMALAYAN_UUID, *AT, "--back-in-stock", "Samosa Chaat"])
    check("offline", "a dish watch on a closed store still answers the dish question (exit 0) and says the store is closed",
          code == 0 and j["observed"]["is_open"] is False and "closed" in out.lower() and "16:30" in out, f"exit {code}: {out[:200]!r}")
    check("offline", "watch JSON: condition is an object {kind, dish, under}",
          isinstance(j["condition"], dict) and set(j["condition"]) >= {"kind", "dish", "under"}, f"got {j['condition']!r}")
    # no path returns 1 on an error
    error_kinds = [
        ("Cloudflare", responder_with(getStoreV1=Blocked(uehttp.BLOCKED_MESSAGE)), 3, "blocked"),
        ("HTTP 502 after retries", responder_with(getStoreV1=UEHTTPError("Uber Eats returned HTTP 502")), 3, "network"),
        ("network failure", responder_with(getStoreV1=UEHTTPError(f"cannot resolve {HOST}")), 3, "network"),
        ("payload drift", responder_with(getStoreV1={"uuid": HIMALAYAN_UUID, "title": "x"}), 3, "payload"),
        ("other failure status", responder_with(getStoreV1=PayloadError("Uber Eats reported status 'failure': rate_limited")), 3, "payload"),
        ("invalid store id", responder_with(getStoreV1=LookupFailure("invalid_store_uuid")), 2, "lookup"),
        ("a TypeError in the parser", responder_with(getStoreV1={"uuid": HIMALAYAN_UUID, "title": "x", "catalogSectionsMap": {"m": [{"payload": {"standardItemsPayload": {"catalogItems": [{"uuid": "u", "title": "t", "price": "1919"}]}}}]}}), 3, "payload"),
        ("a budget of 0 left", responder_with(getStoreV1=RequestBudgetError("request ceiling of 1 reached")), 2, "usage"),
    ]
    for label, resp, want, kind in error_kinds:
        for argv in (["--open"], ["--deal"], ["--item", "Samosa Chaat", "--under", "5"], ["--back-in-stock", "Samosa Chaat"]):
            code, j, calls, err = run_json(["watch", HIMALAYAN_UUID, *AT, *argv], resp)
            check("offline", f"watch {argv[0]} on {label} → exit {want} ({kind}), never 1",
                  code == want and code != 1 and j is not None and j["ok"] is False and j["kind"] == kind, f"exit {code}: {j}")
    for argv in ([], ["--open", "--deal"], ["--under", "5"], ["--item", "x"], ["--item", "x", "--under", "5", "--deal"]):
        code, j, calls, err = run_json(["watch", HIMALAYAN_UUID, *AT, *argv])
        check("offline", f"watch with condition {argv or 'none'} → exit 2 before any request", code == 2 and calls == [] and j["kind"] == "usage",
              f"exit {code}: {j}")


def test_doctor_cmd() -> None:
    print("\ndoctor")
    code, j, calls, err = run_json(["doctor"])
    check("offline", "doctor on healthy stubs: exit 0, three steps mapsSearchV1/getFeedV1/getStoreV1 all ok, 3 requests, transport/tls_path keys",
          code == 0 and j and j["ok"] and [s["name"] for s in j["steps"]] == ["mapsSearchV1", "getFeedV1", "getStoreV1"]
          and all(s["ok"] and not s["blocked"] for s in j["steps"]) and j["requests_used"] == 3 and "transport" in j and "tls_path" in j
          and [c[0] for c in calls] == ["mapsSearchV1", "getFeedV1", "getStoreV1"] and calls[1][2] == client_mod.DOCTOR_LOCATION,
          f"exit {code}: {err or str(j)[:300]}")
    code, j, calls, err = run_json(["doctor"], responder_with(getFeedV1=Blocked(uehttp.BLOCKED_MESSAGE)))
    check("offline", "a challenge at step 2: exit 3, kind blocked, the error names getFeedV1, steps[1].blocked true, step 3 reported as skipped",
          code == 3 and j["ok"] is False and j["kind"] == "blocked" and "getFeedV1" in j["error"] and j["steps"][1]["blocked"] is True
          and j["steps"][0]["ok"] is True and j["steps"][2]["ok"] is False and "Cloudflare" in j["error"], f"exit {code}: {str(j)[:400]}")
    with call_stub(responder_with(getFeedV1=Blocked(uehttp.BLOCKED_MESSAGE))):
        code, out, err = run_cli(["doctor"])
    check("offline", "…human mode prints BLOCKED on that step and one error line", code == 3 and "BLOCKED" in out and _only_error_line(err),
          f"got {out[:300]!r} / {err!r}")
    for label, resp in (("an unknown store at step 3", responder_with(getStoreV1=LookupFailure("invalid_store_uuid"))),
                        ("no candidates at step 1", responder_with(mapsSearchV1=[])),
                        ("a 502 at step 2", responder_with(getFeedV1=UEHTTPError("HTTP 502")))):
        code, j, calls, err = run_json(["doctor"], resp)
        check("offline", f"doctor with {label} → exit 3, never 1 or 2", code == 3 and j["ok"] is False, f"exit {code}: {str(j)[:200]}")


def test_exit_invariants() -> None:
    print("\nexit codes: --json and human identical; errors one line; no traceback; Ctrl-C 130")
    blocked = responder_with(getStoreV1=Blocked(uehttp.BLOCKED_MESSAGE))
    runs = [
        (["nearby", *AT], None), (["nearby", *AT, "--min-rating", "5", "--max-eta", "5"], None),
        (["find", "tim hortons", *AT], None), (["find", "nobody", *AT], None),
        (["menu", HIMALAYAN_UUID, *AT], None), (["menu", "pizza", *AT], None), (["menu", RANDOM_UUID, *AT], None),
        (["menu", HIMALAYAN_UUID, *AT], blocked), (["menu", HIMALAYAN_UUID, *AT, "--match", "zzz"], None),
        (["item", BLONDIES_UUID, "Custom Pizza 16", *AT], None), (["item", BLONDIES_UUID, "haggis", *AT], None),
        (["deals", *AT], None), (["compare", "butter chicken", *AT, "--stores", "2", "--name", "tikka"], None),
        (["watch", TIKKA_UUID, *AT, "--deal"], None), (["watch", HIMALAYAN_UUID, *AT, "--deal"], None),
        (["watch", HIMALAYAN_UUID, *AT, "--open"], blocked), (["doctor"], None), (["doctor"], blocked),
        (["locate", "Union Station Toronto"], None), (["nearby", *AT, "--max-requests", "0"], None),
        (["nearby"], None), (["menu", HIMALAYAN_UUID, "--bogus"], None),
    ]
    seen_codes = set()
    for argv, resp in runs:
        with call_stub(resp):
            hcode, hout, herr = run_cli(argv)
        with call_stub(resp):
            jcode, jout, jerr = run_cli(argv + ["--json"])
        payload = as_json(jout, jerr)
        seen_codes.add(hcode)
        ok = hcode == jcode and payload is not None and payload.get("ok") is (jcode in (0, 1)) and payload.get("schema_version") == 1
        if hcode in (2, 3):
            ok = ok and _only_error_line(herr) and (hout == "" or argv[0] == "doctor") and "\n" not in payload["error"].strip() \
                and payload["kind"] in ("usage", "lookup", "network", "blocked", "payload", "internal")
        else:
            ok = ok and herr == "" and "Traceback" not in hout
        check("offline", f"{' '.join(a for a in argv if a != UNION_TOKEN)}: human exit {hcode} == json exit {jcode}; errors one stderr line",
              ok, f"human {hcode} {herr[:120]!r} / json {jcode} {str(payload)[:160]}")
    check("offline", "the runs above exercised every exit code 0, 1, 2 and 3", seen_codes == {0, 1, 2, 3}, f"got {seen_codes}")
    with call_stub(responder_with(getStoreV1=KeyboardInterrupt())):
        code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT])
    check("offline", "Ctrl-C during a request → exit 130, no traceback", code == 130 and "Traceback" not in err, f"exit {code}: {err[:100]!r}")
    saved = parse_store.store
    parse_store.store = lambda *a, **k: (_ for _ in ()).throw(TypeError("'<' not supported between 'int' and 'str'"))
    try:
        with call_stub():
            hcode, hout, herr = run_cli(["menu", HIMALAYAN_UUID, *AT])
            jcode, jout, jerr = run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
    finally:
        parse_store.store = saved
    payload = as_json(jout, jerr)
    check("offline", "a TypeError inside the parser → exit 3 in both modes, one line naming the exception, no traceback",
          hcode == 3 and jcode == 3 and _only_error_line(herr) and "TypeError" in herr and payload and payload["kind"] == "payload",
          f"human {hcode} {herr[:120]!r} / json {jcode}")
    code, out, err = run_cli(["nearby", *AT, "--max-requests", "26", "--json"])
    check("offline", "--max-requests above the hard cap 25 is a usage error (exit 2) with a JSON error object",
          code == 2 and (as_json(out, err) or {}).get("kind") == "usage", f"exit {code}: {out[:100]!r}")


# ---------------------------------------------------------------------------
# [offline] review rulings (A/B/C rounds) pinned
# ---------------------------------------------------------------------------


def test_review_rulings() -> None:
    print("\nreview rulings: the budget ceiling, merges, promotion typing, prefix-only names")
    code, j, calls, err = run_json(["nearby", *AT])
    check("offline", "JSON carries requests_planned (1), requests_ceiling (plan + 2 = 3) and requests_used (1)",
          code == 0 and j["requests_planned"] == 1 and j["requests_ceiling"] == 3 and j["requests_used"] == 1, f"got {str(j)[:200]}")
    cache = tempfile.mkdtemp(prefix="addr2-", dir=_SCRATCH)
    for command, argv in (("nearby", ["nearby"]), ("deals", ["deals"]), ("find", ["find", "tim hortons"]),
                          ("menu", ["menu", "Tikka by LTH"]), ("compare", ["compare", "butter chicken", "--stores", "2", "--name", "tikka"])):
        code, j, calls, err = run_json(argv + ["--at", "Union Station Toronto"], cache=tempfile.mkdtemp(prefix="c-", dir=_SCRATCH))
        check("offline", f"{command} with a text --at: requests_used ≤ requests_planned (locate was planned, not hidden by the headroom)",
              code in (0, 1) and j["requests_used"] <= j["requests_planned"] and j["requests_planned"] >= 3,
              f"exit {code}: used {j and j.get('requests_used')} planned {j and j.get('requests_planned')}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "3", "--max-requests", "3"])
    check("offline", "--max-requests only lowers the ceiling: compare --stores 3 --max-requests 3 (plan 4) → exit 2, zero calls",
          code == 2 and calls == [], f"exit {code}: {j}")
    code, j, calls, err = run_json(["deals", *AT, "--items", "--limit", "30"])
    check("offline", "deals --items --limit 30 (plan 31 > cap 25) is refused up front", code == 2 and calls == [], f"exit {code}: {j}")
    code, j, calls, err = run_json(["deals", *AT, "--items", "--type", "percent"])
    check("offline", "deals --items defaults to --limit 10: planned 11, ceiling 13", code in (0, 1) and j["requests_planned"] == 11
          and j["requests_ceiling"] == 13, f"exit {code}: planned {j and j.get('requests_planned')}")
    # a single 5xx then a 200 succeeds under the default ceiling (real transport, session.post stubbed)
    seq = iter([500, 200])

    def post(url, **kw):
        s = next(seq)
        return _response(s, _ok_body(data(HIMALAYAN)) if s == 200 else "down")
    original_build = Transport._build_session
    clock, slept = _no_sleep()

    def build(self):
        session = original_build(self)
        if session is not None:
            session.post = post
        return session
    Transport._build_session = build
    try:
        with _patched(time=clock):
            code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
    finally:
        Transport._build_session = original_build
    j = as_json(out, err) or {}
    check("offline", "a 5xx then a 200 on a default menu run: exit 0, requests_used 2 of ceiling 3 (the retry fits the headroom)",
          code == 0 and j.get("dishes_total") == 98 and j.get("requests_used") == 2 and j.get("requests_ceiling") == 3,
          f"exit {code}: {err[:200]!r} {str(j)[:120]}")
    code, j, calls, err = run_json(["doctor", "--max-requests", "1"])
    check("offline", "doctor ignores --max-requests: still 3 requests and exit 0", code == 0 and len(calls) == 3, f"exit {code}: {len(calls)}")
    with call_stub() as calls:
        run_cli(["watch", TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "12", "--json"])
    check("offline", "watch --item reads the menu only: no getMenuItemV1", [c[0] for c in calls] == ["getStoreV1"], f"got {[c[0] for c in calls]}")
    code, j, calls, err = run_json(["menu", "Himal", *AT])
    check("offline", "a lone prefix-only name match → exit 2 listing it (never proceeds on a prefix)",
          code == 2 and j["kind"] == "lookup" and HIMALAYAN_UUID in j["error"] and [c[0] for c in calls] == ["getFeedV1"], f"exit {code}: {j}")
    rows, _ = parse_feed.stores(data(FEED), UNION)
    nearest = [r.uuid for r in sorted(rows, key=lambda r: r.distance_km if r.distance_km is not None else 999.0)][:4]

    def exhaust(n, endpoint, body, location):
        if endpoint == "getStoreV1" and body["storeUuid"] == nearest[1]:
            raise RequestBudgetError("request ceiling of 4 reached")
        return _compare_responder(nearest)(n, endpoint, body, location)
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "4"], exhaust)
    check("offline", "budget exhausted mid-compare: the store that hit it and every remaining store land in failed[] as 'not checked'; Tikka's row stands",
          code == 0 and len(j["failed"]) == 3 and sum("not checked" in f["reason"] for f in j["failed"]) == 2
          and j["rows"] and j["rows"][0]["store"] == "Pizza Pizza", f"exit {code}: {j and j.get('failed')}")
    # feed dedupe merges later copies into the first-seen (sparse) row
    merged = data(FEED)
    sparse = {"storeUuid": TIM_UUID, "title": "Tim Hortons", "actionUrl": "/store/x/" + TIM_URL_ID}
    merged["feedItems"].insert(0, {"uuid": "syn", "type": "FEATURED_STORES", "payload": {"stores": [sparse]}})
    rows2, _ = parse_feed.stores(merged, UNION)
    tim = next(r for r in rows2 if r.uuid == TIM_UUID)
    check("offline", "synthetic: a sparse FEATURED copy first, richer copies later → one row, first position, rating 4.6 / '700+' / ETA / 0.2 km filled in",
          rows2[0].uuid == TIM_UUID and sum(1 for r in rows2 if r.uuid == TIM_UUID) == 1 and tim.rating == 4.6
          and tim.rating_count_text == "700+" and (tim.eta_min, tim.eta_max) == (10, 20) and tim.distance_km == 0.2, f"got {tim}")
    # promotionType attaches only when the store has exactly one deal
    one = {"storeUuid": "s1", "title": {"text": "One"}, "signposts": [{"text": "Buy 1, get 1"}],
           "tracking": {"metaInfo": {"additionalTrackingData": {"promotionType": "BOGO"}}}}
    two = {**one, "storeUuid": "s2", "signposts": [{"text": "Buy 1, get 1"}, {"text": "$5 off"}]}
    r1, r2 = parse_feed.stores({"feedItems": [{"type": "REGULAR_STORE", "store": one}, {"type": "REGULAR_STORE", "store": two}]}, None)[0]
    check("offline", "synthetic: promotionType BOGO attaches as raw_type when the store has one deal, never when it has two",
          r1.deals[0].raw_type == "BOGO" and len(r2.deals) == 2 and all(d.raw_type is None for d in r2.deals),
          f"got {[d.to_dict() for d in r1.deals]}, {[d.to_dict() for d in r2.deals]}")
    check("offline", "a signpost 'New' is skipped; ' new ' too; 'New menu' stays type other",
          parse_feed.stores({"feedItems": [{"type": "REGULAR_STORE", "store": {**one, "tracking": {}, "signposts": [{"text": " New "}, {"text": "New menu"}]}}]}, None)[0][0].deals
          == (Deal(text="New menu", type="other"),))
    check("offline", "parse_item on a failure envelope → PayloadError, never an empty item",
          raises(PayloadError, parse_item.item, {"status": "failure", "data": {"message": "x"}}, _pizza_dish(), "CAD"))
    only_featured = data(HIMALAYAN)
    for blocks in only_featured["catalogSectionsMap"].values():
        for b in blocks:
            sp = b["payload"]["standardItemsPayload"]
            if (sp.get("title") or {}).get("text") != "Featured items":
                sp["catalogItems"] = [it for it in sp["catalogItems"] if it["uuid"] != CHILI_UUID]
    chili = next(d for d in parse_store.store(only_featured).dishes if d.uuid == CHILI_UUID)
    check("offline", "synthetic: a dish listed only under 'Featured items' still gets a section (never None)",
          isinstance(chili.section, str) and chili.section, f"got {chili.section!r}")


# ---------------------------------------------------------------------------
# [offline] review B gaps (parsers) and review C gaps (client / cli)
# ---------------------------------------------------------------------------


def _feed_of(*stores: dict) -> dict:
    return {"feedItems": [{"type": "REGULAR_STORE", "store": s} for s in stores], "isInServiceArea": True, "currencyCode": "CAD"}


def _store_min(uuid: str, name: str, **extra) -> dict:
    return {"storeUuid": uuid, "title": {"text": name}, "actionUrl": f"/store/{name.lower()}/{TIM_URL_ID}", **extra}


def _catalog(*items: dict, title: str = "Mains", promo: bool = False) -> dict:
    sp = {"title": {"text": title}, "sectionUUID": "s", "catalogItems": list(items)}
    if promo:
        sp["promoUUID"] = "p"
    return {"catalogSectionUUID": "s", "type": "VERTICAL_GRID", "payload": {"standardItemsPayload": sp}}


def _item(uuid: str, title: str, price, **extra) -> dict:
    return {"uuid": uuid, "title": title, "price": price, "priceTagline": {"text": "", "textFormat": ""}, "isSoldOut": False,
            "hasCustomizations": False, "sectionUuid": "s", "subsectionUuid": "ss", **extra}


def _store_payload(*blocks: dict, **extra) -> dict:
    return {"uuid": "st", "title": "Synthetic Store", "currencyCode": "CAD", "isOpen": True, "isOrderable": True,
            "catalogSectionsMap": {"m": list(blocks)}, **extra}


def test_review_b_gaps() -> None:
    print("\nreview B gaps: feed merges and deal edge cases, store deal markers, money, item options")
    t = parse_feed.stores
    one = t(_feed_of(_store_min("u1", "A", signposts=[{"text": "$5 off"}, {"text": "$5 off"}])), None)[0][0]
    check("offline", "F09 the same signpost text twice → one Deal", [d.text for d in one.deals] == ["$5 off"], f"got {one.deals}")
    mm = t(_feed_of(_store_min("u2", "B", signposts=None, mapMarker={"latitude": 43.6, "longitude": -79.4, "secondaryMarkerContent": {"text": "20% off"}})), None)[0][0]
    check("offline", "F10 a deal that lives only in the map marker is carried", [d.to_dict()["type"] for d in mm.deals] == ["percent"], f"got {mm.deals}")
    odd = t(_feed_of({"storeUuid": "u3", "title": {"text": "C"}, "actionUrl": "/store/c/abc"}), None)[0][0]
    check("offline", "F21 an actionUrl whose last segment is not a 22-char id → url_id None", odd.url_id is None, f"got {odd.url_id}")
    check("offline", "F26 a feed store without a title → PayloadError", raises(PayloadError, t, _feed_of({"storeUuid": "u4"}), None))
    stringy = t(_feed_of(_store_min("u5", "E", meta=[{"badgeType": "ETD", "text": "2:15PM", "accessibilityText": "2:15PM"}],
                                    tracking={"storePayload": {"etdInfo": {"dropoffETARange": "25-47"}}})), None)[0][0]
    check("offline", "F27 a tracking ETA range that is a string → eta None/None, no crash", (stringy.eta_min, stringy.eta_max) == (None, None))
    first = _store_min("u6", "F", rating={"text": "4.6", "accessibilityText": "based on more than 700 reviews"}, signposts=[{"text": "$5 off"}],
                       meta=[{"badgeType": "ETD", "accessibilityText": "Delivered in 10 to 20 min"}])
    later = _store_min("u6", "F", rating={"text": "3.0", "accessibilityText": "based on 12 reviews"}, signposts=[{"text": "20% off"}],
                       meta=[{"badgeType": "ETD", "accessibilityText": "Delivered in 50 to 60 min"}])
    merged = t(_feed_of(first, later), None)[0]
    check("offline", "F29 a later copy never overwrites a real rating, count, ETA or deals of the first",
          len(merged) == 1 and merged[0].rating == 4.6 and merged[0].rating_count_text == "700+" and merged[0].eta_min == 10
          and [d.text for d in merged[0].deals] == ["$5 off"], f"got {merged}")
    two = t(_feed_of(_store_min("u7", "G", signposts=[{"text": "Items on sale"}],
                                mapMarker={"latitude": 1, "longitude": 1, "secondaryMarkerContent": {"text": "Buy 1, get 1"}})), None)[0][0]
    check("offline", "F30 a marker that is not a prefix of the signpost is a second deal ('Items on sale' + 'Buy 1, get 1')",
          sorted(d.text for d in two.deals) == ["Buy 1, get 1", "Items on sale"], f"got {two.deals}")
    fifty = t(_feed_of(_store_min("u8", "H", signposts=[{"text": "$50 off"}],
                                  mapMarker={"latitude": 1, "longitude": 1, "secondaryMarkerContent": {"text": "$5 off"}})), None)[0][0]
    check("offline", "F31 the prefix rule needs a word boundary: '$5 off' beside '$50 off' is two deals",
          sorted(d.text for d in fifty.deals) == ["$5 off", "$50 off"], f"got {fifty.deals}")
    items_ = t(_feed_of(_store_min("u9", "I", signposts=[{"text": "Free items on sale"}],
                                   mapMarker={"latitude": 1, "longitude": 1, "secondaryMarkerContent": {"text": "Free item"}})), None)[0][0]
    check("offline", "F31b 'Free item' beside 'Free items on sale' is a character prefix but not a word prefix: two deals",
          sorted(d.text for d in items_.deals) == ["Free item", "Free items on sale"], f"got {items_.deals}")
    strdict = t(_feed_of(_store_min("u10", "J", meta=[{"badgeType": "ETD", "text": "2:15PM", "accessibilityText": "2:15PM"}],
                                    tracking={"storePayload": {"etdInfo": {"dropoffETARange": {"min": "25", "max": "47"}}}})), None)[0][0]
    check("offline", "F27b a tracking range with string min/max → eta None/None (never int('25'))", (strdict.eta_min, strdict.eta_max) == (None, None),
          f"got {strdict.eta_min}, {strdict.eta_max}")

    # parse_store
    struck = '<span><span>$9.00 </span><span style="text-decoration:line-through">$20.00</span></span>'
    s = parse_store.store(_store_payload(
        _catalog(_item("d1", "Dup", 1000), title="Featured items"),
        _catalog(_item("d1", "Dup", 900, priceTagline={"text": "$9.00", "textFormat": struck}))))
    check("offline", "S08 a duplicate copy at a different price does not donate its was", s.dishes[0].was is None and s.dishes[0].price.cents == 1000,
          f"got {s.dishes[0]}")
    tagged = parse_store.store(_store_payload(_catalog(
        _item("t1", "Tagged", 1000, itemThumbnailElements=[{"payload": {"tagsPayload": {"tags": [{"text": "25% off"}]}}}]),
        _item("t2", "Liked", 1000, itemThumbnailElements=[{"payload": {"tagsPayload": {"tags": [{"text": "#1 most liked"}]}}}]),
        _item("t3", "Typed", 1000, itemPromotion={"type": "buyXGetYItemPromotion", "buyXGetYItemPromotion": {"buyQuantity": 1, "getQuantity": 1}}),
        _item("t4", "Typed2", 1000, itemPromotion={"type": "buyXGetYItemPromotion", "buyXGetYItemPromotion": {"buyQuantity": 2, "getQuantity": 1}}),
        _item("t5", "NewBadge", 1000, promoInfo={"promoBadge": {"accessibilityText": "New"}}),
        _item("t6", "Comma", 123450, priceTagline={"text": "$1,234.50", "textFormat": '<span>$1,000.00 <span style="text-decoration:line-through">$1,234.50</span></span>'}),
    )), located=False)
    by = {d.uuid: d for d in tagged.dishes}
    check("offline", "S10 a deal text that lives only in a thumbnail tag is a deal (25% off)", by["t1"].deal is not None and by["t1"].deal.percent == 25, f"got {by['t1'].deal}")
    check("offline", "S11 a non-grammar tag ('#1 most liked') is not a deal", by["t2"].deal is None, f"got {by['t2'].deal}")
    check("offline", "S12 itemPromotion.buyXGetYItemPromotion alone → 'Buy 1, get 1 free' (bogo)",
          by["t3"].deal is not None and by["t3"].deal.type == "bogo" and by["t3"].deal.text == "Buy 1, get 1 free", f"got {by['t3'].deal}")
    check("offline", "S13 quantities 2/1 → 'Buy 2, get 1 free'", by["t4"].deal is not None and by["t4"].deal.text == "Buy 2, get 1 free", f"got {by['t4'].deal}")
    check("offline", "S26 an item badge reading 'New' is not a deal", by["t5"].deal is None, f"got {by['t5'].deal}")
    check("offline", "S25 a struck amount with a thousands separator ('$1,234.50') → was 1234.50",
          by["t6"].was is not None and by["t6"].was.cents == 123450 and by["t6"].was.amount == "1234.50", f"got {by['t6'].was}")
    miles = parse_store.store(_store_payload(distanceBadge={"text": "2,263.3 mi"}))
    check("offline", "S15 miles are converted: '2,263.3 mi' → 3642.4 km", miles.distance_km == 3642.4, f"got {miles.distance_km}")
    check("offline", "S16 integer cents stay integers in the JSON (1919, not 1919.0); fractions stay fractions",
          json.dumps(Money(1919, "CAD").to_dict()).startswith('{"cents": 1919,') and parse_store.money(1919.0, "CAD").cents == 1919
          and isinstance(parse_store.money(1919.0, "CAD").cents, int) and parse_store.money(1087.5, "CAD").cents == 1087.5)
    check("offline", "S18 a boolean price is not a price → PayloadError",
          raises(PayloadError, parse_store.store, _store_payload(_catalog(_item("b1", "Bool", True)))))
    strung = parse_store.store(_store_payload(rating={"ratingValue": "4.6", "reviewCount": 67}))
    check("offline", "S24 a string ratingValue is not a number → rating None (the code's rule); a numeric reviewCount → '67'",
          strung.rating is None and strung.rating_count_text == "67", f"got {strung.rating}, {strung.rating_count_text}")

    # parse_item
    base = _pizza_dish()
    g = parse_item.groups([{"uuid": "g", "title": "G", "maxPermitted": 1, "options": [
        {"uuid": "o", "title": "O", "price": None, "isSoldOut": False, "childCustomizationList": []}]}], "CAD")
    check("offline", "I04/I07 a group without minPermitted is optional (min 0); an option without a price is free (0)",
          g[0].min_permitted == 0 and g[0].required is False and g[0].options[0].price.cents == 0 and g[0].options[0].min_qty == 0, f"got {g}")
    check("offline", "I08 a null option → PayloadError", raises(PayloadError, parse_item.groups, [{"uuid": "g", "title": "G", "options": [None]}], "CAD"))
    d = data(ITEM_PIZZA)
    d["price"] = 2500
    it = parse_item.item(d, base, "CAD")
    check("offline", "I09 from_price starts from the ITEM's price (2500), not the dish's (2300)",
          it.price.cents == 2500 and it.from_price.cents == 2500, f"got {it.price.cents}, {it.from_price.cents}")
    deep: dict = {"uuid": "o0", "title": "O", "price": 0, "childCustomizationList": []}
    for i in range(12):
        deep = {"uuid": f"o{i + 1}", "title": "O", "price": 0, "childCustomizationList": [{"uuid": f"g{i}", "title": "G", "options": [deep]}]}
    check("offline", "I11 12-deep nested groups → PayloadError (the depth limit holds)",
          raises(PayloadError, parse_item.groups, [{"uuid": "g", "title": "G", "options": [deep]}], "CAD"))
    d = data(ITEM_PIZZA)
    d["itemPromotion"] = {"type": "buyXGetYItemPromotion", "buyXGetYItemPromotion": {"buyQuantity": 1, "getQuantity": 1}}
    it = parse_item.item(d, base, "CAD")
    check("offline", "I13 a typed promotion on the getMenuItemV1 body → deal bogo", it.deal is not None and it.deal.type == "bogo", f"got {it.deal}")


def test_review_c_gaps() -> None:
    print("\nreview C gaps: client ordering and ambiguity, cli filters, widths, tokens")
    rows, _ = parse_feed.stores(data(FEED), UNION)
    tim = next(r for r in rows if r.uuid == TIM_UUID)
    check("offline", "eta_text puts min before max: '10–20 min'", client_mod.eta_text(tim) == "10–20 min", f"got {client_mod.eta_text(tim)!r}")
    page2 = data(FEED_PAGE2)
    page2["feedItems"].append({"type": "REGULAR_STORE", "store": next(s for s in walk_feed_stores(data(FEED)) if s["storeUuid"] == TIM_UUID)})
    code, j, calls, err = run_json(["nearby", *AT, "--pages", "2", "--limit", "100"],
                                   responder_with(getFeedV1=lambda n, body, loc: page2 if body.get("pageInfo") else data(FEED)))
    check("offline", "a store on page 1 and page 2 is counted once: stores_returned 65 (57 + 8), Tim Hortons once in rows",
          code == 0 and j["stores_returned"] == FEED_DISTINCT + 8 and sum(1 for r in j["rows"] if r["uuid"] == TIM_UUID) == 1,
          f"exit {code}: {j and j.get('stores_returned')}")
    twins = _feed_of(_store_min("a" * 8 + "-0000-4000-8000-000000000001", "Twin Peaks Cafe"),
                     _store_min("b" * 8 + "-0000-4000-8000-000000000002", "Twin Peaks Cafe"))
    code, j, calls, err = run_json(["menu", "Twin Peaks Cafe", *AT], responder_with(getFeedV1=twins))
    check("offline", "two stores with the identical exact name → exit 2 listing both, no store request",
          code == 2 and len(j.get("candidates", [])) == 2 and [c[0] for c in calls] == ["getFeedV1"], f"exit {code}: {str(j)[:200]}")
    menu = parse_store.store(_store_payload(_catalog(_item("x1", "Butter Chicken Pizza", 1500), _item("x2", "Butter Chicken", 1200))))
    check("offline", "an exact dish title is preferred over a longer title containing it", Client.match_dish(menu, "Butter Chicken").uuid == "x2")
    nearest = [r.uuid for r in sorted(rows, key=lambda r: r.distance_km if r.distance_km is not None else 999.0)][:4]
    base_resp = _compare_responder(nearest)

    def soldout_resp(n, endpoint, body, location):
        out = base_resp(n, endpoint, body, location)
        if endpoint == "getStoreV1" and body["storeUuid"] == nearest[2]:
            for e in catalog_entries(out):
                if e["title"] == "Butter Chicken Momo":
                    e["isSoldOut"] = True
        return out
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "4"], soldout_resp)
    check("offline", "compare: a sold-out matching dish (the 15.00 one) sorts after every in-stock row and carries sold_out true",
          code == 0 and j["rows"][-1]["sold_out"] is True and j["rows"][-1]["price"]["amount"] == "15.00"
          and all(r["sold_out"] is False for r in j["rows"][:-1]) and len(j["rows"]) == 3 and len(j["weaker"]) == 1,
          f"got {[(r['price']['amount'], r['sold_out']) for r in j['rows']]}")
    code, j, calls, err = run_json(["nearby", *AT, "--max-requests", "25"])
    check("offline", "--max-requests never raises the ceiling: plan 1 with --max-requests 25 → ceiling 3", code == 0 and j["requests_ceiling"] == 3,
          f"got {j and j.get('requests_ceiling')}")
    code, j, calls, err = run_json(["compare", "butter chicken", *AT, "--stores", "3"],
                                   responder_with(getStoreV1=LookupFailure("Uber Eats does not know that store id (invalid_store_uuid)")))
    check("offline", "every store unknown → exit 3 with kind lookup (not network)", code == 3 and j["kind"] == "lookup", f"exit {code}: {j}")
    code, j, calls, err = run_json(["deals", *AT, "--type", "bogo", "--limit", "100"])
    check("offline", "deals --type bogo: stores[].deals hold only bogo entries even where a store has other deals",
          code == 0 and all(d["type"] == "bogo" for s in j["stores"] for d in s["deals"]) and len(j["stores"]) == 19)
    noeta = data(FEED)
    for s_ in walk_feed_stores(noeta):
        if s_["storeUuid"] == TIM_UUID:
            s_["meta"] = [m for m in s_["meta"] if m.get("badgeType") != "ETD"]
            s_.pop("tracking", None)
    code, j, calls, err = run_json(["nearby", *AT, "--sort", "eta", "--limit", "100"], responder_with(getFeedV1=noeta))
    nulls = [i for i, r in enumerate(j["rows"]) if r["eta_max"] is None]
    check("offline", "--sort eta puts rows without an ETA last", code == 0 and nulls and nulls[0] == len(j["rows"]) - len(nulls)
          and j["rows"][-1]["uuid"] == TIM_UUID, f"got null positions {nulls} of {len(j['rows'])}")
    code, j, *_ = run_json(["menu", ALBERTS_UUID, *AT, "--under", "10.88"])
    check("offline", "menu --under 10.88 keeps the 1087.5 dish (inclusive)", code == 0 and any(d["uuid"] == JERK_UUID for s_ in j["sections"] for d in s_["dishes"]))
    code, j, *_ = run_json(["menu", HIMALAYAN_UUID, *AT, "--under", "19.19", "--match", "honey chicken chili"])
    check("offline", "menu --under 19.19 keeps the 19.19 dish: the bound is inclusive to the cent", code == 0 and j["dishes_shown"] == 1, f"exit {code}")
    mixed = _feed_of(_store_min("cccccccc-0000-4000-8000-000000000003", "Mixed Deals", signposts=[{"text": "Buy 1, get 1"}, {"text": "$5 off"}],
                                mapMarker={"latitude": 43.65, "longitude": -79.38}))
    code, j, calls, err = run_json(["deals", *AT, "--type", "bogo"], responder_with(getFeedV1=mixed))
    check("offline", "deals --type bogo on a store with a bogo AND a dollar deal: stores[0].deals holds the bogo only",
          code == 0 and len(j["stores"]) == 1 and [d["type"] for d in j["stores"][0]["deals"]] == ["bogo"], f"exit {code}: {j and j['stores']}")
    code, j, calls, err = run_json(["compare", "crispy samosa", *AT, "--stores", "4"], base_resp)
    check("offline", "compare with description-only matches and no title match → exit 1, rows [] and weaker [2] (both samosas mention it)",
          code == 1 and j["ok"] is True and j["rows"] == [] and len(j["weaker"]) == 2, f"exit {code}: rows {j and len(j['rows'])} weaker {j and len(j['weaker'])}")
    tik = parse_store.store(data(TIKKA))
    nodeal = next(d for d in tik.dishes if d.deal is None and sum(1 for x in tik.dishes if fold(x.title) == fold(d.title)) == 1)
    code, j, calls, err = run_json(["watch", TIKKA_UUID, *AT, "--item", nodeal.title, "--deal"])
    check("offline", f"watch --item {nodeal.title!r} --deal on Tikka (a store WITH deals) → exit 1: the dish's own deal decides",
          code == 1 and j["fired"] is False, f"exit {code}: {str(j)[:200]}")
    code, j, calls, err = run_json(["deals", *AT, "--type", "percent", "--items", "--limit", "3"])
    failed_ids = {f["uuid"] for f in j["failed"]}
    check("offline", "deals --items: a store whose menu failed has dishes null (not []), the others a list",
          code == 0 and len(failed_ids) == 2 and all(s_["dishes"] is None for s_ in j["stores"] if s_["uuid"] in failed_ids)
          and all(isinstance(s_["dishes"], list) for s_ in j["stores"] if s_["uuid"] not in failed_ids), f"got {[(s_['uuid'][:8], s_['dishes'] is None) for s_ in j['stores']]}")
    alb = parse_store.store(data(ALBERTS))
    plain = next(d for d in alb.dishes if d.deal is None and sum(1 for x in alb.dishes if fold(x.title) == fold(d.title)) == 1)
    code, j, calls, err = run_json(["item", ALBERTS_UUID, plain.title, *AT])
    check("offline", "item on a dish without a deal, in a store that has deals → dish.deal null (store deals never leak onto the dish)",
          code == 0 and j["dish"]["deal"] is None and j["dish"]["was"] is None, f"exit {code}: {j and j['dish']}")
    with call_stub():
        code, out, err = run_cli(["nearby", *AT, "--name", "hot pot"])
    hotpot = next(r.uuid for r in rows if "大味" in r.name)
    header = next(ln for ln in out.splitlines() if ln.startswith("#  Store"))
    row = next(ln for ln in out.splitlines() if hotpot in ln)

    def offset(line: str, marker: str) -> int:
        return sum(2 if __import__("unicodedata").east_asian_width(c) in "WF" else 1 for c in line[: line.index(marker)])
    check("offline", "a CJK store name (大味麻辣烫) keeps the UUID column aligned by display width",
          code == 0 and "大味麻辣烫" in row and offset(header, "UUID") == offset(row, hotpot), f"header {header!r} row {row!r}")
    code, j, calls, err = run_json(["locate", "Union Station Toronto", "--pick", "2"])
    check("offline", "locate --json: only the picked candidate carries a location token, the others null",
          code == 0 and j["candidates"][1]["location"] == j["token"] and all(c["location"] is None for i, c in enumerate(j["candidates"]) if i != 1),
          f"got {[c['location'] is not None for c in j['candidates']]}")


# ---------------------------------------------------------------------------
# [offline] Uber's own bot defense (reCAPTCHA 403 JSON after ~110 requests/day)
# ---------------------------------------------------------------------------

BOTDEFENSE = "botdefense_challenge.json"


def test_botdefense() -> None:
    print("\nUber's own bot defense: a 403 JSON challenge is a block, never 'nothing found'")
    body = fixture_text(BOTDEFENSE)
    doc = json.loads(body)
    check("offline", "the fixture is the live body: status failure, metadata.botdefense.state challenge, provider RECAPTCHA",
          doc["status"] == "failure" and doc["metadata"]["botdefense"]["state"] == "challenge" and doc["metadata"]["botdefense"]["provider"] == "RECAPTCHA")
    check("offline", "botdefense_provider() reads the provider from the body and ignores other JSON",
          uehttp.botdefense_provider(body) == "recaptcha" and uehttp.botdefense_provider(_ok_body({})) is None
          and uehttp.botdefense_provider('{"status":"failure","metadata":{"botdefense":{"state":"ok"}}}') is None)
    for status in (403, 200):
        out, err, t, posts, slept = _requests_call(lambda url, **kw: _response(status, body, {"content-type": "application/json"}))
        check("offline", f"requests path: {status} + the botdefense body → Blocked(provider recaptcha), 1 call, charged once, no retry sleep",
              isinstance(err, Blocked) and err.provider == "recaptcha" and len(posts) == 1 and t.requests_made == 1 and slept == []
              and "bot defense" in str(err) and "reCAPTCHA" in str(err) and "nothing found" in str(err).lower(), f"got {err!r}, {len(posts)} posts")
    out, err, t, argvs, fetched, _ = _curl_call([(0, _curl_stdout(403, body, "content-type: application/json"), b"")])
    check("offline", "curl path: 403 + the botdefense body → Blocked recaptcha, one spawn", isinstance(err, Blocked) and err.provider == "recaptcha"
          and len(argvs) == 1 and fetched == [], f"got {err!r}")
    out, err, seen, t = _urllib_call(raise_exc=_http_error(403, body, {"content-type": "application/json"}))
    check("offline", "urllib path: HTTPError 403 + the botdefense body → Blocked recaptcha, one call", isinstance(err, Blocked)
          and err.provider == "recaptcha" and len(seen) == 1, f"got {err!r}")
    out, err, *_ = _requests_call(lambda url, **kw: _response(403, fixture_text(CHALLENGE)))
    check("offline", "the Cloudflare page still classifies as provider cloudflare with the Cloudflare message",
          isinstance(err, Blocked) and err.provider == "cloudflare" and "Cloudflare" in str(err), f"got {err!r}")
    blocked = responder_with(getFeedV1=Blocked(uehttp.BOTDEFENSE_MESSAGE, provider="recaptcha"),
                             getStoreV1=Blocked(uehttp.BOTDEFENSE_MESSAGE, provider="recaptcha"),
                             getMenuItemV1=Blocked(uehttp.BOTDEFENSE_MESSAGE, provider="recaptcha"))
    for argv in (["nearby", *AT], ["find", "tim hortons", *AT], ["menu", HIMALAYAN_UUID, *AT], ["item", BLONDIES_UUID, "Custom Pizza 16", *AT],
                 ["deals", *AT], ["compare", "butter chicken", *AT, "--stores", "2"], ["watch", HIMALAYAN_UUID, *AT, "--open"],
                 ["watch", TIKKA_UUID, *AT, "--deal"], ["watch", TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "12"],
                 ["watch", HIMALAYAN_UUID, *AT, "--back-in-stock", "Samosa Chaat"]):
        code, j, calls, err = run_json(argv, blocked)
        with call_stub(blocked):
            hcode, hout, herr = run_cli(argv)
        check("offline", f"{argv[0]} under Uber's bot defense → exit 3 kind blocked (never 1) naming the bot defense and reCAPTCHA, both modes",
              code == 3 and hcode == 3 and j["ok"] is False and j["kind"] == "blocked" and "bot defense" in j["error"] and "reCAPTCHA" in j["error"]
              and "bot defense" in herr and _only_error_line(herr), f"exit {code}/{hcode}: {j and j.get('error')!r} / {herr[:120]!r}")
    code, j, calls, err = run_json(["doctor"], blocked)
    check("offline", "doctor: mapsSearchV1 ok, getFeedV1 blocked with the provider named (recaptcha), exit 3 kind blocked",
          code == 3 and j["kind"] == "blocked" and j["steps"][0]["ok"] is True and j["steps"][1]["blocked"] is True
          and "recaptcha" in j["steps"][1]["detail"].lower() and "getFeedV1" in j["error"] and j["steps"][1].get("provider") == "recaptcha",
          f"exit {code}: {str(j)[:400]}")
    with call_stub(blocked):
        code, out, err = run_cli(["doctor"])
    check("offline", "doctor human output prints BLOCKED and names reCAPTCHA / bot defense on that step",
          code == 3 and "BLOCKED" in out and ("reCAPTCHA" in out or "recaptcha" in out.lower()) and "Cloudflare" not in out.split("getFeedV1", 1)[1].splitlines()[0],
          f"got {out[:400]!r}")


# ---------------------------------------------------------------------------
# [offline] §2.8 hostile environment
# ---------------------------------------------------------------------------


def test_hostile_environment() -> None:
    print("\nhostile environment: no HOME, read-only cwd, no requests, no curl, the launcher from elsewhere")
    saved_home, saved_cwd = os.environ.get("HOME"), os.getcwd()
    saved_cache = os.environ.pop("UBEREATS_CACHE_DIR", None)
    ro = tempfile.mkdtemp(prefix="ro-cwd-", dir=_SCRATCH)
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
    try:
        os.environ.pop("HOME", None)
        os.chdir(ro)
        with call_stub() as calls:
            code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"], cache=None)
        payload = as_json(out, err) or {}
        with call_stub():
            code2, out2, err2 = run_cli(["nearby", "--at", "Union Station Toronto", "--json"])
        left = os.listdir(ro)
    finally:
        os.chmod(ro, stat.S_IRWXU)
        os.chdir(saved_cwd)
        if saved_home is not None:
            os.environ["HOME"] = saved_home
        if saved_cache is not None:
            os.environ["UBEREATS_CACHE_DIR"] = saved_cache
    check("offline", "no HOME + read-only cwd: menu still answers (exit 0, 98 dishes) and writes nothing to the cwd",
          code == 0 and payload.get("dishes_total") == 98 and left == [], f"exit {code}: {err[:200]!r}; left {left}")
    check("offline", "…and an address lookup still works there (cache falls back to memory/tempdir, never fails the command)",
          code2 == 0 and (as_json(out2, err2) or {}).get("stores_returned") == FEED_DISTINCT, f"exit {code2}: {err2[:200]!r}")

    # no `requests`: the CLI's own transport goes through curl (subprocess stubbed)
    fake, argvs = _fake_subprocess([(0, _curl_stdout(200, json.dumps(envelope(HIMALAYAN))), b"")])
    with _patched(requests=None, shutil=_fake_shutil("/usr/bin/curl"), subprocess=fake):
        code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
    payload = as_json(out, err) or {}
    check("offline", "requests absent → the real transport uses curl: menu exits 0 with 98 dishes, one POST spawned",
          code == 0 and payload.get("dishes_total") == 98 and len(argvs) == 1 and "POST" in argvs[0][0]
          and argvs[0][0][-1].endswith("/_p/api/getStoreV1?localeCode=ca"), f"exit {code}: {err[:200]!r}; {len(argvs)} spawns")
    # no curl either: urllib
    seen: list = []
    original_opener = uehttp.urllib.request.build_opener

    class _Resp:
        status, headers = 200, {"content-type": "application/json"}

        def read(self):
            return json.dumps(envelope(HIMALAYAN)).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req.full_url)
            return _Resp()

    uehttp.urllib.request.build_opener = lambda *h: _Opener()
    try:
        with _patched(requests=None, shutil=_fake_shutil(None)):
            code, out, err = run_cli(["menu", HIMALAYAN_UUID, *AT, "--json"])
    finally:
        uehttp.urllib.request.build_opener = original_opener
    payload = as_json(out, err) or {}
    check("offline", "requests and curl absent → urllib: menu exits 0 with 98 dishes, one request",
          code == 0 and payload.get("dishes_total") == 98 and len(seen) == 1, f"exit {code}: {err[:200]!r}; {seen}")
    # curl present but unspawnable hands the attempt to urllib, charged once
    out_, err_, t, argvs, fetched, _ = _curl_call([(0, b"", b"")], oserror=True)
    check("offline", "curl that cannot be spawned → the same attempt goes to urllib, charged once",
          err_ is None and len(argvs) == 1 and len(fetched) == 1 and t.requests_made == 1, f"got {err_!r}")
    for code_, label in ((6, "cannot resolve"), (7, "cannot connect"), (5, "proxy")):
        out_, err_, *_ = _curl_call([(code_, b"\n000", b"curl: fail")])
        check("offline", f"curl exit {code_} ({label}) is a one-line final UEHTTPError naming the host",
              isinstance(err_, UEHTTPError) and not isinstance(err_, Blocked) and "\n" not in str(err_) and (HOST in str(err_) or "proxy" in str(err_)),
              f"got {err_!r}")
    saved_env = {k: os.environ.get(k) for k in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")}
    try:
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["HTTPS_PROXY"] = "http://proxy.internal:3128"
        os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/corp.pem"
        args = uehttp._curl_env_args()
        check("offline", "curl is handed the proxy and CA bundle requests would use, never -k",
              args[args.index("--proxy") + 1] == "http://proxy.internal:3128" and args[args.index("--cacert") + 1] == "/etc/ssl/corp.pem"
              and not any(a in ("-k", "--insecure") for a in args), f"got {args}")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # the launcher from a foreign cwd, no HOME, no network needed (a refused plan)
    launcher = os.path.join(HERE, "ubereats.py")
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "UBEREATS_CACHE_DIR")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="ueats-cwd-", dir=_SCRATCH) as cwd:
        proc = subprocess.run([sys.executable, launcher, "nearby", *AT, "--pages", "2", "--max-requests", "1", "--json"],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        left = sorted(os.listdir(cwd))
    payload = as_json(proc.stdout, proc.stderr) or {}
    check("offline", "ubereats.py by absolute path from a foreign cwd with no HOME: runs, refuses the over-budget plan (exit 2), leaves the cwd empty",
          proc.returncode == 2 and payload.get("kind") == "usage" and "--max-requests" in payload.get("error", "") and left == [],
          f"exit {proc.returncode}: {(proc.stdout + proc.stderr)[:300]!r}; left {left}")
    proc = subprocess.run([sys.executable, "-m", "ueats", "menu", "Tikka by LTH", "--json"], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=60)
    payload = as_json(proc.stdout, proc.stderr) or {}
    check("offline", "python3 -m ueats is the same entry point (a name without --at → exit 2, no request)",
          proc.returncode == 2 and payload.get("kind") == "usage" and "--at" in payload.get("error", ""),
          f"exit {proc.returncode}: {(proc.stdout + proc.stderr)[:300]!r}")
    proc = subprocess.run([sys.executable, launcher, "--help"], cwd="/", env=env, capture_output=True, text=True, timeout=60)
    check("offline", "ubereats.py --help from / lists the nine commands",
          proc.returncode == 0 and all(c in proc.stdout for c in ("locate", "nearby", "find", "menu", "item", "deals", "compare", "watch", "doctor")),
          f"exit {proc.returncode}: {proc.stdout[:200]!r}")


# ---------------------------------------------------------------------------
# [offline] §2.9 docs agree with code; search is not used
# ---------------------------------------------------------------------------

COMMANDS = ("locate", "nearby", "find", "menu", "item", "deals", "compare", "watch", "doctor")


def _help(command: str) -> str:
    code, out, err = run_cli([command, "--help"])
    return out + err


def _split_depth0(cell: str) -> list[str]:
    items, item, depth = [], "", 0
    for ch in cell:
        depth += ch in "({["
        depth -= ch in ")}]"
        if ch == "," and depth == 0:
            items.append(item)
            item = ""
        else:
            item += ch
    items.append(item)
    return items


def _key_table(text: str) -> dict[str, list[tuple[str, list[str]]]]:
    """{command: [(top-level key, [documented subkeys])]} from SKILL.md's JSON key table."""
    table: dict[str, list[tuple[str, list[str]]]] = {}
    section = text.split("## JSON output", 1)[1].split("\n## ", 1)[0] if "## JSON output" in text else ""
    for m in re.finditer(r"^\| `(\w+)` \| (.+?) \|\s*$", section, re.M):
        command, cell = m.group(1), m.group(2)
        if command not in COMMANDS:
            continue
        entries = []
        for it in _split_depth0(cell):
            km = re.match(r"\s*`([a-z_]+)(?:\[\])?(?::[^`]*)?`", it)
            if not km:
                continue
            key = km.group(1)
            sub: list[str] = []
            pm = re.search(r"\((.*)\)", it[km.end():])
            if pm:
                sub = [s.strip() for s in re.findall(r"`([^`]+)`", pm.group(1)) for s in s.split(",")]
                sub = [re.sub(r"\[\]$", "", s.strip()) for s in sub if re.fullmatch(r"\s*[a-z_]+(\[\])?\s*", s)]
            entries.append((key, sub))
        table[command] = entries
    return table


def test_docs_agree() -> None:
    print("\nthe docs agree with the code (SKILL.md, recipes.md)")
    check("offline", "SKILL.md and recipes.md exist beside scripts/", os.path.exists(SKILL_MD) and os.path.exists(RECIPES_MD))
    if not (os.path.exists(SKILL_MD) and os.path.exists(RECIPES_MD)):
        return
    with open(SKILL_MD, encoding="utf-8") as fh:
        skill = fh.read()
    with open(RECIPES_MD, encoding="utf-8") as fh:
        recipes = fh.read()
    helps = {c: _help(c) for c in COMMANDS}
    check("offline", "every command's --help renders (exit 0) and names the command", all(c in helps[c] for c in COMMANDS))

    # the command table: `command …` | flags | requests
    table_rows = re.findall(r"^\| `(\w+)[^`]*` \| (.*?) \| (.*?) \|\s*$", skill, re.M)
    documented = {c for c, _, _ in table_rows if c in COMMANDS}
    check("offline", "the command table documents all nine commands", documented == set(COMMANDS), f"got {sorted(documented)}")
    for command, adds, _ in table_rows:
        if command not in COMMANDS:
            continue
        flags = set(re.findall(r"`(--[a-z-]+)", adds))
        missing = [f for f in flags if f not in helps[command]]
        check("offline", f"{command}: every flag in the command table exists in argparse ({len(flags)} flags)",
              not missing, f"missing {missing}")
    shared = set(re.findall(r"`(--[a-z-]+)", skill.split("## The nine commands", 1)[1].split("\n|", 1)[0])) if "## The nine commands" in skill else set()
    check("offline", "the shared flags (--at, --pickup, --json, --max-requests, --locale) exist on every command that takes them",
          {"--json", "--max-requests", "--locale"} <= shared and all(f in helps[c] for c in COMMANDS for f in ("--json", "--max-requests", "--locale"))
          and all("--at" in helps[c] for c in COMMANDS if c not in ("locate", "doctor"))
          and all("--pickup" in helps[c] for c in ("menu", "item", "compare", "watch")), f"shared {sorted(shared)}")
    choosing = skill.split("## Choosing a command", 1)[1].split("\n## ", 1)[0]
    for command, cell in re.findall(r"\| `(\w+) [^`]*`([^\n]*)", choosing):
        flags = set(re.findall(r"`(--[a-z-]+)", cell))
        missing = [f for f in flags if command in helps and f not in helps[command]]
        check("offline", f"choosing-a-command row for {command}: flags exist ({sorted(flags)})", command in COMMANDS and not missing, f"missing {missing}")
    codes = set(re.findall(r"^\| `(\d)` \|", skill, re.M))
    check("offline", "the exit-code table documents exactly 0, 1, 2, 3", codes == {"0", "1", "2", "3"}, f"got {codes}")
    flat = " ".join(skill.split())
    check("offline", "SKILL.md quotes the Cloudflare message the transport actually raises",
          uehttp.BLOCKED_MESSAGE.split(" — ")[0] in flat and "doctor` shows which step is blocked" in flat)
    check("offline", "SKILL.md says a find miss is not proof the restaurant isn't on Uber Eats (the sentence client.NOT_IN_FEED emits)",
          "proof the restaurant isn't on Uber Eats" in flat and "isn't on Uber Eats" in client_mod.NOT_IN_FEED)

    # JSON keys: every documented top-level key (and documented subkeys) is emitted by a fixture run
    table = _key_table(skill)
    check("offline", "the JSON key table was found for all nine commands", set(table) == set(COMMANDS), f"got {sorted(table)}")
    rows, _ = parse_feed.stores(data(FEED), UNION)
    nearest = [r.uuid for r in sorted(rows, key=lambda r: r.distance_km if r.distance_km is not None else 999.0)][:4]
    runs = {
        "locate": (["locate", "Union Station Toronto"], None),
        "nearby": (["nearby", *AT], None), "find": (["find", "tim hortons", *AT], None),
        "menu": (["menu", ALBERTS_UUID, *AT], None), "item": (["item", BLONDIES_UUID, "Custom Pizza 16", *AT], None),
        "deals": (["deals", *AT, "--type", "percent", "--items", "--limit", "3"], None),
        "compare": (["compare", "butter chicken", *AT, "--stores", "4"], _compare_responder(nearest)),
        "watch": (["watch", TIKKA_UUID, *AT, "--item", "Butter Chicken Hefty Bowl", "--under", "24"], None),
        "doctor": (["doctor"], None),
    }
    payloads = {}
    for command, (argv, resp) in runs.items():
        code, j, calls, err = run_json(argv, resp)
        payloads[command] = (code, j)
    for command, entries in table.items():
        code, payload = payloads[command]
        if payload is None or payload.get("ok") is not True:
            check("offline", f"{command}: the fixture-driven --json run produced an ok object", False, f"exit {code}: {str(payload)[:200]}")
            continue
        missing = [k for k, _ in entries if k not in payload]
        check("offline", f"{command}: every documented top-level key is emitted ({len(entries)} keys)", not missing and entries, f"missing {missing}")
        for key, sub in entries:
            if not sub or key not in payload:
                continue
            value = payload[key]
            first = value[0] if isinstance(value, list) and value else value if isinstance(value, dict) else None
            if first is None:
                check("offline", f"{command}.{key}: has an element to check {sub} on", False, f"got {value!r}")
                continue
            lacking = [s for s in sub if s not in first]
            check("offline", f"{command}.{key}: documented fields {sub} present", not lacking, f"missing {lacking} in {sorted(first)}")
    # the prose shape lists: `{a, b|null, c[]}` — matched to a payload by their first keys
    shapes = {}
    for m in re.finditer(r"`?\{([a-z_]+(?:\[\])?(?:\|null)?(?:,\s*[a-z_]+(?:\[\])?(?:\|null)?(?:\{[^}]*\})?)+)\}`?", skill.split("## JSON output", 1)[1].split("\n## ", 1)[0]):
        keys = [re.sub(r"\[\]|\|null|\{.*", "", k.strip()) for k in re.split(r",(?![^{]*\})", m.group(1))]
        shapes[tuple(keys[:3])] = keys
    nearby_row = payloads["nearby"][1]["rows"][0]
    menu = payloads["menu"][1]
    item = payloads["item"][1]
    compare = payloads["compare"][1]
    targets = {
        ("uuid", "name", "rating"): ("a feed store row", nearby_row),
        ("uuid", "title", "slug"): ("menu.store", menu["store"]),
        ("uuid", "title", "description"): ("a dish", menu["sections"][0]["dishes"][0]),
        ("uuid", "title", "required"): ("an item group", item["groups"][0]),
        ("uuid", "title", "price"): ("an item option", item["groups"][0]["options"][0]),
        ("price", "was", "deal"): ("a compare row", compare["rows"][0]),
        ("uuid", "name", "reason"): ("a failed[] entry", compare["failed"][0]),
        ("kind", "dish", "under"): ("watch.condition", payloads["watch"][1]["condition"]),
    }
    check("offline", "the prose shape lists in SKILL.md were found (≥ 6)", len(shapes) >= 6, f"got {list(shapes)}")
    for head, keys in shapes.items():
        if head not in targets:
            continue
        label, obj = targets[head]
        lacking = [k for k in keys if not isinstance(obj, dict) or k not in obj]
        check("offline", f"{label} carries the documented shape {keys}", not lacking, f"missing {lacking} in {obj if not isinstance(obj, dict) else sorted(obj)}")
    # recipes: every command line in a code block is a real command with real flags
    used = re.findall(r"ubereats\.py (\w+)((?: [^\n]*)?)", recipes)
    check("offline", "recipes.md shows ≥ 12 command lines", len(used) >= 12, f"got {len(used)}")
    for command, rest in used:
        flags = set(re.findall(r"(--[a-z-]+)", rest))
        missing = [f for f in flags if command in helps and f not in helps[command]]
        check("offline", f"recipes: `{command} {' '.join(sorted(flags))}` — command and flags exist", command in COMMANDS and not missing,
              f"missing {missing}")
    flat_recipes = " ".join(recipes.split())
    check("offline", "recipes.md quotes the exit-code rule (only 1 means keep waiting) and the exact not-in-feed sentence the CLI prints",
          "Only `1` means" in flat_recipes and client_mod.NOT_IN_FEED.split(";")[0] in flat_recipes)

    # search is not used
    source = ""
    for fn in sorted(os.listdir(os.path.join(HERE, "ueats"))):
        if fn.endswith(".py"):
            with open(os.path.join(HERE, "ueats", fn), encoding="utf-8") as fh:
                source += fh.read()
    check("offline", "no code path calls a search endpoint (getSearchFeedV1 / getSearchSuggestionsV1) or sets userQuery",
          "getSearch" not in source and source.count('"userQuery"') == 1 and client_mod.FEED_BODY["userQuery"] == ""
          and os.path.exists(os.path.join(FIXTURES, SEARCH_IGNORED + ".gz")))
    named = set(re.findall(r'"(get\w+V\d|mapsSearchV\d)"', source))
    check("offline", "every endpoint name in the source is on the allowlist", named <= ALLOWED_ENDPOINTS,
          f"got {named - ALLOWED_ENDPOINTS}")
    check("offline", "the source never writes a menu/feed/item response to disk (no open(…,'w') outside the location cache)",
          all("open(" not in line or fn == "location.py" or "encoding" not in line or '"w"' not in line
              for fn in os.listdir(os.path.join(HERE, "ueats")) if fn.endswith(".py")
              for line in open(os.path.join(HERE, "ueats", fn), encoding="utf-8")))


# ---------------------------------------------------------------------------
# [network]
# ---------------------------------------------------------------------------


def test_live(skip_doctor: bool = False) -> None:
    n = 6 + (0 if skip_doctor else 3)
    print(f"\nlive calls to {HOST} ({n} requests, 1 s apart)")
    hint = "if `doctor` reports a Cloudflare block, the site is refusing us, not the skill breaking"

    def why(e: Exception) -> str:
        if isinstance(e, Blocked):
            who = "Uber's own bot defense (reCAPTCHA)" if getattr(e, "provider", "") == "recaptcha" else "Cloudflare"
            return f"BLOCKED by {who} — the site is refusing this address today, not the skill breaking: {e}"
        return f"{type(e).__name__}: {e} — {hint}"
    c = Client(Transport(max_requests=6, throttle=1.0))
    try:
        cands, loc = c.locate("Union Station Toronto")
    except Exception as e:  # noqa: BLE001
        check("network", "locate 'Union Station Toronto'", False, f"{HOST}: {why(e)}")
        return
    check("network", "locate returns ≥ 1 candidate and a location with coordinates",
          len(cands) >= 1 and loc.latitude is not None and loc.longitude is not None, f"{HOST}: got {cands[:1]}, {loc}")
    try:
        rows, meta = c.feed(loc)
    except Exception as e:  # noqa: BLE001
        check("network", "nearby --at that location", False, f"{HOST}: {why(e)}")
        return
    check("network", "the feed near it holds ≥ 20 stores, in the service area, ≥ 1 with a deal, every row with a uuid and name",
          len(rows) >= 20 and meta.in_service_area and sum(1 for r in rows if r.deals) >= 1 and all(r.uuid and r.name for r in rows),
          f"{HOST}: {len(rows)} stores, in_area {meta.in_service_area}, {sum(1 for r in rows if r.deals)} with deals — {hint}")
    first = next((r for r in rows if r.deals and r.rating is not None), next((r for r in rows if r.deals), rows[0]))
    try:
        store = c.menu(first.uuid, loc)
    except Exception as e:  # noqa: BLE001
        check("network", f"menu {first.name}", False, f"{HOST}: {why(e)}")
        return
    check("network", f"menu {first.name!r}: ≥ 5 dishes, every price > 0, no duplicate uuids after dedupe",
          len(store.dishes) >= 5 and all(d.price.cents > 0 for d in store.dishes) and len({d.uuid for d in store.dishes}) == len(store.dishes),
          f"{HOST}: {len(store.dishes)} dishes, {store.entries_total} entries — {hint}")
    with_opts = next((d for d in store.dishes if d.has_options), None)
    if with_opts is None:
        check("network", "item on a dish with options", False, f"{HOST}: {first.name} lists no dish with options — try again")
    else:
        try:
            detail = c.item_for(store, with_opts, loc)
            check("network", f"item {with_opts.title!r}: ≥ 1 option group, from_price ≥ price",
                  len(detail.groups) >= 1 and detail.from_price.cents >= detail.price.cents,
                  f"{HOST}: {len(detail.groups)} groups — {hint}")
        except Exception as e:  # noqa: BLE001
            check("network", f"item {with_opts.title!r}", False, f"{HOST}: {why(e)}")
    err = raised(c.menu, RANDOM_UUID, loc)
    check("network", "getStoreV1 with a random UUID is a 200-failure → LookupFailure (exit 2), live",
          isinstance(err, LookupFailure), f"{HOST}: {why(err) if isinstance(err, Exception) else err!r}")
    if skip_doctor:
        return
    time.sleep(1.0)
    code, out, err = run_cli(["doctor", "--json"], pin=False)
    payload = as_json(out, err) or {}
    check("network", "doctor exits 0 with all three steps ok and a named transport",
          code == 0 and all(s.get("ok") for s in payload.get("steps", [])) and payload.get("transport") in ("requests", "curl", "urllib"),
          f"{HOST}: exit {code}: " + (("BLOCKED — " if payload.get("kind") == "blocked" else "") + str(payload.get("error") or str(payload)[:300])) + f" — {hint}")


# ---------------------------------------------------------------------------


def _audit_writes() -> list[str]:
    """Anything a command left on disk besides the location cache."""
    stray = []
    for root, dirs, files in os.walk(_SCRATCH):
        for f in files:
            path = os.path.join(root, f)
            if f == "locations.json":
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if any(k in text for k in ("feedItems", "catalogSectionsMap", "customizationsList", "priceTagline")):
                    stray.append(path + " (holds a response)")
            elif not f.startswith(".probe-"):
                stray.append(path)
    return stray


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip every test that opens a socket")
    args = parser.parse_args()

    print("uber-eats self-check")
    before = sorted(f for f in os.listdir(HERE) if f != "__pycache__")
    groups = [
        test_location, test_transport, test_review_a_gaps, test_feed, test_deal_grammar, test_menu_parsing, test_item_options,
        test_ids, test_locate_cmd, test_nearby_cmd, test_find_cmd, test_menu_cmd, test_item_cmd, test_deals_cmd,
        test_compare_cmd, test_watch_cmd, test_doctor_cmd, test_exit_invariants, test_review_rulings, test_review_b_gaps, test_review_c_gaps, test_botdefense, test_hostile_environment,
        test_docs_agree,
    ]
    for group in groups:
        try:
            group()
        except Exception as e:  # noqa: BLE001 - a crashed group is a failure, not the end of the run
            import traceback
            check("offline", f"{group.__name__} ran to completion", False, f"crashed with {type(e).__name__}: {e} @ {traceback.format_exc().splitlines()[-3]}")
    stray = _audit_writes()
    check("offline", "no command wrote a feed, menu or item response anywhere (walked every cache dir the suite used)", not stray, f"stray {stray[:5]}")
    after = sorted(f for f in os.listdir(HERE) if f != "__pycache__")
    check("offline", "nothing was written next to the scripts", before == after, f"new {sorted(set(after) - set(before))}")
    if not args.offline:
        try:
            test_live()
        except Exception as e:  # noqa: BLE001
            check("network", "test_live ran to completion", False, f"{HOST}: crashed with {type(e).__name__}: {e}")
    else:
        print("\n[network] skipped (--offline)")
    shutil.rmtree(_SCRATCH, ignore_errors=True)

    print(f"\n{_passed} passed, {len(_failures)} failed")
    for failure in _failures:
        print(f"  FAIL {failure}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
