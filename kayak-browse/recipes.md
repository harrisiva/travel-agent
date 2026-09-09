# Recipes: worked kayak-browse workflows

Loaded on demand. `SKILL.md` covers the command surface and the rules that
change an answer; this file covers the longer shapes.

Every command below assumes `KAYAK_API_KEY` is set, or that
`kayak.py login --key-file <path>` has been run once. `$K` stands for
`python3 <path-to-this-skill>/scripts/kayak.py`.

---

## 1. Resolve a place before searching

Never hard-code a location id — catalogues change, and the same city has an
airport and several downtown counters.

```bash
$K places "Toronto Pearson" --json
```

```json
{
  "results": [
    { "name": "Toronto Pearson Intl",
      "full_name": "Toronto Pearson Intl Airport, Mississauga, Ontario, Canada (YYZ)",
      "place_type": "airport", "iata": "YYZ", "city_id": 30202 }
  ]
}
```

Use `iata` with `--pickup YYZ` (the default `--pickup-type airport`), or
`city_id` with `--pickup 30202 --pickup-type city` to include downtown
counters. Autocomplete returns at most six rows, so it is a resolver rather
than a catalogue: if the user's phrasing is vague, ask rather than guessing
between two airports.

---

## 2. The everyday search

```bash
$K cars --pickup YYZ --from 2026-12-20 --to 2026-12-23 --json
```

Read `meta.complete` before using the word "cheapest". Read
`meta.prices_are_mocked` before quoting any number at all.

With constraints — everything after `--to` is applied client-side:

```bash
$K cars --pickup YYZ --from 2026-12-20 --to 2026-12-23 \
        --type suv,van --min-passengers 5 --transmission automatic \
        --unlimited-mileage --free-cancellation --exclude-opaque --json
```

If that comes back empty, `meta.filtered_out` names the culprit:

```json
"filtered_out": { "--min-passengers 5": 41, "--unlimited-mileage": 12 }
```

Report that, not "nothing found" — it tells the user exactly which constraint
to relax.

---

## 3. Car camping

`--sleepable` is sugar for "an SUV or van with room for at least four", which
is the practical floor for sleeping in the back.

```bash
$K cars --pickup DEN --from 2027-07-04 --to 2027-07-11 \
        --sleepable --unlimited-mileage --min-bags 3 --json
```

Unlimited mileage matters more than the daily rate on a road trip: a 200 km/day
cap on a week-long loop is the difference between a cheap rental and a large
overage bill. When recommending, say the cap out loud rather than ranking on
price alone.

---

## 4. Is it cheaper if we shift the dates?

```bash
$K sweep --pickup YYZ --from 2026-12-18 --to 2026-12-27 --nights 3 \
         --max-requests 200 --json
```

Two-stage by design: every candidate day is scanned only as far as
`second-phase` — enough to rank days against each other — and then the best two
are re-polled to `complete`, because the day you actually recommend is the one
whose price has to be exact.

It prices the whole plan before issuing a single call and refuses with exit `2`
if it would exceed `--max-requests` or the hourly quota. `--max-requests`
defaults to **200**, and the budget is a *ceiling* — a single day costs up to
about a dozen requests, so a ten-day sweep is priced near 130 and a value like
`40` refuses the example above before it starts. When a sweep is refused,
narrow the range rather than raising the ceiling; the quota is 250 car requests
an hour and a refused sweep costs nothing.

`sweep` refuses outright in sandbox mode: ranking days by price is exactly the
operation mock prices cannot support.

---

## 5. One-way rentals

```bash
$K cars --pickup YVR --drop YYC --from 2027-05-02 --to 2027-05-06 --json
```

One-way fees are already inside the total. Say the drop-off city back to the
user when you report — a one-way quote that looks cheap is often a different
route than they meant.

---

## 6. Cheapest date to fly

```bash
$K when --origin JFK --destination LIS --from 2027-03 --to 2027-03 --json
```

`--from` and `--to` are **months**, `YYYY-MM`, and both are required; there is
no `--month`. The window is inclusive, and the API documents a maximum: **2
months** with the default `--aggregation day`, **12 months** with
`--aggregation month`. A wider range is refused with exit `2` rather than sent,
because the response cannot distinguish "too wide" from "no prices".

`--origin` and `--destination` take an IATA airport code or a numeric KAYAK
place id — `places <name> --for flights --json` gives you both. Use the place
id for a metro area (`--for flights` marks these `isMetro`); a three-letter
code is sent as a single airport.

```bash
# Every departure date across two months, non-stop, priced as a return trip
$K when --origin JFK --destination LIS --from 2027-03 --to 2027-04 \
        --round-trip --non-stop --json

# One row per month across a year, to find the cheap season first
$K when --origin JFK --destination LIS --from 2027-01 --to 2027-12 \
        --aggregation month --json
```

One call, no polling. Rows carry `predicted`: a predicted price is a model's
guess, not a fare anyone has been quoted. **Say "predicted" out loud** when you
quote one — the distinction is the whole value of the field. Predictions exist
only for airport-to-airport searches without `--non-stop`; pass `--non-stop`
and every row you get back is a cached price someone actually saw.

For a round trip the price belongs to a *pair* of dates: the CLI reports the
cheapest return leg for each departure date, so quote the `return` field
alongside `date` rather than the departure alone.

`meta.origin_name` and `meta.destination_name` are what the API resolved your
codes to. Check them before reporting — an input that resolved to a metro area
answers a different question than the one the user asked.

---

