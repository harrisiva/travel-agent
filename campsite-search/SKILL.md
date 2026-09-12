---
name: campsite-search
description: Check campsite and cabin availability at Canadian national, provincial and conservation-authority campgrounds — Parks Canada, Ontario Parks, BC Parks, Grand River CA, Manitoba, Nova Scotia, New Brunswick, Newfoundland and Yukon. Use when the user asks whether a campground has sites open, wants to find any opening across a range of dates or a whole park group, wants cabins/yurts/oTENTiks/huts rather than tent sites, wants sites matching criteria (electric, pull-through, private, barrier-free), asks when reservations open for a park, wants one specific site's calendar, asks about park alerts or closures, or wants a watch set up for a sold-out campground.
---

# Campsite search (Canada)

Queries the Camis5 reservation API shared by Parks Canada, Ontario Parks, GRCA,
BC Parks and several provinces. Read-only — it never books anything.

Worked end-to-end examples live in **`recipes.md`**, next to this file. Read it
when you need a full workflow rather than a single command.

## Setup and invocation

**Tool location:** the `scripts/` directory next to this SKILL.md. Everything is
bundled — there is nothing to install from a repo.

```bash
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
```

Preferred invocation — the launcher works from **any** working directory, so
prefer it and never `cd` just to run a query:

```bash
python3 <path-to-this-skill>/scripts/campsites.py <command> ...
```

Equivalent alternative, only if you are already in `scripts/`:

```bash
cd <path-to-this-skill>/scripts && python3 -m campsites <command> ...
```

Both share identical output and exit codes. Use `py` instead of `python3` on
Windows. Do **not** use bare `python` — it does not exist on most macOS/Linux
systems. `ModuleNotFoundError: campsites` means you used the `-m` form from the
wrong directory; use the launcher path instead. `ModuleNotFoundError: requests`
means run the install line above.

**Verify the install** with the bundled self-check — worth running once after
dropping this skill into a new environment:

```bash
python3 <path-to-this-skill>/scripts/test_availability.py --offline   # ~0.3s, no network
python3 <path-to-this-skill>/scripts/test_availability.py             # full live check
```

It prints a PASS/FAIL summary and exits non-zero on failure.

Optional but recommended: `python3 -m pip install -q truststore` verifies TLS
against the OS trust store. Without it, `reservation.pc.gc.ca` may fail
verification on machines with an old `certifi`. On a TLS failure the tool tries,
in order: `curl` if it is on `PATH`, then (only when there is no curl) the
system CA bundle at the usual distribution paths — skipped if
`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` is already set. If all of those fail that
one provider exits `3`; the other eight still work. Certificate checking is
never disabled.

## Providers

Nine supported tenants:

`pc` Parks Canada · `ontario` Ontario Parks · `grca` Grand River CA · `bc` BC
Parks · `manitoba` · `novascotia` · `newfoundland` · `yukon` · `newbrunswick`
(`reservations.parcsnbparks.ca`)

Alberta, Saskatchewan and PEI run ReserveAmerica/Aspira behind a Queue-it
waiting room; Quebec (Sépaq) is in-house and serves a CAPTCHA challenge (HTTP
403); NWT has no reservation API at its host — all **not supported**. Say so
plainly rather than guessing or substituting a nearby park.
`campsites.py providers` prints the live list and is authoritative.

## Choosing a command

| The user wants | Command |
|---|---|
| "Is site X free on these exact dates?" | `search` |
| "Anything open in September?" / "any weekend in the fall?" | `sweep` |
| "Anything in Algonquin?" — a park **group**, or a whole province | `find` |
| "When is site 285 free?" | `site` |
| "When do bookings open?" | `window` |
| "How far ahead can I book?" | `horizon` |
| "What can I filter on?" | `attrs` |
| "What cabins/yurts/oTENTiks exist here?" | `stays` |
| "Any alerts or closures at Killarney?" | `alerts` — see below |

