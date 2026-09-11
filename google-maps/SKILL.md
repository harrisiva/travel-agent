---
name: google-maps
description: >-
  Find places near a location — restaurants, cafés, bars, shops, pharmacies,
  museums — with live open/closed status, the full seven-day opening schedule,
  and travel time by car, foot, bike or transit including live traffic. Use
  whenever the user asks what's open near somewhere now or at a given time,
  what's within walking distance, how long it takes to get between two places,
  a place's hours for the week, or an address or phone number for a place by
  name. Read-only — it never books, reserves or contacts anywhere.
---

# Google Maps — places, hours, travel times

A read-only CLI over three of Google Maps' own endpoints. **No API key, no
login, no dependencies** — standard library only, so there is nothing to
install.

- `scripts/gmaps.py` — the CLI. Runnable by absolute path from any directory.
- `scripts/gmaps/` — the package behind it.
- `NOTES.md` — the endpoint reverse-engineering, if you need to extend this.
- `recipes.md` — worked multi-step workflows. Load when you need one.

## On claude.ai: check network access first

**None of this section has been tested inside the claude.ai sandbox.** It is
derived from Anthropic's documentation, and this skill was verified in Claude
Code, where it needs no setup. Offer the fix below as the likely cause, and if
it does not work, say plainly that the guidance is untested rather than
insisting the user retry it or reporting the skill as broken.

**The sandbox blocks every domain except package managers by default, so every
command here fails until that is changed.** If the first call reports that it
could not reach `www.google.com`, tell the user:

> Settings → Capabilities → Code execution → enable network access for all
> domains (or allowlist `www.google.com`), **then start a new chat** — the
> setting does not apply to the conversation it was changed in.

This needs a paid plan with code execution. It cannot work at all on the Claude
API skills surface, which has no network access by design. In Claude Code it
works with no setup.

## Choosing a command

| The user is asking | Command |
|---|---|
| "Somewhere open near me, not far" | **`nearby`** |
| "What ramen places are around here?" | `search` |
| "How long to get from A to B?" | `travel` |
| "What are X's hours this week?" | `hours` |
| "Where exactly is X?" | `geocode` |

**`nearby` is the default choice.** It finds places, drops the closed ones,
attaches a travel time to each and ranks by it. `search` is the same lookup
without routing — one fewer request when distance genuinely does not matter.

```bash
cd <path-to-this-skill>/scripts

# What's open near here, by travel time
python3 gmaps.py nearby --near "Kensington Market, Toronto" --query ramen

# Walking distance — a completely different answer in a city
python3 gmaps.py nearby --near "The Drake Hotel, Toronto" --mode walk --within 15

# Open at a specific time, in the PLACE's local clock
python3 gmaps.py nearby --near "43.6532,-79.3832" --open-at "Sat 23:00"

# One place's whole week
python3 gmaps.py hours "Richmond Station" --near Toronto

# Compare a shortlist — one routing request, plus traffic on the nearest few
python3 gmaps.py travel --from "Union Station, Toronto" --to "Pearson Airport" --to "Casa Loma"
```

**Flag placement:** `--json`, `--hl` and `--gl` are accepted both before and
after the subcommand, so either form works. Add `--json` when you are going to
parse the output; leave it off for an exploratory look — measured across four
queries the human table is about a third the size of the default JSON and a
sixth the size of `--full`, and it is closer to what you will report anyway.

**`--sort` differs by command.** `search` takes `relevance` (default),
`rating` or `distance`. `nearby` adds `travel` and uses it by default — pass
`--sort relevance` explicitly to get Google's own order. `travel` has no
`--sort` at all and reports destinations in the order given.

**`--open-at` also accepts an ISO datetime** (`'2026-10-04 21:00'`), but only
its weekday and time are used — Google publishes a weekly pattern, not dated
exceptions, so a public holiday is not reflected.

## `--mode` changes the answer, not just the number

`drive` (default), `walk`, `bike`, `transit`. **Always say which mode a duration
is in.** A bare "8 minutes" for a place 600 m away is the correct *driving*
answer and a useless one — it is a seven-minute walk. In a city centre, `walk`
is usually what the user means.

`transit` additionally returns real departure and arrival clock times.

## Things that will otherwise catch you out

**`search` and `nearby` have opposite defaults.** `search` returns everything
unless you pass `--open-now`. `nearby` filters to currently-open places and
needs `--include-closed` to stop. Same query, different result set. Their
`--limit` defaults differ too: `search` 20, `nearby` 8.

