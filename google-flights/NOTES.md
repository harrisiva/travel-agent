# Google Flights — API research notes

Background for the shipped skill: `SKILL.md`, `scripts/`, the symlink in
`.claude/skills/` and `dist/google-flights.skill` all exist. This file records
what the data surface looks like and how each field's meaning was established,
so the reverse-engineering does not have to be redone when Google changes
something. It is not the interface contract — that is `SKILL.md`.

Everything below was verified with live requests on 2026-09-08 against
`www.google.com/travel/flights` with `hl=en&gl=CA&curr=CAD`. The field map is
what `scripts/gflights/tfs.py` actually emits today.

## Links

| What | URL |
|---|---|
| Search page (the only endpoint needed) | `https://www.google.com/travel/flights/search?tfs=<protobuf>&hl=en&gl=CA&curr=CAD` |
| Worked example (YYZ→YHZ 2026-09-25 / YHZ→YYZ 2026-09-27, 1 pax) | `https://www.google.com/travel/flights/search?tfs=CBwQAhoeEgoyMDI2LTA5LTI1agcIARIDWVlacgcIARIDWUhaGh4SCjIwMjYtMDktMjdqBwgBEgNZSFpyBwgBEgNZWVpAAUgBcAGCAQsI____________AZgBAQ&hl=en&gl=CA` |

## There is no JSON endpoint to call

Google Flights server-renders the entire result set into the initial HTML as
`AF_initDataCallback({key: 'ds:N', data: [...]})` blocks. No XHR, no
`batchexecute` RPC, no API key, no cookies, no JavaScript, no browser. A plain
`curl` with a browser `User-Agent` returns the full payload (~2.7 MB of HTML).

This is the opposite of the usual pattern in this repo ("find the JSON API,
don't scrape the HTML") — here the JSON *is* in the HTML, as literal JS array
literals. Parse with a regex for the `ds:` block plus `json.loads`; do not
parse the DOM.

```python
re.search(r"AF_initDataCallback\((\{key: 'ds:1'.*?)\);</script>", html, re.S)
# then: re.search(r"data:(.*), sideChannel", block, re.S) -> json.loads
```

### What each block holds

| Key | Contents |
|---|---|
| `ds:0` | echo of the query — airports, dates, passenger count |
| `ds:1` | **the results** (~36 KB for a simple round trip) |
| `ds:2` / `ds:3` / `ds:4` | static country / currency / language lists |

### Inside `ds:1`

| Index | Contents |
|---|---|
| `[2]` | "Best flights" bucket |
| `[3]` | "Other flights" bucket |
| `[5]` | price history and the "typical price" band. `[5][0]` is a verdict enum — `3` accompanied a rendered "typical" in one capture, which is not enough to map it, so the CLI reads it only to detect that the banner scrape has broken (code present, word missing) and never translates it |
| `[7]` | filter metadata — price slider min/max, full airline and alliance lists for the route |
| `[11]` / `[26]` | per-carrier baggage and accessibility URLs |

Per itinerary, and per leg within it: carrier code and name, origin/destination
codes *and* full airport names, departure and arrival `[h, m]`, duration in
minutes, flight number, aircraft type (`"Boeing 737MAX 8 Passenger"`), legroom
(`"29 in"`), CO2 grams, and price as `[null, 249]` in the requested currency.

Legroom, aircraft and emissions are several clicks deep in the UI but come free
in the payload.

## Filtering

Two layers, and a skill wants both.

### Server-side: re-encode `tfs`

`tfs` is base64url-encoded protobuf. It can be synthesized from scratch — an
encoder written by hand reproduced the example query's results exactly (same 18
itineraries, same $249 floor). Fields confirmed empirically:

