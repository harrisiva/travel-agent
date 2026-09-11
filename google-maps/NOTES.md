# Google Maps — API research notes

What the data surface looks like, so the reverse-engineering does not have to be
redone. Everything below was verified with live requests on 2026-09-08 from a
plain `curl` with a browser `User-Agent` — no key, no cookies, no login, no
browser.

## The one endpoint that matters

```
GET https://www.google.com/search?tbm=map&hl=en&gl=ca&q=<query>&pb=<protobuf>&tch=1&ech=1
```

This is the RPC the Maps web frontend calls for its result list. It returns
places with live open/closed status, today's hours, rating, address,
coordinates, phone, website, categories and IANA timezone.

`tbm=map` is the whole trick. `/maps/preview/place` and `/maps/preview/directions`
both return **400** on every `pb` shape tried; `/maps/dir/` returns the app shell
with no `AF_initDataCallback` payload (unlike Google Flights, which server-renders
its results). Directions are *not* reachable this way — see *Driving distance*.

### Response envelope

Not plain JSON. One or more `{"c":<n>,"d":"<chunk>"}` objects, optionally
separated by `/*""*/`, whose `d` values concatenate into a `)]}'\n`-prefixed
JSON array:

```python
parts, i = [], 0
while i < len(raw):
    if raw.startswith('/*""*/', i): i += 6; continue
    if raw[i] != "{": i += 1; continue
    obj, i = json.JSONDecoder().raw_decode(raw, i)
    parts.append(obj.get("d", ""))
payload = "".join(parts)
data = json.loads(payload[payload.index("\n") + 1:])
```

A single-object read (`json.loads(raw)`) works on small responses and raises
`Extra data` on large ones. Always run the chunk loop.

### The pb parameter

Positional, `!`-delimited, `<index><type><value>`. This shape works:

```
!4m12!1m3!1d<span>!2d<lng>!3d<lat>!2m3!1f0!2f0!3f0!3m2!1i1024!2i768
!4f13.1!7i<page_size>!8i<offset>!10b1!12m6!2m3!5m1!6e2!20e3!10b1!16b1
```

| Token | Meaning |
|---|---|
| `1d<span>` | viewport span in metres — the search radius knob |
| `2d` / `3d` | longitude, then **latitude** (that order) |
| `7i<n>` | results per page; 20 is the natural page size |
| `8i<n>` | offset. `8i20` is page 2 — **verified disjoint** from page 1 |
| `6e2`, `20e3` | required; omitting either empties the result list |

`hl` and `gl` are point-of-sale style knobs, as in the Flights notes. Pin them.

### Where the fields live

Results are slots under `data[0][1]`. A slot is a place when `slot[14]` is a
list — call that the blob.

| Path in blob | Field |
|---|---|
| `[11]` | name |
| `[18]` | full address (name-prefixed) |
| `[9][2]`, `[9][3]` | latitude, longitude |
| `[10]` | feature ID (`0x882b357f...:0x23ea...`) |
| `[4][7]` | rating |
| `[13]` | category list |
| `[30]` | **IANA timezone** (`America/Toronto`, `Asia/Tokyo`) |
| `[7][0]` | website |
| `[178][0][3]` | phone, E.164 |
| `[203][1][8][0]` | **live status** — `"Open"` / `"Closed"` |
| `[203][1][4][0]` | status with detail — `"Open · Closes 12 a.m."` |
| `[203][0][0][3][0][0]` | today's hours — `"11:30 a.m.–12 a.m."` |
| `[78]` | **canonical place_id** (`ChIJJZP2IzPL1IkRoIXM5dEE9ck`) |
| `[32][0][1]` | one-line editorial summary |
| `[14]` | neighbourhood (`"Old Toronto"`) |
| `[166]` | city/region (`"Toronto, ON"`) |
| `[146][0]` | operational state — `9` operating, `10` closed down |

The table in `scripts/gmaps/fields.py` is the executable copy of this; keep the
two in step. Re-verified against a live Toronto search on 2026-09-08.
Verified against Toronto (`gl=ca`) and Tokyo (`gl=jp`); the Tokyo run returned
`Asia/Tokyo` and a correct local-time open/closed split, so status is computed
in the **place's** timezone, not the caller's.

## Limits found

**Only today's hours — exhaustively confirmed.** `[203][0]` is a *list* of day
records, so the schema holds a week; Google sends one entry. What was tried,
all returning exactly one day:

