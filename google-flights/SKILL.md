---
name: google-flights
description: >-
  Search live flight prices, schedules and fare history on Google Flights
  through a bundled Python CLI — routes, dates, airlines, stops, emissions and
  legroom, plus Google's own "is this a good price?" verdict and 60 days of
  price history. Use whenever the user asks what flights exist between two
  places, how much a trip costs, which departure date in a range is cheapest,
  whether a fare is high or low right now or worth waiting on, who flies a
  route, which flight is cleanest or has the most legroom, or wants a watch set
  up for a fare to drop. Read-only — it never books, holds or pays for anything.
---

# Google Flights

A dependency-light Python client for Google Flights' public search page. It
answers the questions a traveller actually asks — "what does this cost", "is
that a good price", "which day is cheapest" — not just "list flights".

**Tool location:** the `scripts/` directory next to this SKILL.md. Commands
below assume you `cd` there first. Longer workflows and scheduled-job templates
are in `recipes.md`, also next to this file.

## Setup

There is nothing to set up. No API key, no login, no configuration.

`requests` is used when present but is **not required** — the client falls back
to `curl` and then to Python's own `urllib`, so it is built to work in a
locked-down sandbox where pip is unavailable. (The self-check's `[offline]` group exercises both: `requests` absent, curl
absent, curl present but unspawnable, and curl behind a proxy — with mocked
transports rather than live calls.) Install it only if you want its retry
handling:

```bash
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
```

## Platform support

**Claude Code / desktop:** works, subject to Google's rate limits.

**claude.ai (web):** needs **Code execution and file creation** *and* **network
egress** enabled in Settings → Capabilities, with the domain allowlist set to
**All domains**. "Package managers only" is not enough — `www.google.com` is not
a package manager. On Team and Enterprise plans this is an **admin** setting in
Organization settings → Capabilities and an individual member cannot turn it
on. Start a new conversation after changing it. (Which plans ship with network
egress already on has not been verified from here — if a command exits 3
naming `www.google.com`, check the setting rather than assuming the plan.)

**Not supported:** the API `code_execution` tool, which has no outbound network
at all. The skill can never work there, and that is expected.

**Either way,** requests leave from a datacenter IP, and Google answers those
more suspiciously than a home connection — sometimes with a consent page, a
captcha or a rate limit instead of results. The tool detects that and exits 3
saying so. Retrying does not fix it; waiting does.

## Choosing a command

| The user asks | Command |
|---|---|
| "What's the code for Toronto? Does London have several?" | `airports` |
| "What flies YYZ to Halifax on the 25th?" | `search` |
| "Is $249 a good price, or should I wait?" | `price-check` |
| "Which day in the next two weeks is cheapest?" | `cheapest` |
| "What can I filter on for this route?" | `route` |
| "Tell me when it drops under $220" | `watch` |

Every command takes `--json`. **Always pass `--json` when you are going to
parse the output** — the human format is for showing the user.

```bash
cd <path-to-this-skill>/scripts
python3 flights.py search YYZ YHZ --depart 2026-09-25 --return 2026-09-27 --json
```

`flights.py` works when invoked by absolute path from any working directory.

## The six commands

Five of them (`search`, `price-check`, `cheapest`, `route`, `watch`) take the
same trip arguments: `ORIGIN DESTINATION --depart DATE`, plus optional
`--return DATE` (omit for one-way), `--adults`, `--children`,
`--infants-in-seat`, `--infants-on-lap`, `--cabin
{economy,premium-economy,business,first}` (default `economy`), and
`--max-stops {0,1,2}`. `airports` takes a place name instead.

Dates are `YYYY-MM-DD` **or** a relative `+N` meaning N days from today, which
is usually what you want when the user says "in three weeks". `+N` is capped at
`+10000` so a typo fails as a usage error rather than a crash.