| Field | Meaning | Evidence |
|---|---|---|
| root `1` | constant `28` | present in every request that works |
| root `2` | constant `2`; not a cabin selector despite appearances | `1`, `3`, `4` all give an empty payload |
| root `3` (repeated) | one slice per leg: `2` = date, `13` = origin, `14` = destination | changing them changes the route |
| root `3` → `5` | **max stops** | YYZ–KTM: unset → $2941 (1–2 stops); `=1` → $3035 (1 stop); `=0` → no results |
| root `8` (repeated) | **passenger list** — one varint per traveller (1 adult, 2 child, 3 infant in seat, 4 infant on lap) | emitting it twice took YYZ–YHZ from 231 to 461 |
| root `9` | **cabin class** — 1 economy, 2 premium economy, 3 business, 4 first | YYZ–LHR, all else equal: 704 / 2214 / 3771 / 9243 CAD |
| root `19` | **trip type** — 1 round trip, 2 one way | flipping 1→2 dropped YYZ–YHZ from 231 to 98, the true one-way fare |
| root `14`, `16` | fixed bytes; must be present verbatim | omitting either returns nothing |

> **Correction.** An earlier version of this table had fields 8 and 9 as trip
> type and passengers, and treated 19 as an opaque tail byte. That mapping is
> wrong, and wrong in the dangerous direction: it returns a real fare for a
> different query. `--adults 2` set the *cabin* instead of the party size, and
> a lap infant (`9 = 4`) silently priced the trip in **first class**. Every row
> above was re-verified by changing one field at a time and watching the fare
> move. Airport endpoints nest as `{1: kind, 2: code}` inside fields 13 and 14,
> where kind `1` is an IATA code — including metro codes like `YTO`, which
> expands to YYZ and YTZ — and kind `2` is a Knowledge Graph id. A kind
> mismatch returns zero results rather than an error.

The tail bytes reproduced verbatim are field 14 (`70 01`) and field 16
(`82 01 0b 08 ff…01`). Their meaning is still unknown; removing either empties
the response, so they are emitted as captured. Cabin class is **not** among
them — an earlier note said seat class lived somewhere in the tail, which is
wrong: it is field 9, verified by the four-way price split above.

### Client-side: filter the parsed array

Price cap, airline whitelist, departure-time window, total duration, carbon,
legroom and aircraft type are all already in `ds:1`, so they need no extra
request. `ds:1[7]` supplies the valid airline values and the price bounds, so a
filter can be validated before it is applied.

## Still open, now that it has shipped

- **Return legs — investigated and unresolved.** Round-trip results are
  outbound-only and the price is a "round trip from" total. `itinerary[1][1]`
  turns out *not* to be a selection handle: it decodes to a display record
  (search-session id, flight number, price ×100, currency). Passing it back as
  `tfu`, `tfs2` and `f`, and sweeping a selected-flight submessage across slice
  fields 1, 4, 6–12 and 15–20, were all silently ignored — the `ds:0` query
  echo came back byte-identical to the control, so Google never parsed them.
  No `tfs`/`tfu`/`flt` link to a return-leg page appears anywhere in the 2.6 MB
  of result HTML. Two one-way searches are the usable approximation, but they
  are genuinely different fares (YYZ–YHZ: 98 one way against 188 round-trip-
  from) and must never be summed and called a round trip.
- **Positional arrays.** The payload is undocumented and positional, so a
  shifted index yields a *plausible wrong answer* rather than a crash — exactly
  the silent failure the self-check convention exists for. Addressed, not
  closed: `test_flights.py`'s offline group
  asserts against a captured payload — price is an int in a sane range, airport
  codes are three letters, duration is minutes. A Google layout change can
  still shift an index, and the fixture is the only thing that would catch it.
- **Hostility.** No key is needed today, which satisfies the repo's "no keys,
  no config" rule, but this is an internal surface with no stability promise
  and Google polices user-agent and request rate harder than Camis5 or
  Cineplex. Hence the fan-out ceiling: 5 requests by default, 40 at the very
  most, and a 0.7 s throttle between consecutive requests.
- **Nothing volatile may be cached.** Fares change by the minute. Airport and
  airline catalogues (`ds:2`, `ds:1[7]`) are the cacheable part — and the skill
  as shipped caches nothing at all, so there is no cache file to go stale.