| Surface | Result |
|---|---|
| `search?tbm=map`, 4 `pb` variants + a `"<name> hours"` query | 1 day |
| `/maps/preview/place` with a real ftid, 5 more `pb` variants | 1 day |
| `/maps/preview/place` with the full `13m` block, counts 40-61 | all HTTP 400 |
| `/maps/place/…` HTML | app shell, zero weekday strings |
| plain `google.com/search`, 6 UAs + `gbv=1` + consent cookies | 92 KB JS shell, `enablejs` redirect, zero weekdays |

The first 400 on `/maps/preview/place` in an earlier pass was a malformed `pb`,
not a dead endpoint — it works fine with a real feature ID. It just serves the
same abridged hours.

The full week is served by a *different* surface entirely — see **Full-week
hours: the embed endpoint** below. None of the search or place-detail `pb`
flags reach it.

**Hours are often missing entirely.** A Tokyo result returned `status=None`
while its neighbours returned `Open`/`Closed`. This is the silent-failure case
that matters: treating absent hours as closed is a false negative, and per the
repo rule it must be reported as *unknown*, never folded into "closed".

**Slot offset is not fixed.** A 20-result search put the header at slot 0 and
results from slot 1; a single-result query put the result at slot 0. Scan every
slot for a blob; never index a fixed position.

**Positional arrays.** As in the Flights notes, a shifted index yields a
plausible wrong answer rather than a crash. Assert on shape: rating in 0–5,
coordinates in range, feature ID matching `0x[0-9a-f]+:0x[0-9a-f]+`.

## Driving distance: `/maps/preview/directions`

**Solved, and it carries live traffic.** The earlier 400 on this endpoint was a
malformed `pb`, not a dead surface. The real `pb` was found by grepping the
`/maps/dir/...` page HTML for the `<link rel=preload>` tag Google itself emits
pointing at this endpoint.

```
GET https://www.google.com/maps/preview/directions?authuser=0&hl=en&gl=ca&pb=<PB>

PB = !1m4!3m2!3d<olat>!4d<olng>!6e2
     !1m4!3m2!3d<dlat>!4d<dlng>!6e2
     !3m12!1m3!1d20000!2d<midlng>!3d<midlat>!2m3!1f0.0!2f0.0!3f0.0!3m2!1i1024!2i768!4f13.1
     !6m6!20m5!1e<mode>!2e3!5e2!6b1!14b1
```

`1e<mode>` selects the travel mode: `0` drive, `1` bike, `2` walk, `3` transit.
**Unrecognised tokens in this block are ignored in silence** — a malformed mode
does not 400, it returns a driving route. Google echoes the mode it actually
used at `route[0][0]`, so check it on the way back rather than assuming; that
check is the only thing between "43 minute walk" and a 13 minute drive reported
as one. Transit routes additionally carry departure/arrival clock times at
`route[0][5]`, as `[[.., .., "9:59 PM"], [.., .., "10:27 PM"]]` with the IANA
zone alongside.

Needs a browser User-Agent; no key, no cookies, no session token.

**Waypoints are `3d`=latitude, `4d`=longitude — the OPPOSITE order from the
search pb**, where `2d` is longitude. Swapping them routes between two other
places and returns a perfectly plausible number rather than an error.

Keep the pb minimal. The page's own URL carries `!15m3!1s<kEI>!7e81!15i10142`
(not required) and `!6m62!…` option blocks — appending those to this minimal pb
returns HTTP 400.

The body is `)]}'\n` + **plain JSON** — not the `{"c":…,"d":…}` chunked envelope
the search endpoint uses. Do not reuse the search decoder.

Routes are at `data[0][1]`; per route `rt`:

| Path | Content |
|---|---|
| `rt[0][1]` | "via" road name (may be null) |
| `rt[0][2]` | `[metres, "31.2 km", 0]` |
| `rt[0][3]` | `[seconds, "32 min"]` — free-flow |
| `rt[1][0][0][10]` | traffic: `[[1826,"30 min"], null, 3, [1680,"28 min"], [1647,2334,"27-39 min"]]` |

That traffic block is `[in-traffic duration, null, level, no-traffic duration,
[best_s, worst_s, range text]]`. It is absent on some routes, so
`traffic_aware` must be checked rather than assumed.

Verified: Union Station -> Pearson = 31.2 km, 32 min free-flow, 30 min in
traffic, range 27-39 min. Matches ground truth.