Party sizes are bounded: `--adults` 1–9, `--children`, `--infants-in-seat` and
`--infants-on-lap` 0–8, at most **9 travellers in total**, and no more lap
infants than adults. All of those are refused with exit 2 before any request.

**`route` lists Google's own filter chips, not the airlines you can actually
fly.** Measured on YYZ–YHZ, the chip list came back with 15 carriers including
Asiana, Austrian, EgyptAir and XiamenAir — codeshare and interline artifacts on
a route flown in practice by a handful of airlines. Use it for the fare and
duration bounds and the connection menu; to answer "who really flies this?",
read the carriers off `search` results instead.

| Command | Adds |
|---|---|
| `airports` | takes a place name instead of a trip: `airports Toronto`. Resolves structurally — it reads the airports Google actually routes to, so `Bali` resolves to `DPS` even though the airport is in Denpasar |
| `search` | all the result filters below, plus `--limit N` (most itineraries to show, default 10, minimum 1) |
| `price-check` | — (it reports the verdict for the trip as asked) |
| `cheapest` | `--days N` (how many departure dates to try, default 14, maximum 60 — but a window is capped at 40 requests, so above 40 days you must raise `--step`), `--step N` (default 1, minimum 1). **`--return` here sets the trip *length*, not a fixed date** — see below |
| `route` | — |
| `watch` | `--under PRICE` (required), plus the result filters |

**`airports` resolves one interpretation of a name, and cannot tell you the
name was ambiguous.** `airports Springfield` returns `SGF` (Springfield,
Missouri) with nothing to say Springfield, Illinois exists. It answers "which
airports serve this place" — including multi-airport cities like London — not
"which places share this name". City and country do not separate the two
(both are "Springfield, United States"): the `latitude`/`longitude` in the JSON
do. When a name could name several places, say which one came back and where it
is, and ask the user to confirm or to give you the IATA code.

**Shared by all six:** `--json`, `--currency` (default `CAD`), `--country`
(default `CA`) and `--max-requests`. That is the whole shared set — `--limit`
belongs to `search` alone, and passing it to any other command is an argparse
error (exit 2). `--currency` wants a 3-letter code and `--country` a 2-letter
one; anything else is a usage error too.

**Result filters** (on `search`, `cheapest`, `watch`) are applied after
fetching, so they cost no extra requests: `--max-price`, `--airlines AC,WS`,
`--exclude-airlines`, `--max-duration MINUTES`, `--depart-after HH:MM`,
`--depart-before HH:MM`, `--arrive-before HH:MM`, `--arrive-same-day`, `--avoid-layovers YUL,YOW`,
`--max-co2 PERCENT`, `--min-legroom INCHES`, `--avoid-aircraft 737MAX`.

`--max-stops` is **not** one of these — Google applies it server-side, so it
changes which flights are searched, not merely which are shown.

`--max-price`, `--max-duration`, `--limit` and `--under` all reject `0` or a
negative number: a zero ceiling used to read as "no filter" rather than
"impossible". `--min-legroom` is the exception — it rejects negatives only, and
`0` is accepted and means no requirement. Above zero **every leg must state at least
that much**, so an itinerary is dropped if any of its legs publishes no legroom
figure at all — assuming an unstated seat is roomy recommends exactly the
flight the user asked to avoid. `--max-co2` is a signed
percentage against the route's typical emissions (`-20` = at least 20%
cleaner), so negative values are expected there.

Run `python3 flights.py <command> --help` for exact flags.

## Things that will otherwise catch you out

**On a round trip, the price is the whole trip but the legs are only the
outbound.** Google prices round trips as "from $X" against a chosen outbound;
the returning flights are not in the payload. Every itinerary carries
`price_covers` saying which it is. Never tell a user "your return leaves at
16:10" from a round-trip search — that time is the outbound's. **And never add
two one-way searches together and call the total a round trip:** they are
genuinely different fares (YYZ–YHZ measured 98 one way against a 188
round-trip-from), so the sum is simply a wrong number. If they need
specific return times, say the tool reports round-trip totals against outbound
options, and that picking an exact return pair is not supported.