## Traps that return a plausible wrong answer

Google answers a query it cannot parse with a *substitution* or an empty
payload, never an error. Each of these was observed live.

- **A malformed date is silently rewritten.** `12/11/2026` returned 17 real
  itineraries — for `2026-10-21`. A user asking about 12 November gets October
  fares with no warning. Validate `YYYY-MM-DD` before sending.
- **Round-trip type with a single slice does not error.** It returns
  round-trip-from prices for a return nobody asked for (188 against a true
  one-way 98). Always send field 19 = 2 explicitly for one-way.
- **A place-kind mismatch returns zero results.** `{1:1, 2:"/m/0h7h6"}` and
  `{1:2, 2:"YTO"}` both come back empty rather than erroring.
- **Origin equal to destination returns empty**, not an error. So does a metro
  code paired with an airport it contains (`YTO` → `YYZ`). The CLI refuses both
  up front; the metro check uses a built-in table of ~19 metro codes, so it is
  a best-effort catch rather than exhaustive.
- **The forward horizon is exactly 330 days.** Binary-searched *from
  2026-09-08*: 2027-08-04 works, 2027-08-05 returns no `ds:1` block at all. The
  dates are the measurement's; the number of days is the finding, and it is
  what `MAX_DAYS_AHEAD` encodes.

### `ds:0` is a free oracle, and worth using

Google restates the query it actually ran: `ds:0[1][1][6]` is
`[adults, children, infants_in_seat, infants_on_lap]`, and each slice's date
sits at `ds:0[1][1][13][k][6]`. Cross-checking the response against the request
catches the entire class of bug above — a wrong party size, a substituted date
— which validating our own inputs cannot. The CLI does this on every search.

### Detecting a block: check for a positive, not for markers

Matching strings like `recaptcha` or `enablejs` does not work: **a perfectly
healthy 2.8 MB results page contains `recaptcha`** (a preloaded JS reference,
about 23 KB in). A marker list would report every successful search as blocked
the moment the search window widened past it.

The reliable signal is the presence of the `ds:0` query echo, which every real
results page carries and no interstitial does. `looks_blocked` is exactly that
one test: `"AF_initDataCallback({key: 'ds:0'" not in html`.

Twenty consecutive searches at ~1 request/second were never blocked, so the
shape of a real block page is still unconfirmed — which is itself a reason to
detect the healthy case rather than guess at the unhealthy one. The corollary:
`block_reason`'s marker list (`consent.google.com`, `/sorry/index`, "detected
unusual traffic") only decorates the error message with a guess at *which*
interstitial arrived, and has never been matched against a real one. It falls
back to "a page with no search results in it". The classification into exit 3
does not depend on those markers; only the wording does.

## Cookies, caching and "do repeat searches raise the price?"

Short answer: no evidence for the cookie mechanism, and the skill should stay
cookieless anyway — for hygiene, not because it wins a lower fare. Do not claim
in `SKILL.md` that the skill dodges price tracking; it can't, and there is less
to dodge than the folklore says.

### Measured, 2026-09-08

Six back-to-back searches for the example query through a persisted cookie jar
(warmed by hitting `google.com` first), against a cookieless control:

```
cookieless #1                n=18  cheapest=249  top5=[249, 306, 306, 306, 306]
WITH cookie jar, repeat #1   n=18  cheapest=249  top5=[249, 306, 306, 306, 306]
WITH cookie jar, repeat #3   n=18  cheapest=249  top5=[249, 306, 306, 306, 306]
WITH cookie jar, repeat #6   n=18  cheapest=249  top5=[249, 306, 306, 306, 306]
cookieless again             n=18  cheapest=249  top5=[249, 306, 306, 306, 306]
```

All 18 fares identical every time. Google set only two cookies on this path —
`SEARCH_SAMESITE` and `__Secure-STRP` — neither an identity cookie (no `NID`,
no `1P_JAR`).