**`alerts <provider>` takes no park.** It returns every alert on the tenant
(27 on Ontario Parks). To answer for one park, resolve its id with
`parks <provider> --search <name>`, run `alerts <provider> --json`, and keep
only alerts whose `affectedResourceLocationIds` contains that id; the title and
body are in the English entry of `localizedValues` (`messageTitle`,
`htmlMessageText` — HTML, strip it), and `transactionDates` says when each
applies. The text output is one truncated JSON line per alert and is not worth
reading. Alerts are never cached. Recipe 8 has the one-liner.

### Defaults, and what `--limit` means per command

`--equipment tent` picks the **first** equipment whose name contains "tent"
(an exact name wins if there is one) — on some tenants that is not a plain
tent, so check `equipment` when results look wrong. `--party 2`,
`--nights 2`, `--booking-category 0` (Campsite), `--max-requests 200`.

`--limit` (default 40) truncates different things:

| Command | Text output | `--json` |
|---|---|---|
| `search` | sites listed (and partial sites) | not applied — full lists |
| `sweep` | check-in dates shown | sites per date (`site_count` is still the full count) |
| `find` | parks shown | sites per park (`opening_count` is still full) |
| `alerts` | alerts printed | not applied |
| others | ignored | ignored |

## `--end` means something DIFFERENT in `search` and in `sweep`

This has produced wrong answers to users. Learn the two meanings:

- **`search --start A --end B`** — `B` is the **departure** date. The night of
  `B` is *not* part of the stay. `A=08-22 B=08-24` is 2 nights: 22nd and 23rd.
- **`sweep`/`find --start A --end B`** — `B` is the **last night considered,
  inclusive**. Check-in dates can land *on* `B`, and the checkout then falls
  *after* `B`. `--start 08-21 --end 08-23 --nights 1` really does report a
  check-in on `08-23` departing `08-24`.
- **`site --start A --end B`** — also inclusive: the calendar has one line per
  date from `A` through `B`.

`--help` on each command states which meaning applies. The `sweep` header reads
`N-night stays checking in A to B inclusive`.

**Rule: read the `check_in` field on every row. Never infer a date from the
header, and never assume a row means the day before it.** A row reading
`2026-08-23` means the 23rd is open — it says nothing about the 22nd.

**Before telling a user something is available, confirm it with a per-site
calendar**, which is unambiguous — one line per date with an explicit status:

```
python3 <path>/scripts/campsites.py site ontario "Killarney Provincial Park" \
    --site 16 --start 2026-08-21 --end 2026-08-25 --party 1
```

Treat `site` as the authoritative check. `search`/`sweep` find candidates;
`site` confirms them.

## A default search only sees ONE booking category

`--booking-category` defaults to `0` (Campsite). Everything in another category
is **invisible** to that query, and the tool reports the same empty result it
would for a genuinely full park. Two ways this misleads:

- **Roofed accommodation** (cabins, yurts, oTENTiks, huts) searched under the
  default category comes back as `INVALID`, which looks exactly like "nothing
  available".
- **Backcountry, paddle-in and hike-in sites** live in their own categories and
  simply do not appear. An empty `dates: []` under the default category means
  *"nothing in THIS category"*, **not** *"the park is full"*. The Massasauga and
  Kawartha Highlands both look sold out on a default sweep while holding dozens
  of open paddle-in sites.

So: **never report a park as sold out until you have checked the categories it
actually has.** Run `stays <provider> "<park>"` — it lists every booking
category on the tenant *and* counts what exists in that park.

**The IDs are tenant-specific.** On Ontario, `paddling`=4, `hiking`=5,
`backcountry`=11, `roofed`=2 — but on Parks Canada `roofed`=1, `backcountry`=5,
and `4` is the West Coast Trail. Never pass a raw number you saw elsewhere. Use
the portable aliases, which resolve per tenant:

`campsite` · `roofed` · `cabin` · `group` · `backcountry` · `seasonal` ·
`dayuse` · `paddling` · `hiking`

