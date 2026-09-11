# Recipes

Worked end-to-end workflows. `SKILL.md` is the interface contract; this file is
loaded when a task needs one of these shapes.

All examples assume:

```bash
cd <path-to-this-skill>/scripts
```

---

## 1. "Find me the cheapest weekend in the next month to fly to Halifax"

Two commands. Sweep first, then look at the winner in detail.

```bash
# One request per departure date; --step 2 halves that for a wide window.
python3 flights.py cheapest YYZ YHZ --depart +7 --return +9 --days 21 --step 1 --json
```

That is 21 requests, well over the 5-request default ceiling. `cheapest` sets
its own budget — one request per date it will actually visit, plus three spare
for retries, capped at 40 —
so this runs as written. You only need `--max-requests` here to lower it.

`--return` sets the *trip length*, and the sweep preserves it: a Friday
departure with a Sunday return stays a two-night trip on every date tried. It
is **not** a fixed return date — every row in the JSON carries the `return` it
actually priced, so quote that rather than the date you passed in.

Then price the winner properly:

```bash
python3 flights.py search YYZ YHZ --depart 2026-09-26 --return 2026-09-28 --json
```

**Report it as:** the cheapest date, what it saves against the date they asked
for, and the actual flight. Users ask for a specific weekend and then happily
move it for $80.

---

## 2. "Is this a good price, or should I wait?"

```bash
python3 flights.py price-check YYZ YHZ --depart 2026-09-25 --return 2026-09-27 --json
```

Returns Google's own verdict plus the evidence behind it:

```json
{
  "cheapest_now": 231,
  "price_context": {
    "verdict": "typical",
    "typical": 228, "low": 190, "high": 345,
    "history": [{"date": "2026-07-10", "price": 206}, ...],
    "advice": "earlier, about 1–4 months before takeoff"
  }
}
```

How to read it out loud:

- `verdict: "low"` → tell them to book; this is the value the command exists for.
- `verdict: "typical"` and the fare near `typical` → no urgency either way.
- fare well above `high` → something specific is going on with those dates; try
  `cheapest` on nearby dates before advising them to wait.

The block above is a real response, observed on 2026-09-08. Do not expect to
reproduce those numbers: the bundled capture of the *same* query earlier that
day shows 231 as 249, because fares move intraday — which is the whole reason
nothing here is cached. `history` is the last ~60 days of the cheapest fare for
the route (61 daily points in that run), so quote the range (`low`–`high`)
rather than the series.

The command exits `0` only when `verdict` is present. Exit `1` means there is
no verdict to give — either `price_context` is `null` (and you get
`cheapest_now` and `route_fare_range` instead — Google's route-wide filter
bounds, not the range for your dates) or the band came back
without its verdict word. The fare is still real; there is just nothing to
judge it against, and no flag or retry changes that.

---

## 3. Watch a fare and act when it drops

`watch` is one check, designed for cron. Exit `0` means the threshold was met.

```bash
python3 flights.py watch YYZ YHZ --depart 2026-12-20 --return 2027-01-03 \
    --under 600 --json
```

A daily job:

```cron
0 9 * * * cd /path/to/skill/scripts && python3 flights.py watch YYZ YHZ \
    --depart 2026-12-20 --return 2027-01-03 --under 600 --json \
    >> ~/fare-watch.log 2>&1
```

**The exit codes are the whole point.** Only `1` means "not yet, keep waiting".
`3` means the check failed — Google blocked the request, or the network is
down — and says nothing about the fare. A loop that treats `3` as `1` will
happily poll a captcha page until the flight departs. `2` means the job is
misconfigured and every future run will fail the same way (a departure date
that has now slipped into the past, a filter the route rules out); stop the job
rather than retrying it.

**An impossible threshold is refused, not polled.** `--under` is checked
against the route's cheapest fare before the job can settle into a loop, so a
threshold below anything the route has ever sold exits `2` naming the real
range rather than exiting `1` forever. You can still run `route` first if you
want to pick a threshold that is merely ambitious rather than impossible —
`price_min`–`price_max` is the range to aim inside.

Twice a day is plenty. Fares do not move minute to minute, and hammering
Google is how you start getting `3`s.

---

## 4. "I want to avoid connecting through Toronto / I want the cleanest flight"

Everything except stop count is filtered locally, so these cost no extra
requests and can be combined freely:

```bash
python3 flights.py search YVR YHZ --depart +30 --return +37 \
    --avoid-layovers YYZ --max-duration 480 --depart-after 08:00 --json
```

```bash
# At least 20% below the typical emissions for the route.
python3 flights.py search YYZ LHR --depart +45 --max-co2 -20 --json
```

Both of those were run on 2026-09-08 and returned matches (3 of 5, and 2 of 11
respectively), so they are live examples rather than illustrations.

If a filter set merely happens to match nothing, the command exits `1` and the
human output says how many flights existed before filtering — that difference
is the useful thing to tell the user ("18 flights on this route, but none
leaving after 08:00 that also avoid YYZ").

If the route's own published bounds *prove* the filter can never match — a
`--max-price` under the cheapest fare, a `--max-duration` under the quickest
flight, a carrier list where nothing asked for flies the route, a departure window that excludes
itself — it exits `2` with the reason instead. So `--max-price 200` on a route
whose floor is $231 is a **refusal**, not an empty result; pass it on as one.

---

## 5. "Who flies this route?"

Before searching, or when the user wants options rather than a single answer:

```bash
python3 flights.py route YYZ YHZ --depart +30 --json
```

Gives the airports Google will route you through, the real price and duration
bounds, and Google's airline and alliance **filter chips**.

**The airline list is not "who flies this route".** Run live on YYZ–YHZ it
returned 15 carriers — Asiana, Austrian, EgyptAir and XiamenAir among them —
which are codeshare and interline artifacts, not airlines a traveller would
board between Toronto and Halifax. Use `route` for the bounds and the
connection menu, and to check a `--airlines` filter is worth applying before
you apply it. To tell the user who actually flies the route, read the carriers
off `search` results.

---

## 6. Comparing two origins, or two trip lengths

There is no built-in comparison command; run the searches and compare the JSON.

```bash
for origin in YYZ YHM; do
  python3 flights.py search $origin YHZ --depart +30 --return +34 \
      --json --limit 1 > /tmp/$origin.json
done
```

Keep the total request count in mind: each search is one request, and a
`cheapest` sweep is one per date it visits — `--days N --step S` visits
`ceil(N/S)` of them, not N. A single search is capped at 5 requests; `cheapest`
budgets one per visited date, up to a hard maximum of 40.

---

## Notes for Claude on the web

The skill needs outbound HTTPS to `www.google.com`. If the environment has no
network egress, the first command fails with exit `3` and a message naming the
host — that is the environment, not a bug in the skill, and no retry or
different flag will fix it. Say so plainly and offer to work from prices the
user supplies instead.

`requests` is optional: the client falls back to `curl` and then to `urllib`,
and all three paths share the same budget and throttle. The self-check's
`[offline]` group exercises all three — `requests` absent, curl absent, curl
present but unspawnable, and curl behind a proxy — so a missing `requests` is
not a reason to spend a turn installing anything before trying a command.

Each search downloads ~2.5 MB of HTML and parses it in memory. Measured on
2026-09-08, one search took about 1.4 seconds; add roughly 0.7 s of deliberate
throttling between consecutive requests, so a 20-day sweep is on the order of
40 seconds rather than 20. Prefer `--step 2` over a 40-day `--days` when the
user just wants a general shape.
