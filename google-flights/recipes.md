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

A weekend sweep starts on a Friday and steps a week at a time, so every date
it tries is a Friday. `+N` counts from today, so work out N for the next
Friday first:

```bash
# Days until the next Friday (date +%u: Monday=1 … Sunday=7, Friday=5).
fri=$(( (5 - $(date +%u) + 7) % 7 )); [ "$fri" -eq 0 ] && fri=7
python3 flights.py cheapest YYZ YHZ --depart +$fri --return +$((fri + 2)) \
    --days 28 --step 7 --json
```

That visits `ceil(28/7)` = 4 Fridays, so it costs 4 requests. `cheapest` sets
its own budget: one request per visited date plus 3 spare for retries, capped
at 40. Here that is 7, so the command runs as written, even though 7 is above
the 5-request default the other commands use. You only need `--max-requests`
here to lower it.

`--return` sets the *trip length*, and the sweep keeps it: a Friday departure
with a Sunday return stays a two-night trip on every date tried. It is **not**
a fixed return date. Every row in the JSON carries the `return` it actually
priced, so quote that rather than the date you passed in.

Then price the winner properly, using the `depart` and `return` from `best` in
the sweep's JSON:

```bash
python3 flights.py search YYZ YHZ --depart <best.depart> --return <best.return> --json
```

**Report it as:** the cheapest date, what it saves against the date they asked
for, and the actual flight. Users ask for a specific weekend and then happily
move it for $80.

---

## 2. "Is this a good price, or should I wait?"

```bash
python3 flights.py price-check YYZ YHZ --depart +15 --return +17 --json
```

Returns Google's own verdict plus the evidence behind it (abridged from a real
response):

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

The block above is abridged from a real response for YYZ–YHZ, departing
2026-09-25 and returning 2026-09-27, observed on 2026-09-08. The `query` and
`schema_version`/`ok` keys and most of `history` are omitted. Do not expect to
reproduce those numbers: the bundled capture of the *same* query earlier that
day shows 231 as 249, because fares move intraday — which is the whole reason
nothing here is cached. `history` is the last ~60 days of the cheapest fare for
the route (61 daily points in that run), so quote the range (`low`–`high`)
rather than the series.

The command exits `0` only when `verdict` is present. Exit `1` means there is
no verdict to give — either `price_context` is `null` (and you get
`cheapest_now` and `route_fare_range` instead. `route_fare_range` holds the
ends of Google's price slider for this search: `min` is at or just below
today's cheapest fare for these dates, and `max` is not a fare, so do not
quote it). Or the band came back
without its verdict word. The fare is still real; there is just nothing to
judge it against, and no flag or retry changes that.

---

## 3. Watch a fare and act when it drops

`watch` is one check, designed for cron. Exit `0` means the threshold was met.

```bash
python3 flights.py watch YYZ YHZ --depart 2026-12-20 --return 2027-01-03 \
    --under 600 --json
```

A daily job. It has to act on exit `0`: a job that only appends to a log
never tells anyone the fare dropped. `&&` runs the alert only when the watch
exits `0`. Replace `<your alert command>` with whatever reaches the user, such
as `mail` or a webhook `curl`:

```cron
0 9 * * * cd /path/to/skill/scripts && /usr/bin/python3 flights.py watch YYZ YHZ --depart 2026-12-20 --return 2027-01-03 --under 600 --json >> "$HOME/fare-watch.log" 2>&1 && <your alert command>
```

Crontab has no line continuation, so the whole entry must stay on **one
line**. For anything longer, put the command in a wrapper script and call
that. Give `python3` as an absolute path (check it with `command -v python3`),
because cron runs with a minimal `PATH`.

Use absolute `YYYY-MM-DD` dates in a scheduled job. `+N` is relative to the
day the job runs, so the trip being watched would slide forward a day with
every run.

**The exit codes are the whole point.** Only `1` means "not yet, keep waiting".
`3` means the check failed — Google blocked the request, or the network is
down — and says nothing about the fare. A loop that treats `3` as `1` will
happily poll a captcha page until the flight departs. `2` means the job is
misconfigured and every future run will fail the same way (a departure date
that has now slipped into the past, a filter the route rules out); stop the job
rather than retrying it.

**A threshold below today's fare is the normal case.** `--under` is never
checked against the current price: it exits `1` until the fare falls to it.
To pick a realistic threshold, run `price-check` first and aim near its `low`
(the bottom of the last ~60 days). `route`'s `price_min` is only at or just
below today's cheapest fare for those dates. Its `price_max` is where Google's slider stops,
not a fare, so it is not a range to aim inside. A filter the search proves
impossible (a carrier that does not fly the route, a `--max-duration` under
the quickest flight) is still refused with exit `2`.

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

If the search's own published bounds *prove* the filter can never match, it
exits `2` with the reason instead. That covers a `--max-price` under this
search's cheapest fare, a `--max-duration` under its quickest flight, a
carrier list where nothing asked for flies the route, and a departure window
that excludes itself. So on `search`, `--max-price 200` when the cheapest fare
for those dates is $231 is a **refusal**, not an empty result; pass it on as
one. `cheapest` and `watch` do not refuse on the price floor, because a later
date or a later day can go below it.

---

## 5. "What can I filter on, and where would I connect?"

Before searching, or when the user wants options rather than a single answer:

```bash
python3 flights.py route YYZ YHZ --depart +30 --json
```

Gives the airports Google will route you through, the price and duration
bounds for that date, and Google's airline and alliance **filter chips**.

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
`ceil(N/S)` of them, not N, and never more than 40. Every command except
`cheapest` defaults to a 5-request ceiling. `cheapest` defaults to one per
visited date plus 3 for retries. `--max-requests` overrides either default, up
to a hard maximum of 40.

---

## Notes for Claude on the web

The skill needs outbound HTTPS to `www.google.com`. If the environment has no
network egress, the first command fails with exit `3` and a message naming the
host — that is the environment, not a bug in the skill, and no retry or
different flag will fix it. Say so plainly and offer to work from prices the
user supplies instead.

`requests` is optional: the client falls back to `curl` and then to `urllib`,
and all three paths share the same budget and throttle. The self-check's
`[offline]` group exercises all three, in four cases: `requests` absent, curl
absent, curl present but unspawnable, and curl behind a proxy. So a missing `requests` is
not a reason to spend a turn installing anything before trying a command.

Each search downloads ~2.5 MB of HTML and parses it in memory. Measured on
2026-09-08, one search took about 1.4 seconds; add roughly 0.7 s of deliberate
throttling between consecutive requests, so a 20-day sweep is on the order of
40 seconds rather than 20. Prefer `--step 2` over a 40-day `--days` when the
user just wants a general shape.