**Unknown hours are not closed.** Places that publish none come back as
`status: "unknown"`, are excluded from open-only results, and are counted in
`hours_unknown`. Never report one as closed. If a list is short and
`hours_unknown` is non-zero, say so: "4 open, plus 3 whose hours aren't listed".

The payload keeps five more counters apart on purpose, and they mean different
things: `week_unknown` (Google publishes no weekly schedule), `hours_failed`
(the weekly lookup errored — retry, do not report it as a fact about the place)
and `network_errors` (any enrichment failure), `travel_unknown` (places that could not be routed) and `skipped` (places deliberately not looked up because the request ceiling was reached — NOT a fact about the place). A non-zero `hours_failed` means
part of the answer is missing, not that those places have no hours.

**Permanently closed listings are dropped before you see them.** Google still
returns them as search results; offering one as "nearby" is worse than no
answer, so anything Google marks `business_status: "closed"` is dropped
(`"unknown"` survives — absence of a marker is not a closure). Pass
`--include-permanently-closed` only when the user is asking *whether* a place
has shut down.

**`--open-at` supersedes the open-now filter, it does not intersect with it.**
Asking `nearby` at 10pm on a Tuesday "what is open Monday 9am" is one question,
not two, so the live filter is dropped when `--open-at` is given. You do **not**
need `--include-closed` alongside it. The payload's `open_now` reports the
filter actually applied, so it reads `false` whenever `--open-at` is set.

**Two ceilings, and they cap different things.** `--max-requests` (default 5)
caps *search result pages*; `--max-place-requests` (default 25) is ONE ceiling
covering every per-place lookup, hours and routing together. An oversized plan
is refused up front with exit 2 and the number it would have taken, rather than
quietly answering a smaller question. Narrow with `--limit`, or raise the
ceiling deliberately. `--concurrency` (default 12) changes speed, not cost —
lower it if Google starts rate-limiting.

**The full week is opt-in and costs one request per place.** `--with-hours`
attaches it, `--open-at` implies it, and `hours` gets it for one place.
`--open-at` is interpreted in **the place's own local time** — the clock Google
publishes hours in, and what a traveller means by "open when we land at 9pm".
Verified: live `status` is computed in the place's timezone too, so a Tokyo
search run from Toronto reports Tokyo's morning correctly.

**Travel times include live traffic only when `traffic_aware` is true.** On
some routes Google's traffic figure describes a different journey from the
route itself — an island airport reached by ferry, say — and the tool discards
it rather than report a number that is twenty minutes short. Check the flag
before saying a time accounts for traffic. Traffic is *current*, not predicted.

**Only the nearest five destinations get a traffic figure.** Routing prices
every destination in one cheap request that carries no traffic block, then
re-fetches just the five nearest individually. Everything past those five keeps
its free-flow number with `traffic_aware: false`. The top-level `traffic_aware`
is an `all()` over the results, so it goes `false` as soon as there are more
than five — read the per-place flag, not the summary one.

**`hours` returns Google's best match, which may not be the place you named.**
It does not fail on a name that does not exist: `hours "Zzqqx Nonexistent
Bistro" --near Toronto` comes back as "Muse Bistro + Bar" with a full schedule
and **exit 0**. The exit code will not tell you — the human output prints
`(nearest match for '…' — check this is the right place)` when the name you
asked for is not a substring of the name it found, and `matched_query` carries
the name you asked for in both output modes. Under `--json` there is no warning
line, so compare `matched_query` against `name` yourself, and name the place the
hours actually belong to when you report.

**There is no price data.** Google does not return a price level on these
endpoints. Do not infer `$$` from rating, category or anything else — say it
is not available.

**Nothing is cached, ever.** Live status changes by the hour and the claude.ai
container is per-session, so a cache would never be warm. The tool writes
nothing to disk at all — verified: the package opens no file for writing.

**JSON is trimmed by default.** The payload carries what a decision needs; add
`--full` for coordinates, ids, URLs and structured hour spans. `place_id`, `ftid`, `lat`/`lng`, `timezone`, `categories`, `city_region`,
`website`, `maps_url`, `hours_week_source` and `hours_week_days` are
`--full`-only **in `search` and `nearby`**. `travel` is not trimmed and has
no `--full` flag at all — it always returns `lat`/`lng`, `straight_km`,
`route_via`, `free_flow_minutes`, `traffic_range` and, for transit,
`transit`. Passing `--full` to `travel` is a usage error.

