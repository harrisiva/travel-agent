#!/usr/bin/env python3
"""Self-check for the google-flights skill.

    python3 test_flights.py            # everything
    python3 test_flights.py --offline  # no sockets; safe in any sandbox

Two labelled groups:

    [offline]  pure logic against a saved real payload — encoder, parser,
               guards, filters, validation. No network.
    [network]  live calls to Google. A failure here names the host, so
               "Google is blocking this IP" stays distinguishable from
               "the skill is broken".

What these tests are actually for: the payload is a positional, untyped JSON
array, so the dangerous bug is not a crash — it is the answer that looks
plausible and is wrong. A price read from a shifted index is still an integer.
So the offline group asserts on *values from a known capture* rather than on
"it returned something", and deliberately feeds the parser a shifted payload to
prove the guards fire.

Two rules learned the hard way, both from tests in this file that passed while
checking nothing:

* **Never assert `all(...)` over a sequence the fixture might not contain.**
  Three tests here filtered a capture for midnight arrivals and mixed-carrier
  itineraries, found none — the only fixture was four nonstop, single-carrier,
  same-day flights — and passed vacuously over the empty list. They covered
  fixes made after review, which makes a false pass worse than no test at all.
  Every such assertion now also states how many rows it expected to see.
* **Assert what is dropped as well as what survives.** "Everything left
  satisfies the filter" is equally true of a filter that kept everything and of
  one that kept nothing.

Exits non-zero on any failure, so it can gate an install.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import types
from dataclasses import replace
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from gflights.client import (  # noqa: E402
    Client, Filters, QueryError, build_query, validate_dates, validate_route,
)
from gflights import http as gfhttp  # noqa: E402
from gflights.http import (  # noqa: E402
    HOST, FlightsHTTPError, RequestBudgetError, Transport, looks_blocked,
)
from gflights.client import _check_echo  # noqa: E402
from gflights import cli  # noqa: E402
from gflights.parse import (  # noqa: E402
    PayloadError, UnresolvableQuery, blocks, detected_currency, parsed_query,
    search_result,
)
from gflights.tfs import Query, Slice  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fixtures")

#: Two captures, because one route cannot exercise the parser.
#:
#: YYZ->YHZ is four nonstop, single-carrier, same-day itineraries. Everything
#: about connections is unreachable from it — layovers, per-leg carriers, an
#: arrival on a later date, and Google's elided-midnight [null, 49] hour. Tests
#: for those paths were written against it anyway and passed *vacuously*: they
#: iterated an empty list, so `all(...)` was true and nothing was checked.
#:
#: YYZ->KTM supplies exactly those shapes. Anything asserting about
#: connections, codeshares, overnights or midnight belongs there.
NONSTOP = "search_yyz_yhz.html"
CONNECTING = "search_yyz_ktm.html"

#: The tfs a real browser produced for YYZ->YHZ 2026-09-25 / YHZ->YYZ 2026-09-27,
#: one adult, economy. The encoder must reproduce this byte for byte; if it ever
#: stops doing so, every search silently returns the wrong trip.
BROWSER_TFS = (
    "CBwQAhoeEgoyMDI2LTA5LTI1agcIARIDWVlacgcIARIDWUhaGh4SCjIwMjYtMDktMjdqBwgB"
    "EgNZSFpyBwgBEgNZWVpAAUgBcAGCAQsI____________AZgBAQ"
)

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


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# [offline]
# ---------------------------------------------------------------------------


def test_encoder() -> None:
    print("\ntfs encoder")
    q = Query(slices=(
        Slice("YYZ", "YHZ", date(2026, 9, 25)),
        Slice("YHZ", "YYZ", date(2026, 9, 27)),
    ))
    check("offline", "reproduces the browser's own tfs byte for byte",
          q.tfs() == BROWSER_TFS, f"got {q.tfs()}")

    # The three fields that were originally mis-mapped, each verified live:
    # 8 is the passenger list, 9 is cabin, 19 is trip type. Getting any of them
    # wrong returns a real fare for the wrong query — 2 adults priced as
    # premium economy, or a lap infant priced as first class — with no error.
    def field(query: Query, tag: bytes) -> int:
        return query.encode().count(tag)

    trip = (Slice("YYZ", "YHZ", date(2026, 9, 25)),)
    check("offline", "a second adult repeats field 8 rather than incrementing it",
          field(Query(slices=trip, adults=2), b"\x40\x01") == 2
          and field(Query(slices=trip, adults=1), b"\x40\x01") == 1)
    check("offline", "a lap infant is a passenger, not a cabin upgrade",
          field(Query(slices=trip, adults=1, infants_on_lap=1), b"\x40\x04") == 1
          and field(Query(slices=trip, adults=1, infants_on_lap=1), b"\x48\x04") == 0,
          "field 9 = 4 would silently search first class")
    check("offline", "cabin sets field 9",
          field(Query(slices=trip, cabin="business"), b"\x48\x03") == 1
          and field(Query(slices=trip, cabin="economy"), b"\x48\x01") == 1)
    check("offline", "trip type sets field 19, not field 8",
          field(Query(slices=trip, trip_type=2), b"\x98\x01\x02") == 1
          and field(Query(slices=trip, trip_type=2), b"\x40\x02") == 0,
          "field 8 = 2 would silently add a child to the party")

    plain = Query(slices=(Slice("YYZ", "YHZ", date(2026, 9, 25)),)).encode()
    nonstop = Query(slices=(Slice("YYZ", "YHZ", date(2026, 9, 25), max_stops=0),)).encode()
    check("offline", "max_stops is emitted only when set",
          len(nonstop) > len(plain) and b"\x28\x00" in nonstop and b"\x28\x00" not in plain)

    check("offline", "a third slice encodes for multi-city",
          Query(slices=(
              Slice("YYZ", "YHZ", date(2026, 9, 25)),
              Slice("YHZ", "YUL", date(2026, 9, 27)),
              Slice("YUL", "YYZ", date(2026, 9, 29)),
          )).encode().count(b"\x1a") >= 3)


def test_parser() -> None:
    print("\npayload parser (real captured payload)")
    result = search_result(fixture(NONSTOP), "CAD")
    check("offline", "parses every itinerary in the capture",
          len(result.itineraries) == 4, f"got {len(result.itineraries)}")

    first = result.itineraries[0]
    # Exact values from the capture. Asserting on "is an int" would pass just
    # as happily with a duration in the price slot.
    check("offline", "price is the fare, not a neighbouring integer",
          first.price == 249, f"got {first.price}")
    check("offline", "carrier resolves to a name", first.carrier_name == "Flair Airlines")
    check("offline", "duration is minutes", first.duration_minutes == 130)
    check("offline", "stop count derives from leg count", first.stops == 0 and first.nonstop)
    check("offline", "legroom survives", first.legs[0].legroom == "29 in")
    check("offline", "aircraft survives",
          first.legs[0].aircraft == "Boeing 737MAX 8 Passenger")
    check("offline", "emissions are read as a signed percentage",
          first.co2_percent_vs_typical == -42, f"got {first.co2_percent_vs_typical}")

    # Google elides trailing zeros in [hour, minute] and writes a null hour for
    # midnight, so 13:00 arrives as [13] and 00:49 as [null, 49]. Getting this
    # wrong shifts a departure by hours without ever raising.
    check("offline", "an elided minute parses as :00",
          first.depart.endswith("T13:00:00"), f"got {first.depart}")
    # The null-hour half of that encoding is asserted in test_connecting_parser:
    # nothing in this capture arrives near midnight, and the assertion that used
    # to live here iterated an empty list and therefore checked nothing.

    context = result.price_context
    check("offline", "price verdict is read from the rendered banner",
          context.verdict == "typical", f"got {context.verdict}")
    check("offline", "typical/low/high band survives",
          (context.typical, context.low, context.high) == (228, 190, 345),
          f"got {(context.typical, context.low, context.high)}")
    check("offline", "price history parses as (date, price) points",
          len(context.history) == 6 and context.history[0] == ("2026-07-10", 206),
          f"got {context.history[:1]}")
    check("offline", "booking advice survives its nested markup",
          bool(context.advice) and "1–4 months" in context.advice,
          f"got {context.advice!r}")

    filters = result.filters
    check("offline", "route filter metadata parses",
          filters.price_min == 249 and len(filters.airlines) == 15,
          f"got {filters.price_min}, {len(filters.airlines)} airlines")
    check("offline", "connection airports parse as (code, city)",
          ("YUL", "Montreal") in filters.connection_airports)

    airports = {a.code: a for a in result.airports}
    check("offline", "airports carry coordinates",
          "YYZ" in airports and airports["YYZ"].latitude is not None)


def test_guards() -> None:
    print("\nshape guards (the silent-wrong-answer defence)")
    html = fixture(NONSTOP)
    payload = blocks(html)["ds:1"]

    # Simulate Google inserting one element at the front of the results block.
    # Every index after it shifts. Without guards this yields wrong-but-
    # plausible flights instead of an error.
    shifted = json.dumps([None] + payload, separators=(",", ":"))
    mutated = html.replace(json.dumps(payload, separators=(",", ":")), shifted)
    check("offline", "a shifted payload raises instead of reporting wrong flights",
          raises(PayloadError, search_result, mutated, "CAD"))

    # One broken row is survivable; a broken layout is not.
    one_bad = json.loads(json.dumps(payload))
    one_bad[3][0][0][0][9] = "not-a-duration"      # corrupt a single itinerary
    partial = html.replace(
        json.dumps(payload, separators=(",", ":")),
        json.dumps(one_bad, separators=(",", ":")),
    )
    result = search_result(partial, "CAD")
    check("offline", "one unreadable itinerary is dropped and counted, not fatal",
          len(result.itineraries) == 3 and result.unparsed == 1,
          f"got {len(result.itineraries)} kept, {result.unparsed} dropped")

    all_bad = json.loads(json.dumps(payload))
    for bucket in (2, 3):
        for row in all_bad[bucket][0]:
            row[0][9] = "not-a-duration"
    wrecked = html.replace(
        json.dumps(payload, separators=(",", ":")),
        json.dumps(all_bad, separators=(",", ":")),
    )
    check("offline", "a broadly unreadable payload raises rather than half-answering",
          raises(PayloadError, search_result, wrecked, "CAD"))

    check("offline", "a page with no results block raises",
          raises(PayloadError, search_result, "<html><body>nothing</body></html>", "CAD"))

    # Google ships the price-history block only for some routes and dates, and
    # sometimes omits it for a query that carried it minutes earlier. Absent
    # context must degrade to "no verdict", never to an exception or an error.
    without = json.loads(json.dumps(payload))
    without[5] = None
    degraded = html.replace(
        json.dumps(payload, separators=(",", ":")),
        json.dumps(without, separators=(",", ":")),
    )
    parsed = search_result(degraded, "CAD")
    check("offline", "a payload with no price context still parses its flights",
          len(parsed.itineraries) == 4 and parsed.price_context is None,
          f"got {len(parsed.itineraries)} itineraries, "
          f"context={parsed.price_context is not None}")

    # A wrong tfs returns a results block with nothing in it. That must not be
    # reported as "this route has no flights" — a real empty route still ships
    # its airline and airport metadata.
    hollow = json.loads(json.dumps(payload))
    hollow[2] = hollow[3] = None
    hollow[7] = None
    emptied = html.replace(
        json.dumps(payload, separators=(",", ":")),
        json.dumps(hollow, separators=(",", ":")),
    )
    check("offline", "a hollow payload raises rather than reporting no flights",
          raises(PayloadError, search_result, emptied, "CAD"))

    # Google restates the query it actually ran. Cross-checking against it is
    # what catches a mis-set field returning a real fare for a different trip.
    echo = parsed_query(html)
    check("offline", "the query echo parses",
          echo is not None and echo["adults"] == 1 and echo["children"] == 0,
          f"got {echo}")
    trip = Query(slices=(Slice("YYZ", "YHZ", date(2026, 9, 25)),), adults=1)
    check("offline", "a party-size mismatch is caught",
          raises(PayloadError, _check_echo, trip,
                 {"adults": 1, "children": 1, "infants_in_seat": 0,
                  "infants_on_lap": 0, "dates": ["2026-09-25"]}),
          "an extra child in the echo must not pass silently")
    check("offline", "a substituted date is caught",
          raises(PayloadError, _check_echo, trip,
                 {"adults": 1, "children": 0, "infants_in_seat": 0,
                  "infants_on_lap": 0, "dates": ["2026-10-21"]}),
          "Google rewrites dates it cannot parse rather than refusing them")
    check("offline", "a matching echo passes",
          not raises(PayloadError, _check_echo, trip,
                     {"adults": 1, "children": 0, "infants_in_seat": 0,
                      "infants_on_lap": 0, "dates": ["2026-09-25"]}))
    check("offline", "an absent echo is not treated as a mismatch",
          not raises(PayloadError, _check_echo, trip, None))

    check("offline", "an empty results block is a usage error, not a network one",
          raises(UnresolvableQuery, search_result,
                 html.replace(json.dumps(payload, separators=(",", ":")), "[]"),
                 "CAD"))

    check("offline", "a healthy page is never mistaken for a block",
          not looks_blocked(html))
    check("offline", "a consent interstitial is recognised as blocking",
          looks_blocked(fixture("blocked_consent.html")))



def test_validation() -> None:
    print("\nquery validation (refusing the false negatives)")
    today = date.today()
    # Each of these returns an empty payload from Google with no error, which
    # would be reported as "no flights available" — a confident wrong answer.
    check("offline", "a past departure is refused",
          raises(QueryError, validate_dates, today - timedelta(days=1), None))
    check("offline", "a departure beyond the booking horizon is refused",
          raises(QueryError, validate_dates, today + timedelta(days=400), None))
    check("offline", "a return before departure is refused",
          raises(QueryError, validate_dates, today + timedelta(days=10),
                 today + timedelta(days=5)))
    check("offline", "a valid trip is accepted",
          not raises(QueryError, validate_dates, today + timedelta(days=10),
                     today + timedelta(days=14)))
    check("offline", "a non-IATA origin is refused",
          raises(QueryError, validate_route, "TORONTO", "YHZ"))
    check("offline", "origin equal to destination is refused",
          raises(QueryError, validate_route, "YYZ", "YYZ"))
    check("offline", "a same-day return is allowed",
          not raises(QueryError, build_query, "YYZ", "YHZ",
                     today + timedelta(days=5), today + timedelta(days=5)))


def test_filters() -> None:
    print("\nresult filters")
    items = search_result(fixture(NONSTOP), "CAD").itineraries
    cheapest = min(i.price for i in items if i.price)

    at_cheapest = Filters(max_price=cheapest).apply(items)
    check("offline", "a price ceiling keeps only fares at or under it",
          len(at_cheapest) == 1
          and all(i.price <= cheapest for i in at_cheapest),
          f"kept {sorted(i.price for i in at_cheapest)} at a ceiling of {cheapest}")
    check("offline", "a price ceiling below every fare returns nothing, not everything",
          Filters(max_price=1).apply(items) == [])
    only_f8 = Filters(airlines=("F8",)).apply(items)
    check("offline", "an airline whitelist is honoured",
          len(only_f8) == 1 and all(i.carrier == "F8" for i in only_f8),
          f"kept {[i.carrier for i in only_f8]} of {[i.carrier for i in items]}")
    no_f8 = Filters(exclude_airlines=("F8",)).apply(items)
    check("offline", "an airline exclusion is honoured",
          len(no_f8) == 3 and all(i.carrier != "F8" for i in no_f8),
          f"kept {[i.carrier for i in no_f8]}")

    check("offline", "a legroom floor rejects legs that do not state it",
          Filters(min_legroom_inches=99).apply(items) == [])

    # Every assertion below states both what survives and what is dropped. An
    # "all survivors satisfy the filter" test alone passes just as happily when
    # the filter is a no-op that kept everything, or a bug that kept nothing.
    no_737 = Filters(avoid_aircraft=("737",)).apply(items)
    check("offline", "an aircraft exclusion drops the matching aircraft and keeps the rest",
          len(no_737) == 2
          and all("737" not in (l.aircraft or "") for i in no_737 for l in i.legs),
          f"kept {len(no_737)} of {len(items)}")

    # Departures in this capture are 13:00, 16:55, 17:40, 20:40.
    after_1700 = Filters(depart_after="17:00").apply(items)
    check("offline", "a departure window excludes earlier departures",
          len(after_1700) == 2 and len(after_1700) < len(items)
          and all(i.depart[11:16] >= "17:00" for i in after_1700),
          f"kept {len(after_1700)} of {len(items)} "
          f"({sorted(i.depart[11:16] for i in items)})")
    # The bound is inclusive, asserted on the exact minute rather than left to
    # be inferred: > versus >= is a one-character edit that silently drops the
    # flight the user named, and no other test here would notice.
    check("offline", "a departure bound includes a departure at exactly that minute",
          len(Filters(depart_after="16:55").apply(items)) == 3,
          f"got {len(Filters(depart_after='16:55').apply(items))} — 16:55 must "
          f"satisfy --depart-after 16:55")
    check("offline", "the same bound is inclusive from the other side",
          len(Filters(depart_before="16:55").apply(items)) == 2,
          f"got {len(Filters(depart_before='16:55').apply(items))}")

    check("offline", "no filters keeps everything",
          len(Filters().apply(items)) == len(items))


def test_codeshare_filters() -> None:
    """The filters that only a connecting, mixed-carrier capture can exercise.

    These ran against the nonstop fixture for a while and passed without
    testing anything: it holds no mixed-carrier itinerary, so the comprehension
    they asserted over was empty and `all([])` is true.
    """
    print("\nresult filters (connecting, mixed-carrier capture)")
    items = search_result(fixture(CONNECTING), "CAD", "one way").itineraries

    # Google labels a mixed-carrier itinerary "multi" and names one airline;
    # PD flies only the first leg of the 1394 fare. A whitelist that checks the
    # headline carrier alone silently drops exactly the trip the user asked for.
    porter = Filters(airlines=("PD",)).apply(items)
    check("offline", "a whitelist matches on any leg's carrier, not just the label",
          len(porter) == 1 and porter[0].price == 1394
          and porter[0].carrier != "PD"
          and "PD" in {l.carrier for l in porter[0].legs},
          f"matched {[(i.price, i.carrier) for i in porter]} — the 1394 fare is "
          f"labelled {items[0].carrier!r} and flown PD+QR+QR")

    qatar = Filters(airlines=("QR",)).apply(items)
    check("offline", "a whitelist keeps both the labelled and the unlabelled carrier",
          {i.price for i in qatar} == {1394, 1708},
          f"matched {sorted(i.price for i in qatar)}")

    without_qr = Filters(exclude_airlines=("QR",)).apply(items)
    check("offline", "an exclusion drops an itinerary flying the carrier on any leg",
          {i.price for i in without_qr} == {1536, 3205},
          f"kept {sorted(i.price for i in without_qr)}")

    # A layover only exists on a connecting itinerary, so this too was
    # unreachable from the nonstop capture.
    no_doha = Filters(avoid_layovers=("DOH",)).apply(items)
    check("offline", "a layover exclusion drops connections through that airport",
          {i.price for i in no_doha} == {1536, 3205},
          f"kept {sorted(i.price for i in no_doha)}")


def test_connecting_parser() -> None:
    """The parser against a connecting, overnight, mixed-carrier capture.

    Every shape here is unreachable from the nonstop fixture, and each one has
    a silent failure mode: a lost layover reads as a nonstop, a dropped leg
    carrier reads as a single-carrier trip, and a mis-parsed midnight arrival
    reads as a flight landing a whole day early.
    """
    print("\npayload parser (connecting/overnight capture)")
    result = search_result(fixture(CONNECTING), "CAD", "one way")
    check("offline", "parses every itinerary in the connecting capture",
          len(result.itineraries) == 4 and result.unparsed == 0,
          f"got {len(result.itineraries)}, {result.unparsed} unparsed")

    prices = [i.price for i in result.itineraries]
    check("offline", "fares are the captured ones, not neighbouring integers",
          prices == [1394, 1536, 1708, 3205], f"got {prices}")

    porter_qatar = result.itineraries[0]
    check("offline", "a connection reports its stops and layovers in order",
          porter_qatar.stops == 2 and porter_qatar.layovers == ["YUL", "DOH"]
          and not porter_qatar.nonstop,
          f"got {porter_qatar.stops} stops via {porter_qatar.layovers}")
    check("offline", "each leg keeps its own carrier and flight number",
          [(l.carrier, l.flight_number) for l in porter_qatar.legs]
          == [("PD", "129"), ("QR", "764"), ("QR", "648")],
          f"got {[(l.carrier, l.flight_number) for l in porter_qatar.legs]}")
    check("offline", "the headline carrier of a mixed itinerary is not a leg carrier",
          porter_qatar.carrier == "multi"
          and porter_qatar.carrier_name == "Porter Airlines",
          f"got {porter_qatar.carrier!r}/{porter_qatar.carrier_name!r}")
    check("offline", "elapsed time is the itinerary's own, not the sum of its legs",
          porter_qatar.duration_minutes == 1705,
          f"got {porter_qatar.duration_minutes}")

    # THE midnight case. Google elides a zero hour, so 00:05 ships as
    # [null, 5]. Read naively that is a missing value; the two plausible wrong
    # answers are 00:00 (minute dropped) and a whole missing arrival. Both look
    # entirely reasonable in output, which is why this is asserted exactly.
    overnight = result.itineraries[3]
    check("offline", "a null hour parses as midnight, not as a missing value",
          overnight.arrive == "2026-10-25T00:05:00",
          f"got {overnight.arrive!r} — the raw payload holds [null, 5]")
    check("offline", "the leg carrying the midnight arrival parses it too",
          overnight.legs[-1].arrive == "2026-10-25T00:05:00",
          f"got {overnight.legs[-1].arrive!r}")
    check("offline", "an overnight itinerary arrives on a later date than it departs",
          overnight.depart[:10] == "2026-10-23"
          and overnight.arrive[:10] == "2026-10-25",
          f"got {overnight.depart} -> {overnight.arrive}")
    check("offline", "an elided minute on a leg parses as :00",
          porter_qatar.legs[2].depart == "2026-10-25T01:05:00"
          and result.itineraries[2].legs[0].depart == "2026-10-23T21:00:00",
          f"got {result.itineraries[2].legs[0].depart!r}")

    check("offline", "route metadata parses on a long-haul route",
          result.filters is not None and len(result.filters.airlines) == 24
          and ("DOH", "Doha") in result.filters.connection_airports,
          f"got {result.filters and len(result.filters.airlines)} airlines")
    check("offline", "the connecting capture's query echo parses",
          parsed_query(fixture(CONNECTING)) == {
              "adults": 1, "children": 0, "infants_in_seat": 0,
              "infants_on_lap": 0, "dates": ["2026-10-23"],
          })


def test_arrival_window() -> None:
    """`--arrive-before` against a flight that lands after midnight.

    Comparing the clock alone ("00:05" <= "23:00") hands the user the red-eye
    they asked to avoid, and the fixture that had no overnight arrival could
    not show it.
    """
    print("\narrival windows (the red-eye trap)")
    overnight = [i for i in
                 search_result(fixture(CONNECTING), "CAD", "one way").itineraries
                 if i.arrive[11:16] < "06:00"]
    check("offline", "the capture really does contain an after-midnight arrival",
          len(overnight) == 1 and overnight[0].arrive == "2026-10-25T00:05:00",
          f"got {[i.arrive for i in overnight]}")
    # Semantics deliberately reversed after review: the bound is a time of day
    # at the destination, because resolving it against the departure date made
    # every long-haul fail ("no flights match" for any bound at all). The
    # red-eye intent is a separate flag, asserted below.
    check("offline", "a long-haul arrival is not excluded merely for landing later",
          len(Filters(arrive_before="23:59").apply(
              search_result(fixture(CONNECTING), "CAD", "one way").itineraries)) == 4,
          "resolving the bound against the departure date rejected all four")
    check("offline", "an after-midnight arrival passes a time-of-day bound",
          len(Filters(arrive_before="23:00").apply(overnight)) == 1,
          "00:05 is before 23:00; this is the documented reading")
    check("offline", "--arrive-same-day excludes it",
          Filters(arrive_before="23:00", arrive_same_day=True).apply(overnight) == [],
          "the red-eye intent is this flag, not the clock bound")
    check("offline", "--arrive-same-day keeps a same-day arrival",
          len(Filters(arrive_same_day=True).apply(
              search_result(fixture(NONSTOP), "CAD").itineraries)) == 4,
          "it must not simply reject everything")
    check("offline", "an arrival bound still keeps a same-day arrival before it",
          len(Filters(arrive_before="17:00").apply(
              search_result(fixture(NONSTOP), "CAD").itineraries)) == 1,
          "the bound must not simply reject everything")


def test_zero_valued_filters() -> None:
    """Zero is a value, not an absent filter.

    `if self.max_duration_minutes` and `if self.min_legroom_inches` both read
    zero as "no filter set", which turns an impossible constraint into a silent
    no-op — the tool answers the question it was not asked.
    """
    print("\nzero-valued filters")
    items = search_result(fixture(NONSTOP), "CAD").itineraries
    check("offline", "a zero duration ceiling is a real constraint, not no filter",
          Filters(max_duration_minutes=0).apply(items) == [],
          f"kept {len(Filters(max_duration_minutes=0).apply(items))} of "
          f"{len(items)} — zero minutes is impossible, not unset")
    check("offline", "a nonzero duration ceiling still filters",
          len(Filters(max_duration_minutes=128).apply(items)) == 2,
          f"got {len(Filters(max_duration_minutes=128).apply(items))}")
    # The two zero cases are deliberately asymmetric — 0 minutes is impossible,
    # 0 inches is no requirement — so the legroom half has to be asserted on a
    # leg that states *no* legroom. Every leg in both captures publishes a
    # figure, and against those a broken zero case is indistinguishable from a
    # working one: `int("31") < 0` is false either way. So one is synthesised.
    unstated = replace(items[0], legs=[replace(items[0].legs[0], legroom=None)])
    mixed = items + [unstated]
    check("offline", "a zero legroom floor excludes nothing rather than everything",
          len(Filters(min_legroom_inches=0).apply(mixed)) == len(mixed),
          "zero inches is no requirement at all, so even a leg that publishes "
          "no figure must survive it")
    check("offline", "a legroom floor above zero still rejects an unstated leg",
          Filters(min_legroom_inches=1).apply(mixed) == items,
          "assuming an unpublished figure is fine quietly recommends the "
          "cramped seat the user asked to avoid")


def test_malformed_rows() -> None:
    """One unreadable row is dropped and counted; it never takes down a search.

    Both shapes below came from shifting the payload by hand. Each used to
    escape the guard for a different reason: an int in the origin slot raised
    TypeError out of `len()` before any guard saw it, and a three-element list
    satisfied `len(origin) == 3` and sailed through as a valid IATA code.
    """
    print("\nmalformed itinerary rows")
    html = fixture(CONNECTING)
    payload = blocks(html)["ds:1"]
    serialised = json.dumps(payload, separators=(",", ":"))

    _ORIGIN_SLOT = 3          # itinerary[0][3], the group's origin code

    def with_broken_origin(value):
        """The capture plus one extra itinerary whose origin slot is `value`."""
        mutated = copy.deepcopy(payload)
        bad = copy.deepcopy(mutated[3][0][0])
        bad[0][_ORIGIN_SLOT] = value
        mutated[3][0].append(bad)
        return html.replace(serialised, json.dumps(mutated, separators=(",", ":")))

    for label, value in (("an int", 12345),
                         ("a three-element list", ["Y", "Y", "Z"])):
        try:
            result = search_result(with_broken_origin(value), "CAD", "one way")
        except Exception as e:  # noqa: BLE001 - the failure under test
            check("offline", f"{label} in the origin slot is survivable", False,
                  f"raised {type(e).__name__}: {e}")
            continue
        check("offline", f"{label} in the origin slot is dropped and counted",
              len(result.itineraries) == 4 and result.unparsed == 1,
              f"got {len(result.itineraries)} kept, {result.unparsed} counted — "
              f"a dropped row must be reported, never silently missing")


def test_currency_honesty() -> None:
    """A fare must not be labelled with a currency the page did not price in.

    Google accepts any `curr=` and quietly falls back to the point-of-sale
    currency when it does not recognise one. Stamping the requested code onto
    the fare then produces the worst shape of wrong answer available here: the
    number is real, and only its label is a lie.
    """
    print("\ncurrency the page actually priced in")
    html = fixture(CONNECTING)
    check("offline", "the currency is read back off the rendered page",
          detected_currency(html) == "CAD", f"got {detected_currency(html)}")
    check("offline", "a page with no priced markup yields no guess",
          detected_currency(fixture(NONSTOP)) is None,
          "precision over recall: an unsure page must return None, not a guess")
    check("offline", "an unrelated page yields no guess",
          detected_currency("<html><body>no prices here</body></html>") is None)

    # The page above prices in CAD. Asking for USD must not silently produce
    # USD-labelled Canadian fares. Either resolution is acceptable: relabel to
    # what the page actually says, or carry a warning the caller can surface.
    result = search_result(html, "USD", "one way")
    warned = bool(getattr(result, "currency_warning", None)
                  or getattr(result, "detected_currency", None) not in (None, "USD"))
    check("offline", "a currency the page did not price in is not reported as fact",
          all(i.currency == "CAD" for i in result.itineraries) or warned,
          "every fare is labelled USD on a page that priced in CAD, and nothing "
          "on the result says so")


def _parses(argv: list[str]) -> bool:
    """True if the CLI accepts these arguments. Never opens a socket."""
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            cli.build_parser().parse_args(argv)
        except SystemExit:
            return False
    return True


def test_cli_guards() -> None:
    """Flags whose bad values produced an answer rather than an error.

    Every rejection below replaces a *confident wrong answer*: a party of zero
    priced a trip for nobody, a stride of zero swept no dates and reported
    "nothing available" (exit 1 — a watch loop's signal to keep waiting).
    """
    print("\nCLI argument guards")
    trip = ["search", "YYZ", "YHZ", "--depart", "+30"]
    sweep = ["cheapest", "YYZ", "YHZ", "--depart", "+30"]

    check("offline", "a party of zero adults is refused",
          not _parses(trip + ["--adults", "0"]),
          "Google prices a zero-passenger search rather than refusing it")
    check("offline", "a negative passenger count is refused",
          not _parses(trip + ["--adults", "-1"])
          and not _parses(trip + ["--children", "-2"])
          and not _parses(trip + ["--infants-on-lap", "-1"]))
    check("offline", "one adult is still accepted", _parses(trip + ["--adults", "1"]))
    check("offline", "a party of zero adults is refused below the CLI too",
          raises(QueryError, build_query, "YYZ", "YHZ",
                 date.today() + timedelta(days=30), adults=0))
    check("offline", "a negative passenger count is refused below the CLI too",
          raises(QueryError, build_query, "YYZ", "YHZ",
                 date.today() + timedelta(days=30), adults=1, children=-1))

    check("offline", "a zero or negative sweep stride is refused",
          not _parses(sweep + ["--step", "0"])
          and not _parses(sweep + ["--step", "-1"]),
          "range(0, days, 0) raises and range(0, days, -1) is empty — reported "
          "as 'nothing available' rather than as the mistake it is")
    check("offline", "a stride of one is still accepted", _parses(sweep + ["--step", "1"]))
    check("offline", "a zero or negative sweep stride is refused below the CLI too",
          raises(QueryError, Client(Transport(max_requests=5)).sweep,
                 build_query("YYZ", "YHZ", date.today() + timedelta(days=30)),
                 5, step=0))

    check("offline", "a zero-day sweep is refused", not _parses(sweep + ["--days", "0"]))
    check("offline", "a zero result limit is refused",
          not _parses(trip + ["--limit", "0"]),
          "an empty list beside a count of 18 is not an answer")
    check("offline", "an impossible duration ceiling is refused",
          not _parses(trip + ["--max-duration", "0"]))
    check("offline", "a negative legroom floor is refused",
          not _parses(trip + ["--min-legroom", "-1"]))
    check("offline", "a zero legroom floor is accepted as 'no requirement'",
          _parses(trip + ["--min-legroom", "0"]))
    check("offline", "a currency that is not a 3-letter code is refused",
          not _parses(trip + ["--currency", "dollars"]))


def test_cheapest_defaults() -> None:
    """`cheapest` must work with nothing but a route and a date.

    Its default window is 14 days and the default request budget is 5, so the
    documented invocation refused itself before making a single request — the
    command was unusable exactly as the README shows it.
    """
    print("\ncheapest with its default flags")
    parsed = search_result(fixture(NONSTOP), "CAD")
    calls = []

    original = Client.search

    def stub(self, query):
        calls.append(query.slices[0].depart)
        return parsed

    Client.search = stub          # no sockets; the budget check is in sweep()
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["cheapest", "YYZ", "YHZ", "--depart", "+30"])
    finally:
        Client.search = original

    check("offline", "the documented invocation is not refused by its own budget",
          code == 0,
          f"exit {code} with no flags but a route and a date — the default "
          f"--days 14 must not exceed the default request ceiling")
    check("offline", "it really did sweep the whole default window",
          len(calls) == 14, f"searched {len(calls)} dates")


def test_budget() -> None:
    print("\nrequest budget")
    transport = Transport(max_requests=5)
    check("offline", "an over-budget plan is refused before any request",
          raises(RequestBudgetError, transport.plan, 30, "a 30-day sweep")
          and transport.requests_made == 0)
    check("offline", "a within-budget plan is allowed",
          not raises(RequestBudgetError, transport.plan, 5, "a 5-day sweep"))


@contextlib.contextmanager
def _patched(**attrs):
    """Swap module attributes on gflights.http, and always put them back."""
    saved = {name: getattr(gfhttp, name) for name in attrs}
    for name, value in attrs.items():
        setattr(gfhttp, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(gfhttp, name, value)


def _fake_shutil(curl_path):
    """A stand-in for `shutil` whose `which` only ever finds curl (or not)."""
    return types.SimpleNamespace(which=lambda name: curl_path if name == "curl" else None)


def _fake_subprocess(returncode, stdout=b"", stderr=b"", raises_oserror=False):
    """A stand-in for `subprocess` that answers one canned curl invocation."""
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        if raises_oserror:
            raise OSError(8, "Exec format error")
        return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                     stderr=stderr, args=argv)

    return types.SimpleNamespace(run=run), seen


def _fallback(url, params, curl, oserror=False, returncode=0, stdout=b"",
              stderr=b"", detail=False):
    """Drive Transport._get_fallback with curl present or absent, safely.

    Returns the URLs urllib saw, the page, and the exception it raised (if
    any) rather than propagating — a fallback that raises where it should fall
    through is exactly what the caller is asserting about.
    """
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
    """The transport fallbacks, as they behave on claude.ai rather than here.

    This skill ships as a .skill zip uploaded to a sandboxed container: no
    root, no pip, possibly no `requests`, possibly no `curl`, an egress proxy
    that may refuse the domain, and no writable HOME. Every one of those paths
    was written for an environment nobody had run the skill in.

    The invariant each test below defends is the same one: a *transport*
    failure must surface as an error, never as an empty result. Exit 1 is the
    code a watch loop reads as "keep waiting", so a sandbox with no egress
    that reports 1 polls a dead network forever.
    """
    print("\nsandbox degradation")
    url = f"https://{HOST}/travel/flights/search"
    params = {"tfs": "abc==", "hl": "en", "q": "a b"}

    # -- requests missing entirely ---------------------------------------
    with _patched(requests=None):
        transport = Transport(max_requests=5, throttle=0)
        check("offline", "no `requests` still builds a transport",
              transport._session is None,
              "a sandbox without requests must not fail at construction")

        took = []
        transport._get_fallback = lambda u, p: took.append(("fallback", u)) or "<html>"
        transport._get_requests = lambda u, p: took.append(("requests", u)) or "<html>"
        transport.get("/travel/flights/search", params)
        check("offline", "no `requests` routes the fetch to the fallback",
              [step for step, _ in took] == ["fallback"],
              f"took {took!r} — the requests path must not be reached at all")

    # -- curl missing too: urllib carries it ------------------------------
    #    Both of these call _get_fallback through a helper that survives it
    #    raising: a fallback that gives up instead of falling through is the
    #    failure being tested, and must be reported as a FAIL, not as a
    #    traceback that takes the rest of the suite with it.
    fetched, page, error = _fallback(url, params, curl=None)
    check("offline", "no curl either falls through to urllib",
          error is None and page == "<html>" and len(fetched) == 1,
          f"urllib saw {len(fetched)} fetches (expected 1), raised {error!r} — "
          f"with no curl, urllib is the only path left and must be taken")
    check("offline", "the urllib URL percent-encodes every parameter",
          bool(fetched) and "q=a+b" in fetched[0] and "tfs=abc%3D%3D" in fetched[0],
          f"got {fetched[0] if fetched else None!r} — an unescaped value can "
          f"smuggle a second parameter into the query")

    # -- curl present but unspawnable: same attempt continues to urllib ----
    fetched, page, error, invoked, transport = _fallback(
        url, params, curl="/usr/bin/curl", oserror=True, detail=True)
    check("offline", "a curl that cannot be spawned hands the same attempt to urllib",
          error is None and page == "<html>"
          and len(invoked) == 1 and len(fetched) == 1,
          f"curl invoked {len(invoked)}x, urllib {len(fetched)}x, raised "
          f"{error!r} — expected one of each and no error")
    check("offline", "and nothing was charged twice for it",
          transport.requests_made == 0,
          f"{transport.requests_made} requests charged by _get_fallback, which "
          f"does not meter — get() does")

    # -- curl reporting a settled failure ---------------------------------
    for code, label in ((6, "cannot resolve the host"), (7, "cannot reach the host"),
                        (5, "cannot resolve the proxy")):
        _, page, error = _fallback(url, params, curl="/usr/bin/curl",
                                   returncode=code, stdout=b"\n000",
                                   stderr=b"curl: fail")
        final = error is not None and error.startswith("FlightsHTTPError: ")
        message = error.split(": ", 1)[1] if final else error
        check("offline", f"curl exit {code} ({label}) is a one-line, final error",
              page is None and final and "\n" not in message
              and (HOST in message or "proxy" in message),
              f"got {error!r} — an agent sees this one line and nothing else, "
              f"so it has to name what could not be reached; a _Again here "
              f"would instead retry a settled failure three times")

    # -- an egress proxy is diagnosed, not retried -------------------------
    proxy_error = gfhttp.requests.exceptions.ProxyError("tunnel refused")
    check("offline", "a proxy failure is never retried",
          gfhttp._transient(proxy_error) is False,
          "a misconfigured proxy is a settled fact; retrying spends the "
          "budget on it three times and delays the real message")
    check("offline", "a DNS failure is never retried either",
          gfhttp._transient(gfhttp.requests.exceptions.ConnectionError(
              "NameResolutionError: Failed to resolve")) is False)
    check("offline", "but a timeout still is",
          gfhttp._transient(gfhttp.requests.exceptions.Timeout("slow")) is True,
          "a timeout is the one connection failure another attempt can fix")
    check("offline", "the proxy message names the variable to look at",
          "HTTPS_PROXY" in gfhttp._describe(proxy_error, 60)
          and HOST in gfhttp._describe(proxy_error, 60),
          f"got {gfhttp._describe(proxy_error, 60)!r}")

    # -- curl must inherit the same proxy/CA environment requests reads -----
    saved_env = {k: os.environ.get(k) for k in
                 ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY",
                  "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")}
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ["HTTPS_PROXY"] = "http://proxy.internal:3128"
        os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/corp.pem"
        args = gfhttp._curl_env_args()
        check("offline", "curl is handed the proxy and CA bundle requests would use",
              args[args.index("--proxy") + 1] == "http://proxy.internal:3128"
              and args[args.index("--cacert") + 1] == "/etc/ssl/corp.pem"
              if "--proxy" in args and "--cacert" in args else False,
              f"got {args!r} — curl reads neither HTTPS_PROXY (upper case) nor "
              f"REQUESTS_CA_BUNDLE on its own")
        check("offline", "and never a flag that would disable verification",
              not any(a in ("-k", "--insecure") for a in args), f"got {args!r}")

        os.environ["NO_PROXY"] = "google.com"
        bypassed = gfhttp._curl_env_args()
        check("offline", "NO_PROXY covering the host takes curl off the proxy",
              "--noproxy" in bypassed and "--proxy" not in bypassed,
              f"got {bypassed!r} — NO_PROXY=google.com exempts {HOST} by suffix, "
              f"so routing curl through the proxy anyway contradicts requests")

        os.environ["NO_PROXY"] = "example.com"
        unrelated = gfhttp._curl_env_args()
        check("offline", "an unrelated NO_PROXY entry leaves the proxy in place",
              "--proxy" in unrelated,
              f"got {unrelated!r} — dropping the proxy for every NO_PROXY value "
              f"would be the same bug in the other direction")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- no HOME, nothing written to disk ---------------------------------
    #    An empty scratch directory, not the repo: the tests above have already
    #    built transports here, so a cache file created by the first of them
    #    would be in any "before" snapshot taken now and the check would pass
    #    while proving nothing.
    saved_home = os.environ.get("HOME")
    saved_cwd = os.getcwd()
    scratch = tempfile.mkdtemp(prefix="gflights-sandbox-")
    error = None
    try:
        os.environ.pop("HOME", None)
        os.chdir(scratch)
        try:
            homeless = Transport(max_requests=5, throttle=0)
            homeless.plan(1, "a search")
            built = homeless.max_requests == 5
        except Exception as e:  # noqa: BLE001 - the outcome under test
            built, error = False, f"{type(e).__name__}: {e}"
        left_behind = sorted(os.listdir(scratch))
    finally:
        os.chdir(saved_cwd)
        if saved_home is not None:
            os.environ["HOME"] = saved_home
    check("offline", "a container with no HOME still builds a transport",
          built, f"raised {error!r} — claude.ai containers may have no HOME, "
          f"and this skill keeps no cache that would need one")
    check("offline", "and the skill writes nothing to the working directory",
          left_behind == [],
          f"left behind {left_behind} in an empty cwd — the sandbox filesystem "
          f"may be read-only, and CLAUDE.md forbids shipping anything cached")

    # -- the invariant all of the above exists to protect -------------------
    commands = {
        "search": ["search", "YYZ", "YHZ", "--depart", "+30", "--json"],
        "watch": ["watch", "YYZ", "YHZ", "--depart", "+30", "--under", "200",
                  "--json"],
        "price-check": ["price-check", "YYZ", "YHZ", "--depart", "+30", "--json"],
        "cheapest": ["cheapest", "YYZ", "YHZ", "--depart", "+30", "--days", "2",
                     "--max-requests", "2", "--json"],
        "route": ["route", "YYZ", "YHZ", "--depart", "+30", "--json"],
        "airports": ["airports", "Toronto", "--json"],
    }
    dead_network = FlightsHTTPError(
        f"cannot resolve {HOST} — no DNS, or no network egress."
    )

    def unreachable(self, path, params):
        raise dead_network

    original_get = Transport.get
    Transport.get = unreachable
    codes = {}
    try:
        for name, argv in commands.items():
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                codes[name] = (cli.main(argv), out.getvalue())
    finally:
        Transport.get = original_get

    check("offline", "every command exits 3 when the network is gone",
          len(codes) == len(commands)
          and all(code == 3 for code, _ in codes.values()),
          "got " + repr({n: c for n, (c, _) in codes.items()}) +
          " — exit 1 means 'nothing matched, keep waiting', so a sandbox with "
          "no egress would make a watch poll a dead network forever")
    def _error_line(text):
        """The `error` string a caller would read, or None if there isn't one."""
        try:
            payload = json.loads(text)
        except ValueError:
            return None  # not JSON at all — a traceback, or a half-written page
        return payload.get("error") if payload.get("ok") is False else None

    lines = {name: _error_line(text) for name, (_, text) in codes.items()}
    check("offline", "and none of them reports it as an empty result",
          len(codes) == len(commands) and all(lines.values()),
          "got " + repr(lines) + " — an ok:true payload with no itineraries is "
          "the false negative this repo forbids, and a payload that will not "
          "parse is no better")
    check("offline", "and each says so in one line that names the host",
          len(codes) == len(commands)
          and all(line and HOST in line and "\n" not in line
                  for line in lines.values()),
          "got " + repr(lines) + " — the user needs to know which domain to "
          "allow, on one line")
    noisy = sorted(name for name, (_, text) in codes.items()
                   if "Traceback" in text)
    check("offline", "and none of them prints a traceback",
          not noisy and len(codes) == len(commands),
          f"{noisy} put a traceback in the transcript — an agent reads that as "
          f"the skill being broken rather than the network being off")


# ---------------------------------------------------------------------------
# [network]
# ---------------------------------------------------------------------------


def test_live() -> None:
    print("\nlive calls to www.google.com")
    depart = date.today() + timedelta(days=30)
    client = Client(Transport(max_requests=4))
    try:
        result = client.search(build_query("YYZ", "YHZ", depart,
                                           depart + timedelta(days=3)))
    except FlightsHTTPError as e:
        check("network", "search YYZ->YHZ", False,
              f"www.google.com unreachable or blocking: {e}")
        return
    except PayloadError as e:
        check("network", "search YYZ->YHZ", False,
              f"www.google.com answered but the payload shape changed: {e}")
        return

    check("network", "a busy route returns itineraries",
          len(result.itineraries) > 0, f"got {len(result.itineraries)}")
    # Each of these is an all() over a list Google supplies, so the non-empty
    # test is part of the assertion: on an empty result they are true and mean
    # nothing.
    check("network", "every itinerary has a price and a carrier",
          bool(result.itineraries)
          and all(i.price and i.carrier for i in result.itineraries))
    live_prices = [i.price for i in result.itineraries if i.price]
    check("network", "prices are plausible fares, not stray integers",
          bool(live_prices) and all(20 < p < 100_000 for p in live_prices),
          "a value outside this range usually means an index shift")
    # Asserts the verdict WORD, not merely that a context object came back:
    # the exit code of `price-check` depends on the word, and the scrape is a
    # regex over rendered HTML that Google can reword at any time. A context
    # with a band but no word is the exact shape that silent drift produces.
    context = result.price_context
    check("network", "the live payload still parses a price verdict",
          context is not None and context.current is not None
          and (context.verdict is not None or context.verdict_code is None),
          f"band present but verdict word missing while the payload carries "
          f"code {context.verdict_code if context else None} — the banner "
          f"wording has probably changed")
    check("network", "route metadata still lists airlines",
          result.filters is not None and len(result.filters.airlines) > 0)

    # The three field mappings that were once wrong, each asserted against a
    # live fare. These are cheap to get wrong again — the encoding is
    # undocumented and every mistake returns a real price for a different
    # query, never an error.
    def cheapest(**kwargs) -> int | None:
        query = build_query("YYZ", "YHZ", depart, kwargs.pop("ret", None), **kwargs)
        found = Client(Transport(max_requests=2)).search(query)
        return min((i.price for i in found.itineraries if i.price), default=None)

    try:
        one_adult = cheapest(ret=depart + timedelta(days=3))
        two_adults = cheapest(ret=depart + timedelta(days=3), adults=2)
        business = cheapest(ret=depart + timedelta(days=3), cabin="business")
        one_way = cheapest()
    except (FlightsHTTPError, PayloadError) as e:
        check("network", "passenger/cabin/trip-type semantics", False,
              f"www.google.com: {e}")
        return

    check("network", "a second adult roughly doubles the fare",
          one_adult and two_adults and 1.7 * one_adult <= two_adults <= 2.3 * one_adult,
          f"1 adult {one_adult}, 2 adults {two_adults} — field 8 may be setting "
          f"cabin instead of party size")
    check("network", "business costs more than economy",
          business and one_adult and business > one_adult,
          f"economy {one_adult}, business {business} — field 9 may not be cabin")
    check("network", "one way is cheaper than the round-trip total",
          one_way and one_adult and one_way < one_adult,
          f"one way {one_way}, round trip {one_adult} — field 19 may not be "
          f"trip type, and a 'one way' search may silently still be a round trip")

    try:
        resolved, _ = Client(Transport(max_requests=2)).resolve("Toronto")
        toronto = {a.code for a in resolved}
        check("network", "a place name resolves to its airports",
              {"YYZ", "YTZ"} <= toronto, f"got {sorted(toronto)}")

        # Bali's airport is in Denpasar, so no substring of "Bali" matches it.
        # This is the case that proved resolution had to be structural: the old
        # implementation read the egress IP's own city and silently failed for
        # anywhere far from the datacenter.
        bali, _ = Client(Transport(max_requests=2)).resolve("Bali")
        check("network", "a place whose airport is named differently resolves",
              {a.code for a in bali} == {"DPS"}, f"got {[a.code for a in bali]}")
    except (FlightsHTTPError, PayloadError, QueryError) as e:
        check("network", "place-name resolution", False, f"www.google.com: {e}")

    # What varies here is `curr` as well as `gl`, and only the currency claim is
    # defensible: holding the currency constant and changing `gl` alone returns
    # the *same numbers* on this route, so "point of sale changes the fare" was
    # never what this checked. `gl` does still change which carriers and fare
    # feeds surface, but that is a result-set difference this one request cannot
    # separate from ordinary fare movement, so it is not asserted.
    usd = Client(Transport(max_requests=2), country="US", currency="USD")
    try:
        other = usd.search(build_query("YYZ", "YHZ", depart, depart + timedelta(days=3)))
    except FlightsHTTPError as e:
        check("network", "point of sale query", False, f"www.google.com: {e}")
        return

    # The fare itself has to move. Asserting only that the second search
    # returned *something* passes even when gl and curr are dropped on the
    # floor, which is precisely the failure this is here to notice: a USD label
    # on a Canadian-dollar number is a wrong answer that looks entirely right.
    in_usd = min((i.price for i in other.itineraries if i.price), default=None)
    # An identical number is the tell: Google accepts any `curr=` and falls back
    # to the point-of-sale currency when it does not honour one, so the fare
    # comes back in CAD wearing a USD label. The number looks right, which is
    # what makes it the worst answer this skill can give. USD is worth less than
    # CAD, so the honoured case is strictly the smaller number.
    check("network", "the requested currency changes the quoted number",
          in_usd is not None and one_adult is not None
          and 0.5 * one_adult < in_usd < 0.95 * one_adult,
          f"CAD {one_adult} at curr=CAD, {in_usd} at curr=USD — an equal or "
          f"larger number means curr was ignored and the USD label is a lie")
    check("network", "the fare is labelled with the currency it was priced in",
          bool(other.itineraries)
          and all(i.currency == "USD" for i in other.itineraries),
          f"got {sorted({i.currency for i in other.itineraries})}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="skip every test that opens a socket")
    args = parser.parse_args()

    print("google-flights self-check")
    test_encoder()
    test_parser()
    test_connecting_parser()
    test_guards()
    test_malformed_rows()
    test_validation()
    test_filters()
    test_codeshare_filters()
    test_arrival_window()
    test_zero_valued_filters()
    test_currency_honesty()
    test_cli_guards()
    test_cheapest_defaults()
    test_budget()
    test_degraded_sandbox()
    if not args.offline:
        test_live()
    else:
        print("\n[network] skipped (--offline)")

    print(f"\n{_passed} passed, {len(_failures)} failed")
    for failure in _failures:
        print(f"  FAIL {failure}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