```
python3 <path>/scripts/campsites.py stays ontario "The Massasauga Provincial Park"
python3 <path>/scripts/campsites.py sweep ontario "The Massasauga Provincial Park" \
    --start 2026-08-22 --end 2026-08-24 --nights 2 --booking-category paddling
```

`--type` filters by what the thing *is*: `oTENTik`, `Ôasis`, `Yurt`,
`MicrOcube`, `Teepee`, `Prospector Tent`, `Rustic Cabin`, `Cabin`, `Cottage`,
`Soft-sided Shelter`, `Equipped Camping`, `Backcountry Cabin`,
`Backcountry Yurt`, `Backcountry Zone Shelter`. Run `stays` for the live list —
it differs per tenant and is authoritative.

Roofed units are few and book out first; "nothing available" is a common and
correct answer there. Confirm with `search -v` (see below).

## Use `find` when the user names a region, not one campground

"Algonquin" is 17 separate locations on Ontario Parks; `search` and `sweep` only
ever look at one. `find ontario "Algonquin"` sweeps all 17 and ranks them by how
much is open. An empty pattern (`find grca ""`) sweeps every park on the tenant.
It refuses up front if the plan exceeds `--max-requests`, so start narrow.
Input that is wrong for every park (span, dates, equipment, category) exits `2`
before any park is tried, and so does a run where every park was skipped. A
park is skipped only when its availability data fails to parse (API drift) — a
park with nothing open, or no maps, is an ordinary empty result.

Parks that `find` reports with no openings are often backcountry, day-use or
group-only locations needing a different `--booking-category` — not failures.

`sweep` is usually the better answer for anything vague about dates. It costs
the same as `search` — the API returns per-night status for the whole range in
one request per map — so never loop `search` over dates.

## Workflow

1. **Resolve the park.** Names are long and repetitive. An ambiguous match
   prints the candidates and exits `2` — show them and ask, or pick the obvious
   one if intent is clear.
   ```
   python3 <path>/scripts/campsites.py parks ontario --search algonquin
   ```

2. **Look up equipment names — they differ per provider** ("Single Tent" on
   Ontario, "Small Tent" on Parks Canada, "1 Tent" on BC). Guessing produces an
   all-`INVALID` result that looks like "sold out".
   ```
   python3 <path>/scripts/campsites.py equipment pc
   ```

3. **Search or sweep**, minding the `--end` rule above.
   ```
   python3 <path>/scripts/campsites.py sweep ontario "Pinery Provincial Park" \
       --start 2026-09-01 --end 2026-09-30 --nights 2 --weekends
   ```
   Flags: `--nights`, `--weekends`, `--weekday fri --weekday sat`, `--map AREA`,
   `--party N`, `--booking-category roofed`, `--attr NAME=VALUE`, `--type`,
   `--limit N`, `--json`, `-v`.

4. **Filter on what the user actually cares about.** Run `attrs` first to see
   real values, then filter. Common ones: `"Service Type=Electric"`,
   `"Electrical Service=15/30 Amps"`, `"Pull-through=Yes"`, `"Privacy=Good"`,
   `"Barrier Free=Yes"`, `"Site Shade=Full Shade"`, `"Dogs Allowed=Yes"`.

5. **When nothing is available**, do not report "sold out" until you know why.
   The status histogram is a **`search -v` feature only** — `sweep -v` and
   `find -v` do not print one, so re-run the same query as `search` to diagnose:
   ```
   python3 <path>/scripts/campsites.py search pc "Banff - Two Jack Lakeside" \
       --booking-category roofed --start 2026-08-20 --end 2026-08-21 -v
   # status counts: {'INVALID': 62, 'UNAVAILABLE': 10, 'CLOSED': 2}
   ```
   - `NOT_OPERATING` — the park is closed that season. Say that, not "sold out".
   - `UNAVAILABLE` — genuinely booked. If the count matches the number of units
     in that category (10 oTENTiks above), they really are all taken.
   - `INVALID` — the equipment, party size or booking category doesn't fit.
     Re-check steps 2 and the category section.
   - `NON_RESERVABLE` — first-come-first-served; tell the user to just show up.

   Then offer `--include-partial`, a wider `sweep`, or another booking category.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | found something |