An oversized result sheds whole rows and sets `shown` / `matched` /
`output_truncated` rather than emitting JSON your output cap would corrupt into
something unparseable. Verified: a 60-result `--with-hours --full` search came
back as about 8 rows with `matched: 60`, `output_truncated: true`. The row
count drifts with live data; the flags are what to read.

**`--json` shapes are not uniform.** `search`, `nearby` and `travel` return an
envelope — `{"from", "mode", "count", "results": [...]}` — while `geocode`
returns a flat `{"query", "name", "lat", "lng"}` and `hours` returns a **single
place object** with no envelope and no `results` array. `hours` also ignores
trimming: it always emits the full record, and has no `--full` flag. Indexing
`["results"][0]` on `hours` or `geocode` is the mistake to avoid.

## What this cannot do, and what to do instead

Say the limit plainly rather than improvising an answer around it.

| Not supported | Workaround |
|---|---|
| **Search along a route** ("where can we stop for lunch on the way to X?") | No `along` command exists. Interpolate waypoints between the two ends and search around each — the worked, tested method is **recipe 5** in `recipes.md`. It approximates the route as a straight line, so sanity-check the towns it names against the real road. |
| **Automatic radius widening** when nothing is open | No `--expand`. Retry manually at increasing `--span` (3000 → 10000 → 25000) and say which radius the answer came from. A small `--span` is a common cause of a false "nothing open nearby". |
| **Price level** (`$`–`$$$$`) | Not returned by these endpoints at all. Say it is unavailable; never infer it from rating or category. |
| **Sorting `travel` results** | `travel` returns destinations in `--to` order, never ranked. Sort them yourself before reporting "the closest is…". |
| **Dated departure or arrival times** | Routing is always "leave now". Transit clock times are for the current departure; there is no "at 8am tomorrow". |
| **Reviews, photos, popular times / how busy** | Not wired. Review counts were observed once and then stopped being served, so they are deliberately not used. |
| **Accessibility and reservation links** | Not surfaced. Point the user at the place's `website` or `phone` (`website` needs `--full`). |
| **Dated holiday hours** | Google publishes a weekly pattern; `--open-at` matches on weekday. A public holiday may differ, so for a date that matters, say the schedule is the normal week. |
| **Anything transactional** — booking, reserving, ordering | Out of scope by design and will not be added. Give the phone number or website. |

Two honest caveats about the tool itself:

- **The claude.ai egress guidance is untested inside that sandbox** — flagged
  again here because it is the one instruction in this file nobody has run. If
  the fix does not work, say so rather than insisting.
- **These are Google's internal endpoints, not a supported API.** They can
  change shape without warning. A shape change exits `3`, never `1`, so it will
  not be mistaken for "nothing matched" — but if results look wrong, suspect
  that before blaming the query.

## Exit codes

Identical with and without `--json`:

| Code | Meaning |
|---|---|
| `0` | found something |
| `1` | query worked, nothing matched |
| `2` | usage or lookup error |
| `3` | network, API or parse error |
| `130` | interrupted (Ctrl-C) |

Verified in place: a nonsense query exits `1` with a normal envelope and
`count: 0`; `--min-rating 9`, an unknown flag, `--max-requests 0` and an
over-ceiling plan all exit `2`; an unreachable host exits `3`. `hours` exits
`3` when the weekly lookup itself failed, so a caller retries instead of
recording "this place publishes nothing".

Only `1` means "keep waiting" — a network outage, a blocked sandbox and a
Google schema change all return `3`, never `1`. Under `--json` a failure writes
`{"ok": false, "exit_code": N, "error": ...}` to stdout as well as stderr.
Exit `1` is not a failure: `search` and `nearby` keep the normal envelope
with `count: 0`. **`hours` is the exception** — a name matching nothing
writes `{"ok": false, "exit_code": 1, "error": ...}` instead of an envelope.

## Reporting back

Lead with the answer. *"Three ramen places are open near Kensington Market; the
closest is Machida Shoten, a 4-minute walk, open until 10pm."* Then the list.

Quote what a person decides on: name, **time with its mode**, closing time,
rating, and the one-line editorial description when there is one. Skip
coordinates, feature ids and URLs unless asked. Never paste the JSON.

Say "open until 10pm" rather than "status: open" — the closing time is usually
the deciding fact. When hours matter across timezones, name the clock: "closes
10pm Toronto time".