**On `cheapest`, `--return` sets the trip length, and the sweep slides it.**
`cheapest YYZ YHZ --depart 10-22 --return 10-27 --days 7` prices a *five-night*
trip departing on each of seven days — not seven return options for a fixed
27th. Each row in the JSON carries the `return` date actually priced, so check
it before quoting a saving. If the user wants a fixed return, use `search`.

**Prices are for the whole party.** `--adults 2` returns roughly double, and
the figure covers everyone; the human header says "for 2 adults" and the JSON
carries `query.price_covers_travellers`. Never report a party total as a
per-person fare.

**`price` can be `null`.** Google occasionally lists an itinerary with no fare.
Those sort last and are skipped by `cheapest` and `watch`, but a naive
`min(i["price"] for i in ...)` over `search` output raises `TypeError`. Filter
on `price is not None` first.

**`--max-co2` drops itineraries with no emissions estimate**, since there is
nothing to compare. On a route where Google publishes none, any `--max-co2`
returns nothing.

**`--country` may not change the fare.** It is sent as the point of sale, and
in principle that decides which carriers and fare feeds appear — but holding
the currency constant and varying only the country returned *identical* prices
on the route we measured (YYZ–YHZ). The often-quoted "249 CAD vs 181 USD" pair
turns out to be plain FX conversion, not a different fare feed. So: set
`--country` and `--currency` deliberately, report the currency the tool returns,
and do **not** tell a user that searching from another country will find them a
different fare — that is unverified.

**Metro codes work and are often what you want.** `YTO` searches all of
Toronto's airports, `LON` all of London's. `airports` marks them.

**A metro code that already contains the other endpoint is refused.** `YTO YYZ`
or `NYC EWR` exits 2 rather than searching: Google answers those with an empty
payload, which would read as "no flights". The check uses a small built-in
table of metro codes (Toronto, Montreal, New York, London, Paris, Milan, Rome,
Tokyo, Osaka, Washington, Chicago, Berlin, São Paulo, Rio, Buenos Aires,
Moscow, Stockholm, Seoul, Beijing), so it catches the common cases and may miss
a metro code outside that list — a missed check, never a wrong answer.

**A currency Google did not actually price in is refused, not relabelled.**
Google silently ignores a `curr=` value it does not support and prices the page
in the market default. The tool reads the currency off the rendered page and
exits 2 if it disagrees with `--currency`, naming the currency it did get, so
you never see CAD numbers labelled something else. The check is deliberately
conservative — when the page's evidence is mixed or absent it makes no claim
and the search proceeds.

**A filter that cannot possibly match is refused, not silently applied.**
Google publishes each route's real bounds. `--airlines` is a whitelist and so
an OR — `--airlines F8,XX` is fine when F8 flies the route, and only a list
where *nothing* asked for flies it is refused. Excluding every carrier on the
route is refused too. Along with those, a `--max-price` below the cheapest fare, a `--max-duration`
under the quickest flight, or a departure window that excludes itself all exit
2 and say why. Returning "nothing matched" instead would be a false negative —
and exit 1 is the code a watch loop reads as "keep waiting", so an impossible
filter would poll forever and never fire. This applies to `search`, `cheapest`
and `watch` alike (on `cheapest`, the check runs against the first date of the
sweep).

**`watch --under` is checked against the route's floor too.** A threshold below
anything the route has ever sold exits `2` with the real range, rather than
exiting `1` on every poll forever:

```
--under 20 CAD is below every fare on this route; the cheapest is 102 and the
range runs to 953. A watch on this threshold would never fire.
```

`--under` is not a result filter — it is the trigger — so it gets its own check
in `watch`, alongside the filter check that covers the flags above. You do not
need to mirror it with `--max-price`, and should not: `--max-price` also filters
the results.