| `1` | query worked, nothing available |
| `2` | usage/lookup error — bad or ambiguous park, unknown provider, unknown equipment or category, span over 367 days, request ceiling, `find` with every park skipped |
| `3` | network or API error |

Stable across `--json`. In a watch loop, treat **only** `1` as "keep waiting";
`2` and `3` mean the loop is broken and must stop.

## JSON shapes

Every `--json` output is one **object**, never a bare array:
`{"schema_version": 1, "ok": true, ...}`. On exit `2`/`3` it is instead
`{"schema_version": 1, "ok": false, "exit_code": N, "error": "..."}` — check
`ok` before reading anything else. Top-level keys on success:

| Command | Keys |
|---|---|
| `search` | `provider park start end nights equipment party_size requests available[] partial[] counts{}` — `partial[]` rows add `free_nights` and `nights[]` (status per night) |
| `sweep` | `provider park window{start,end} nights requests dates[]` — each `{check_in check_out weekday site_count sites[]}` |
| `find` | `provider pattern parks_searched requests nights window parks[] skipped[]` — each park `{park park_id opening_count check_in_dates[] sites[]}`; `skipped[]` is `{park reason}` |
| `site` | `site area description max_capacity attributes calendar[] free_nights[]` — `calendar[]` is `{date status}` |
| `horizon` | `park probed_maps from last_date_with_availability last_date_in_booking_window days_out booking_windows[]` — or just `park probed_maps error` (still `ok: true`, exit 1) when no map answered |
| `window` | `park windows[]` — each `{schedule start end go_live}` |
| `parks` | `provider parks[]` of `{id name}` |
| `attrs` | `park facets{name: {value: count}}` |
| `stays` | `provider park booking_categories[] stay_types[]` |
| `equipment` | `equipment[] booking_categories[]` |
| `alerts` | `alerts[]` — raw API objects, see above |
| `providers` | `camis[] unsupported{}` |
| `cache-clear` | `cleared dir` (`dir` is null when the cache is memory-only) |

Where present and non-null, `park` is the resolved full name, not what was
typed (`stays` with no park gives `null`; `site` has no `park` key).

## Watching a sold-out campground

`search`/`sweep` exit `0` on a hit, so they drop into a polling loop directly.
Keep the interval at **15 minutes or more**. Never set up anything that races
other users at a launch-day opening — `window` shows when that is. Template in
`recipes.md`.

In `window`/`horizon` output, `opens: —` means the season has **no go-live
date on file** — not "open now" and not "never". Report it as unknown; if the
season's dates are current, bookings are usually already open.

## Hard limits

- Date spans over **367 days** are rejected. The API returns an empty body
  rather than an error, so the tool refuses instead of reporting a false "none".
- A request ceiling of 200 stops runaway sweeps; raise with `--max-requests`.
  It counts **availability** requests (plus `window`/`horizon` schedule and
  `alerts` calls) — not reference-data fetches. On a cold cache `find` fetches
  the park list and one map list per matching park (plus the equipment and
  booking-category lists) *before* it can plan, so a refused `find` has still
  made those calls.
- Reference data is cached on disk: parks, maps and site metadata 7d;
  equipment, booking categories and attributes 30d; stay types 7d.
  Availability, date schedules (`window`, go-live dates) and alerts are **never**
  cached. Use `--no-cache` if a park's site list looks stale, `cache-clear` to
  reset.

## Reporting back

Lead with the answer: how many sites, which areas, which dates — quoting the
`check_in` dates verbatim. Quote site numbers and their descriptions — they
carry real detail ("Backs onto Gorge", "Small entrance, low trees", "Very
private site. Sloped laneway") and Ontario flags site `Conditions` like
`Poison Ivy`. For a sweep, lead with the check-in dates that have the most
options rather than dumping every opening.

Say which booking site to finish on (`campsites.py providers` lists the hosts),
and state plainly that this tool cannot book — the user has to complete it there.