## 7. Hotels

```bash
$K places "Austin" --for hotels --json          # resolve first
$K hotels --destination kplace:31097 \
          --checkin 2027-04-14 --checkout 2027-04-16 --json
```

`--destination` takes an **EntityKey**, not a bare id: a type prefix and an
identifier joined by a colon (`kplace:31097` for a city or region,
`khotel:2589314` for one property). Read `entity_key` from the `--json` output
of `places … --for hotels`; when a row has none, compose it from the id the row
does carry — `kplace:<place_id>`, or `khotel:<hotel_id>`. `--id <EntityKey>`
narrows to a single property and makes `--destination` unnecessary.

**Hotels complete in phases, exactly as cars do.** The endpoint answers with
whatever providers have reported so far and sets `isComplete` on the response;
`onlyIfComplete` makes the server reply `202 Accepted` until it is finished, and
the CLI polls that for you. So `complete` *is* part of the answer here:

```bash
# take what has arrived — fast, and honest about being partial
$K hotels --destination kplace:31097 --checkin 2027-04-14 --checkout 2027-04-16 --json

# insist on a finished search: exit 5, with the partial rows, if it times out
$K hotels --destination kplace:31097 --checkin 2027-04-14 --checkout 2027-04-16 \
          --complete --max-poll-seconds 90 --json
```

`meta.complete` and `meta.partial` carry the verdict. **The honesty rule for
cars applies unchanged to hotels: the words "cheapest", "best price" and
"lowest" require `complete: true`.** The lowest rate in a half-finished search
is the lowest rate *so far*, and the provider that undercuts it is exactly the
one still reporting. Without `--complete`, say "cheapest of what had come back";
with it, exit `5` means you did not get an answer worth that word — report the
rows as partial and offer to re-run with a longer `--max-poll-seconds`.

Rates are for **one room, two adults** (`meta.occupancy`). There is no
party-size flag yet, so never present one as a per-person or a family price.

The search asks the API for the largest page it allows (250 hotels), for the
same reason `cars` asks for 500: every filter and comparison you make happens
after the fetch, and the server's own default page is 25 popularity-sorted
rows. `--limit` then trims what you see, as it does on `cars` — `meta.matched`
says how many came back and `meta.truncated` whether anything was cut.

---

## 8. Watching for a price drop (Claude Code only)

There is no scheduler on claude.ai, so a watch belongs in Claude Code. Give the
loop room — `--max-poll-seconds 90` — and leave a real interval between runs:
the quota is hourly, and a tight loop spends it in minutes.

```bash
#!/bin/bash
# Re-check a rental once an hour; alert when a total drops below a threshold.
K="python3 /path/to/kayak-browse/scripts/kayak.py"
THRESHOLD=350

while true; do
  out=$($K cars --pickup YYZ --from 2026-12-20 --to 2026-12-23 \
               --unlimited-mileage --max-poll-seconds 90 --json)
  code=$?
  case $code in
    0) best=$(printf '%s' "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
rows=[r for r in d['results'] if r['price']['total'] is not None]
print(min(r['price']['total'] for r in rows) if rows else '')")
       [ -n "$best" ] && awk \"BEGIN{exit !($best < $THRESHOLD)}\" \
         && echo \"under threshold: $best\" ;;
    1) echo "nothing matched those filters" ;;
    4) echo "key rejected or expired — stopping"; exit 4 ;;   # never keep polling
    5) echo "search timed out; treating as no reading this cycle" ;;
    *) echo "error $code" ;;
  esac
  sleep 3600
done
```

The `case` is the point. Exit `4` must break the loop — an expired key that is
retried hourly looks exactly like "no cars available" forever. Exit `1` is the
only code that means "carry on waiting".

---

## 9. Exercising the nothing-available path

The sandbox honours a header that forces an empty result, which is how to test
the exit-`1` branch without waiting for genuinely sold-out dates:

```
sandbox-api-empty: true
```

The offline suite covers this with `fixtures/cars_empty.json`; the header is
here for anyone testing against a live sandbox key.

---

## 10. `userTrackId`, if you hand-write a loop

The CLI mints one UUID per process invocation and shares it across every
thread, which is what KAYAK asks for: one id per end user per session. You
never need to touch it.

It matters only if someone bypasses the CLI and calls the API directly. A
constant value (`test`), or an id rotated per request, reads as id churn and
gets the key rate-limited and then blocked. One id per user per session, a
fresh one next session.

---

## 11. When the answer has to come from raw data

`--json` emits a trimmed projection: a full car response is megabytes of logo
URLs and tracking tokens, all of which would land in the context window.

```bash
$K cars --pickup YYZ --from 2026-12-20 --to 2026-12-23 --full --json
```

`--full` replaces the projection with the API's own `results[]` rows — each one
a `CarSearchResult` with its `bookingOptions` exactly as KAYAK sent them — and
puts the `agencies`, `providers` and `carLocations` maps on `meta.maps`, so you
can do the join yourself if you need a field the projection dropped.

The envelope is unchanged: `results` is still a list and `count` still matches
its length, so a consumer that reads the trimmed output reads this too.
`--limit` applies here as well, and it applies to `--json` generally — trimming
the array, not merely the printed table — with `meta.kept` reporting how many
matched and `meta.truncated` saying whether anything was cut.

Prefer adding a missing field to `model.py` over living in `--full`: everything
downstream of the projection (filters, sorting, the table) reads the flat
`Offer`, so a field that only exists in `--full` is a field no filter can use.