Limits of that test: one route, logged out, over a few minutes, against
Google's aggregated feed rather than a carrier's own booking funnel. It rules
out the crude "search twice, price goes up" mechanism on this query. It is not
proof that no personalization exists anywhere.

### Point of sale: a claim this file used to overstate

The original measurement here varied `gl` **and** `curr` together:

```
gl=CA curr=CAD  ->  cheapest 249
gl=US curr=USD  ->  cheapest 181     # ~246 CAD
```

That only demonstrates currency conversion — 181/249 is 0.73, the FX rate — and
it was wrongly written up as evidence that point of sale changes the fare.
Holding the currency constant and varying only `gl` gives identical prices:

```
gl=CA curr=USD  ->  [72, 80, 80, 80, 80, 80, ...]
gl=US curr=USD  ->  [72, 80, 80, 80, 80, 80, ...]   # n=18 both, byte-identical
```

`gl` may still matter on routes where carrier availability genuinely differs by
market; it was simply never shown to here. Pin `gl` and `curr` explicitly for
reproducibility, and do not tell a user that searching from another country
finds a different fare.

### What the published evidence says

- **Consumer Reports, 2016** — 372 searches across nine airline ticketing
  sites, same itinerary, two simultaneous browsers (cookies intact vs.
  scrubbed). Identical prices 88% of the time; incognito cheaper in 7%, *more
  expensive* in 5%. Where fares differed, the scrubbed browser was higher 59%
  of the time — the opposite of the folklore — mostly on OTAs, not airlines.
- **US DOT, 2017** — found no evidence of systematic price increases on repeat
  searches; carriers reported no persistent per-user identifier feeding fare
  decisions.
- **What cookies are actually used for** — counting views and abandoned
  bookings per flight to feed demand models. Aggregate, not per-person.
- **Why it feels true** — fares move in real time on inventory and demand. A
  fare that rises for everyone between two searches looks caused by the second
  search.

### The real version of the concern

Personalized pricing in air travel is not a myth; it just lives at the airline,
not in the browser, and would not appear in the test above at all.

- **Delta + Fetcherr** — AI-set fares on ~3% of flights, targeted at 20%. Delta
  states pricing "never takes into account personal data"; outside experts note
  such a model can still infer willingness to pay from device type, search
  behavior and location.
- **FTC, January 2025** — preliminary surveillance-pricing findings confirmed
  mouse movements and abandoned carts can be used to tailor individual prices.
- **Congressional probe** — eight US carriers pressed to explain whether AI and
  consumer data shape individual fares, August 25 deadline; still live as of
  August 2026.

### Consequences for the skill

- Send no cookies and stay logged out. Removes a variable rather than winning a
  discount.
- Pin `gl` and `curr` on every request; treat them as part of the query.
- Never cache fares. Already required by the repo convention, and this is the
  second reason: if prices move minute to minute, a cached "cheap" answer is
  wrong in the direction that costs the user money.

Sources: [Consumer Reports via TIME](https://time.com/4899508/flight-search-history-price/) ·
[Skyscanner](https://www.skyscanner.net/flights/advice/do-flight-prices-go-up-the-more-you-search) ·
[Kiwi.com](https://www.kiwi.com/stories/stop-wasting-time-incognito-mode-doesnt-cut-airline-fares/) ·
[Mighty Travels](https://www.mightytravels.com/2024/12/do-browser-cookies-really-impact-flight-prices-a-data-driven-investigation/) ·
[ABC News](https://abcnews.com/GMA/Travel/delta-ai-ticket-pricing-means-air-travel/story?id=124343088) ·
[Afar](https://www.afar.com/magazine/delta-is-using-ai-to-set-fares-what-that-means-for-travelers) ·
[PYMNTS](https://www.pymnts.com/transportation/travel-payments/2025/delta-air-lines-tests-ai-powered-personalized-pricing/) ·
[TechTimes](https://www.techtimes.com/articles/324599/20260815/surveillance-pricing-probe-targets-eight-airlines-august-25-deadline.htm)