**Clock filters are times of day, local to their own airport.**
`--arrive-before 18:00` means "lands by 6pm" wherever it lands, so a long-haul
arriving two days later still qualifies — the bound is not a deadline on the
departure date. One consequence worth knowing: a red-eye landing at 00:05 has
the clock time `00:05` and passes any evening bound. If the user means "nothing
landing after 11pm tonight", add `--arrive-same-day`, which requires the arrival
to fall on the departure date. All clock bounds are inclusive.

**`verdict_code` exists to catch a broken scrape, not to be reported.**
`verdict` is read from Google's rendered banner; `verdict_code` is the enum the
payload carries alongside it. Its value-to-word mapping has **not** been
established — only that `3` accompanied "typical" once — so never translate it
or show it to a user. Its whole job is to separate two look-alike cases: no
code and no word means Google published no verdict; a code present with no word
means the banner wording changed and the scrape silently stopped working. The
CLI prints a note when it sees the second. If you see that note on every route,
the skill needs fixing — say so rather than reporting "no verdict available".

**`price-check` does not always get a verdict.** Google ships the
typical/low/high history for some routes and dates and not others, and will
sometimes omit it for a query that carried it minutes earlier. The command
exits `0` only when there is a rendered verdict word ("low", "typical",
"high") to report — that is the whole point of it, so branch on the exit code
rather than null-checking a field. There are two ways to get exit `1`:

- no history block at all — `price_context` is `null`, and the payload carries
  `cheapest_now` plus `route_fare_range` instead;
- a history block whose verdict word is missing — `price_context` is present
  and populated (typical/low/high/history), but `verdict` is `null`.

Either way the fare reported is real. Report that honestly — do not retry
hoping for a different answer, and do not present the cheapest fare as though
it were a verdict.

**Fares are never cached, by design.** Two runs minutes apart legitimately
differ — that is the market, not a bug. Do not tell a user a price is "wrong"
because it moved.

**`--days` on `cheapest` is one request per day.** Single searches are capped
at **5 requests**; `cheapest` defaults its own budget to one request per date
plus three spare for retries
(up to 40), because asking for `--days 14` *is* asking for 14 requests. A plan
that would exceed the ceiling is refused before the first request rather than
half-run. Use `--step 2` to halve a wide sweep.

That ceiling is low on purpose. Google blocks rather than throttles, and on
claude.ai the request leaves from a shared datacenter address — an over-eager
sweep does not just fail for you, it degrades that address for everyone using
it.

**Google blocking looks nothing like an error.** When it decides you are a bot
it returns HTTP 200 with a consent or captcha page. The client detects this by
checking for the query echo every real results page carries, and exits 3 with a
clear message. It will never be reported as "no flights".

**Every search is cross-checked against Google's own echo of the query.** It
restates the party size and dates it actually searched; if those do not match
what was asked, the command exits 3 rather than returning a real fare for a
different trip. That is the failure this undocumented encoding actually has — a
malformed date, for instance, is silently rewritten to a different one. A page
carrying no echo at all is the same signal the block detector uses, so it is
reported as a block, not as an empty route.

## Exit codes

Identical under `--json`.

| Code | Meaning |
|---|---|
| `0` | found what was asked for (`airports`: the place resolved to at least one airport) (for `watch`: the fare is at or below `--under`; for `price-check`: there is a verdict word) |
| `1` | the query worked, nothing matched (`airports`: Google returned a results page but routed nowhere) (for `watch`: still above the threshold, *or* nothing priced matched the filters; for `price-check`: no verdict word for these dates; for `cheapest`: no priced option on any date swept) |
| `2` | usage or lookup error — see below |
| `3` | network failure, Google blocking, a payload whose shape changed, or (for `route`) a response with no route metadata in it |

Exit `2` covers, all refused **before** any request unless noted:

- a code that is not three letters, or origin equal to destination;
- a metro code that already contains the other endpoint (`YTO YYZ`);
- a departure in the past, or a date beyond the ~330-day booking horizon;
- a return before the departure;
- a party outside the bounds above (argparse rejects the per-flag ranges; the
  9-traveller total and the lap-infant-per-adult rule are checked after);
- a bad flag or a bad value for one — argparse errors also exit 2, and under
  `--json` they come back as a proper error object rather than a usage block on
  stderr;
- a sweep whose plan exceeds `--max-requests`, or `--days` over 60;
- **after** one request: a currency Google did not actually price in, a filter
  the route's own bounds prove can never match, and a `watch --under` threshold
  below the route's cheapest fare.

**The 1 vs 3 split is what makes a watch loop safe.** Only `1` means keep
waiting. A `3` means the check itself failed and says nothing about the fare —
without that distinction a watch would poll a captcha page forever.

## JSON output

Every command returns a single object: `{"schema_version": 1, "ok": true, ...}`
on success, `{"schema_version": 1, "ok": false, "error": "..."}` on failure.
Unlike some skills in this repo, **no command returns a bare array** — the
per-command payload always hangs off that object.

On failure the object has **only** those three keys — there is no partial
payload to salvage, so branch on `ok` (or on the exit code) before reading
anything else.

Which keys you get depends on the command — they are **not** interchangeable.
This table is exhaustive for the top level:

| Command | Keys beside `schema_version` / `ok` |
|---|---|
| `search` | `query`, `returned`, `count`, `total_before_filters`, `unparsed`, `itineraries[]` |
| `price-check` | `query`, `cheapest_now`, `price_context` (and `route_fare_range` when there is no context) |
| `cheapest` | `query`, `days[]` (each `depart`, `return`, `cheapest`, `options`), `best` |
| `route` | `query`, `filters`, `airports[]` |
| `watch` | `query`, `hit`, `under`, `cheapest`, `itinerary` |
| `airports` | `place`, `matched[]`, `also_named_on_the_page[]` |

`query` (on all five trip commands) restates what was searched: `origin`,
`destination`, `depart`, `return`, `trip` (`"round trip"` / `"one way"`),
`cabin`, `price_covers_travellers`, `passengers` (`adults`, `children`,
`infants_in_seat`, `infants_on_lap`), `country`, `currency`.