The trap that was almost shipped: the `"15 min"` / `"30 min"` strings in the
`/maps/dir/` HTML are the departure-time dropdown, **not** a route.

### The star trick: many destinations in one request

The endpoint accepts a long waypoint chain and reports every leg, so chaining
`O, D0, O, D1, O, …` makes each **even-indexed** leg an origin-to-destination
route. Legs are at `data[0][1][0][1]`. Verified identical to the standalone
two-point call for the same pair, at roughly a third of the wall time for four
destinations.

**Star legs carry no traffic block** — the per-leg traffic slots come back
empty. So `routing.annotate` does two passes: one star request to price and
rank everything, then an individual two-point call for just the five nearest,
which is where the live-traffic numbers come from. Anything past those five
keeps its free-flow figure and `traffic_aware: false`.

## Full-week hours: the embed endpoint

**Solved.** `www.google.com/maps/embed` returns all seven days for a feature id
in a ~4 KB HTML response — no key, no cookies, and verified with **no
User-Agent header at all**:

```
GET https://www.google.com/maps/embed?pb=!1m17!1m12!1m3!1d2886!2d0!3d0
    !2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m2!1m1!1s<FTID>
    !5e0!3m2!1sen!2sca!5m2!1sen!2sca
```

Measured coverage: **20/20** of a live downtown Toronto search, versus 25% for
the OpenStreetMap fallback this replaced — which was also stale, saying `02:00`
for Culture Crust, Kitchener, where Google says 3 a.m. That fallback is gone;
Google is now the only host contacted.

Constraints, each verified:

- The FTID's `:` must be percent-encoded. The **`place_id` (`ChIJ…`) does not
  work** — HTTP 200 with an empty map. Only the hex feature id from `blob[10]`.
- **Do not pass the pb through `requests`' `params=`.** It percent-encodes the
  `!` delimiters to `%21`, and Google answers with an empty map rather than an
  error — a silent "no hours". Build the URL by hand.
- `hl` must be `en`, or day names come back localised and any English-weekday
  parser silently fails.
- The `!1m12` viewport wrapper is required, but its coordinates are ignored:
  `!2d0!3d0` works. Removing the wrapper gives HTTP 400.

The body is plain HTML containing `initEmbed([...])` — no `)]}'` prefix and no
chunk envelope. Decode with `raw_decode` from the `[`. The place record is found
structurally (its head is `[ftid, "name, address", [lat,lng], cid]`) and the day
block by shape, rather than by fixed index — it sat at index 39 everywhere, but
shape-matching costs nothing and survives renumbering.

### The ragged clock encoding — the trap worth knowing

Intervals are `[display_string, [[start_h, start_m], [end_h, end_m]]]`, but
Google omits whatever it considers obvious, and **an omission means midnight,
not zero**:

| raw | display | meaning |
|---|---|---|
| `[[17], []]` | "5 p.m.-12 a.m." | ends at midnight |
| `[[], [23]]` | "12 a.m.-11 p.m." | starts at midnight |
| `[[], []]` | "Open 24 hours" | both |
| `[[8], [23, 30]]` | "8 a.m.-11:30 p.m." | fully specified |
| `[["Closed"]]` | "Closed" | no pair at all |

Reading an empty end as `0` turns "5pm till midnight" into a negative span,
which then reads as closed — a false negative that looks entirely plausible.
An end at or before the start means the span crosses midnight (`[[10], [3]]`
= 10:00-03:00) and its tail belongs to the next day.

## Rate limiting

Eight rapid Google calls: all 200, 0.56–1.53 s, no challenge, no cookie
requirement. It was not probed to its limit and Google publishes none. These
are courtesy-hosted internal surfaces with no stability promise, so the request
ceilings are not optional here. The package fans out at 12 concurrent requests
by default (`--concurrency`), measured as the knee: 8 workers 0.74 s, 12 workers
0.60 s, 16 workers 0.61 s on a 20-request workload.

Google is the only host this skill talks to. An earlier OpenStreetMap/OSRM
fallback was removed once the embed endpoint reached 20/20 coverage; nothing in
`scripts/` reaches any other domain.

## Nothing here may be cached

Open/closed status is this skill's equivalent of campsite availability: a stale
"open" sends someone to a locked door. Coordinates from a geocode are the
cacheable part; hours and status never are.