Each entry in `itineraries[]` — and `watch`'s single `itinerary` — carries
`price`, `currency`, `price_covers`, `carrier`, `carrier_name`,
`carriers[]`, `carrier_names[]`, `origin`, `destination`, `depart`, `arrive`,
`duration_minutes`, `stops`, `nonstop`, `layovers[]` (IATA codes, in order),
`layover_details[]`, `legs[]`, `co2_grams`, `co2_typical_grams`,
`co2_percent_vs_typical`, `bucket` (Google's own `"best"`/`"other"` ranking)
and `token` (opaque; not usable to select a return leg — see below).

**Use `carrier_names[]`, not `carrier_name`, when telling the user who flies
it.** On a multi-carrier trip Google puts the literal string `"multi"` in
`carrier` and only its headline airline in `carrier_name`, so a Toronto–
Kathmandu trip flown Porter then Qatar reports `carrier_name: "Porter
Airlines"`. `carriers[]` (`["PD", "QR"]`) and `carrier_names[]` (`["Porter
Airlines", "Qatar Airways"]`) list every operating airline, in leg order.
`--airlines` already matches on the real per-leg carriers, so it is unaffected.

`layover_details[]` gives one entry per connection — `code`, `depart_code`,
`name`, `city`, `minutes`, `airport_change` — so a 7h45m wait in Doha, or a
connection that requires changing airports, can be reported without inferring
it from leg times. `airport_change` is true when `depart_code` differs from
`code`; that is a detail a traveller must be told, not left to discover.

Each entry in `legs[]` carries `carrier`, `carrier_name`, `flight_number`,
`origin`, `origin_name`, `destination`, `destination_name`, `depart`, `arrive`,
`duration_minutes`, `aircraft`, `legroom`, `co2_grams`. `aircraft` and
`legroom` are strings (`"Boeing 737MAX 8 Passenger"`, `"29 in"`) and either can
be `null`.

`price_context` (`price-check`) carries `verdict`, `verdict_code` (see above —
diagnostic only, never report it), `current`, `typical`, `low`,
`high`, `currency`, `history[]` (objects of `date` and `price`) and `advice`.
Any of them except `currency` and `history` can be `null`.

`route_fare_range` (`price-check`, only when there is no `price_context`) —
`min`, `max`, `currency`. These are Google's own filter bounds for the **whole
route**, not for the dates you asked about, and they are typically far wider
than the search returned. Do not quote them as "fares on these dates".

`filters` — the route's `price_min`, `price_max`, `currency`, `airlines[]`,
`alliances[]`, `connection_airports[]` (each a `code`/`name` object — as are `airlines[]` and `alliances[]`),
`duration_min_minutes`, `duration_max_minutes` and `stop_filter_options[]` —
comes **only** from `route`. `search` does not return it. `stop_filter_options`
is Google's own enum for its stops chip, not a count of stops; it is passed
through unmapped, so do not read numbers out of it.

`airports` returns `matched[]` as full objects (`code`, `name`, `city`,
`country`, `latitude`, `longitude`) and `also_named_on_the_page[]` as bare
code strings. The latter is *not* "airports near this place" — it is whatever
else Google happened to name while answering — so never present it as a list of
alternatives. `route`'s `airports[]` uses the same object shape.

`unparsed` on `search` is the count of options Google returned that did not
match the expected shape and were dropped. Non-zero means the list is
incomplete — say so rather than presenting it as the full set. `count` is how
many survived the filters, `returned` how many are actually in the array after
`--limit`, and `total_before_filters` how many the route had.

Times are local to their own airport and carry no timezone offset. Use
`duration_minutes` for elapsed time — never subtract the two clock times across
a timezone change.

## Reporting back

Lead with the answer, not the data.

- **Give the number first.** "$231 round trip on Flair, nonstop, 13:00–16:10."
  Then the detail.
- **For `price-check`, give the verdict and the evidence in one breath.**
  "Google calls this typical — $231 against a usual $228, in a range of
  $190–$345 over the last 60 days." That is the whole value of the command; a
  bare price wastes it.
- **Quote what a human chooses on:** departure and arrival times, airline, stop
  count and where the stop is, aircraft and legroom if they asked about
  comfort, CO2 if they asked about emissions.
- **Say what the price covers.** "Round-trip total" or "one way", every time.
- **Never paste raw JSON at the user.** Summarise; offer the detail.
- **Volunteer the flexible-dates finding.** If they are not fixed to a date,
  `cheapest` frequently beats the asked-for date by a wide margin, and users
  rarely think to ask.
- **Pass a refusal on as a refusal.** Exit 2 means the question as asked cannot
  be answered — an impossible filter, a metro code covering the destination, a
  currency Google did not price in. Say what was refused and why; never
  paraphrase it as "no flights found".
- **Say when the list is incomplete.** A non-zero `unparsed`, or `count`
  smaller than `total_before_filters`, both change what the answer means.
- **Do not read `route`'s airline list out as "who flies this".** It is
  Google's filter chip list. Name carriers from `search` results instead.

## Self-check

```bash
cd <path-to-this-skill>/scripts
python3 test_flights.py --offline   # pure logic against a saved payload, no sockets
python3 test_flights.py             # adds a live group against www.google.com
```

A `[network]` failure names the host, so "Google is blocking this IP" stays
distinguishable from "the skill is broken". Run it if results ever look
implausible: Google's payload is a positional array with no field names, so the
failure to fear is a *plausible wrong answer*, and the offline group asserts
against a real captured payload to catch exactly that.
