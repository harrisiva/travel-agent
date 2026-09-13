---
name: google-hotels
description: >-
  Price a hotel or hostel for specific nights via a bundled Python CLI over
  Google Hotels: every seller's nightly and stay price with and without tax,
  the taxes/fees breakdown, cancellation deadlines, star class, rating
  and amenities — plus a priced shortlist of the hotels google-maps found near
  a place, filterable by amenity, and the cheapest check-in day for one hotel
  across a range. It prices hotels BY ID: run google-maps first (`gmaps.py
  search --near <place> --query hotels --full --json`). Use whenever the user
  asks what a hotel costs on given dates, who sells it cheapest, whether that
  includes tax, what hotels near a place cost, which have free Wi-Fi, which
  night or week is cheaper, or wants a watch on one hotel's price. It can't
  search a whole town's market, has no "good price" verdict, and reaches
  vacation rentals only from a pasted Google Hotels link. Do NOT use for
  flights (google-flights), rental cars (enterprise-rentals), campsites
  (campsite-search) or travel times (google-maps). No key. Read-only.
---

# Google Hotels

Prices one hotel at a time, for real dates and a real party, from Google's
own hotel page: every seller's rate with and without tax, the stay total
broken into base, taxes and fees, and the cancellation deadline each seller
shows. It does not search a town — Google's hotel list is on a path this
skill does not use — so "what's a night in Banff cost" is answered by
pricing the hotels Google Maps lists near Banff, and saying so.

**Tool location:** the `scripts/` directory next to this SKILL.md. Commands
below assume you `cd` there first; `hotels.py` also works by absolute path
from any working directory. Worked workflows and the cron template are in
`recipes.md`; how the page was reverse-engineered is in `NOTES.md`.

## Setup

There is nothing to set up. No API key, no login, no configuration.

`requests` is used when present but is **not required** — the client falls
back to `curl` and then to Python's own `urllib`, so it works in a locked-down
sandbox where pip is unavailable. (The self-check's `[offline]` group exercises
every fallback with mocked transports: `requests` absent, curl absent, curl
present but unspawnable, curl behind a proxy.) Install it only if you want its
retry handling:

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

## This skill prices hotels by id. Finding them is google-maps' job.

Run google-maps first and hand the ids across. `--full` is required — the
ids (`ftid`, `place_id`) are only in google-maps' full payload:

```bash
# "What do hotels near Canmore cost?"
python3 <google-maps>/scripts/gmaps.py search --near Canmore --query hotels --full --json > hotels.json
python3 hotels.py shortlist --ids-from hotels.json --checkin +30 --checkout +32

# "What does the Fairmont Banff Springs cost?"
python3 <google-maps>/scripts/gmaps.py search --near Banff --query "Fairmont Banff Springs" --limit 1 --full --json > f.json
python3 hotels.py quote --ids-from f.json --checkin 2026-12-31 --checkout 2027-01-02
```

`gmaps.py search` takes `--near` (the place) and `--query` (what to look for)
— it has no positional argument. For one named hotel pass `--limit 1`; for a
shortlist let Maps' own ranking and filters (`--min-rating`, `--sort`,
`--limit`) decide who is in the file, because `shortlist` prices exactly the
file's entries and nothing else.

Ids are also accepted directly, in four forms — `0x…:0x…` (ftid), `ChIJ…`
(place_id), a decimal CID, or an entity token — and convert offline, costing
no request. A pasted Google Hotels link carries the entity token in its path
(`/travel/hotels/entity/<token>`); `quote` and `watch` also take it via
`--token` (the only way to price a vacation rental). A bare hotel **name is refused**
(exit 2) with the exact `gmaps.py` command to run; the tool never guesses
which building you meant.

**If google-maps is not installed**, say so, and ask the user for a Google
Maps link (its URL carries the `ftid` / `place_id`) or the hotel's Google
Hotels link (`quote --token`). Do not try to search by name here — there is no
name lookup in this skill.

## Choosing a command

| The user asks | Run |
|---|---|
| "What does the Fairmont cost Dec 31–Jan 2 for two of us — with tax?" | gmaps `search --near Banff --query "Fairmont Banff Springs" --limit 1 --full --json > f.json`, then `quote --ids-from f.json --checkin 2026-12-31 --checkout 2027-01-02` |
| "Who sells it cheapest / can I cancel free?" | the same `quote` — sellers and deadlines are in it |
| "What do hotels near Canmore cost Oct 14–16?" | gmaps `search --near Canmore --query hotels --full --json > h.json`, then `shortlist --ids-from h.json --checkin 2026-10-14 --checkout 2026-10-16` |
| "Which of these three is cheapest?" | gmaps each name (`--limit 1 --full --json`), then `shortlist --ids-from a.json --ids-from b.json --ids-from c.json --checkin … --checkout …` |
| "Which of them has free Wi-Fi?" | `shortlist … --amenity wifi:free` |
| "Is any check-in day in the first three weeks of October cheaper at the Samesun?" | `cheapest <id> --checkin 2026-10-01 --nights 3 --days 21` (or `--step 7` for "which weekend") |
| "Tell me when the Samesun drops under $60" | `watch <id> --checkin … --checkout … --under 60` |
| "Price this Google Hotels link" | `quote --token <token from the link> --checkin … --checkout …` |
| "What are this hotel's ids?" | `resolve <any id form>` or `resolve --ids-from f.json` (offline) |
| "Why did that fail?" | `doctor` |

Every command takes `--json`. **Always pass `--json` when you are going to
parse the output** — the human format is for showing the user.

## The six commands

Shared by every command that prices: `--checkin`/`--checkout` (`YYYY-MM-DD` or
`+N` days from today), `--adults` (default 2), `--child-age A` (repeatable),
`--currency` (default `CAD`), `--country` (default `CA`), `--max-requests`,
`--json`. The `HOTEL` argument is an id in any form (`0x…:0x…`, `ChIJ…`, a
decimal CID, an entity token); `--ids-from FILE` is a `gmaps.py search --full --json` output.

| Command | Adds | Requests |
|---|---|---|
| `resolve ID` / `--ids-from FILE` | — | 0 (offline) |
| `quote ID` / `--ids-from FILE` / `--token TOK` | `--token` prices a pasted Google Hotels link, including a vacation rental (also on `watch`) | 1 |
| `shortlist --ids-from FILE …` / `--hotel ID …` | `--limit` (default 6, max 10), `--max-price`, `--max-total`, `--min-rating`, `--min-stars`, `--free-cancellation`, `--amenity NAME[:free]` (repeatable), `--sort total\|nightly\|rating` (default `total`) | one per hotel, up to `--limit` |
| `cheapest ID` | `--nights N` (required), `--days W` (default 14, max 21), `--step S` (default 1; `--step 7` = "which weekend") | one per visited date: `ceil(W/S)` |
| `watch ID` | `--under P` (per night) **or** `--under-total P` (the stay), `--basis incl\|ex` (default `incl`), `--token` | 1 |
| `doctor` | — | 1 (a fixed hotel, +45 days, one night) |

`--ids-from` reads `results[].ftid`, falling back to `results[].place_id`.
Entries with neither are listed in `skipped[]` by name with a reminder that
`--full` is required; the command exits 2 naming the file only when **no**
entry carries an id. `quote --ids-from` expects a file with **one** id-bearing
entry — that is why the gmaps command above passes `--limit 1`; a file with
several is refused (exit 2) listing them, so the tool never quietly prices the
first of twenty. `resolve` and `shortlist` take the whole file.

`shortlist` filters run **client-side after the fetches** and cost nothing
extra, but they cannot enlarge the candidate set: `--max-price` is incl-tax
per night, `--max-total` incl-tax for the stay, `--min-rating` the guest
score, `--min-stars` the star class, `--free-cancellation` keeps only sellers
showing a deadline — a hotel stays if any seller does, and its row then
carries the cheapest such seller, which may not be its cheapest seller
overall. The output says how many were filtered out.
`--limit` bounds the candidates *fetched* (the first N across all `--ids-from`
files, in Maps' order), not the rows printed.

`watch` makes one check; cron owns the loop (`recipes.md`). `--under` is a
trigger, never a filter — it is never compared against today's price to refuse.
Only `P ≤ 0` is rejected.

### Flags that mean different things per command

- `--nights` on `cheapest` is the **stay length that slides** with the
  sweep — every row prices a different check-in *and* check-out, and each row
  carries the checkout it actually priced. On `quote`, `shortlist` and `watch`
  there is no `--nights`; give `--checkout`.
- `--max-price` on `shortlist` is **incl-tax, per night**, applied after the
  fetches; it drops rows. On `watch` the threshold is `--under`, and it never
  drops anything or refuses — it decides the exit code.
- `--basis` exists only on `watch`. `incl` (the default) compares what leaves
  the card; `ex` compares the before-tax figure, for matching a price seen on
  the hotel's own site. Under `ex`, single-figure sellers (below) are listed
  but can never fire the watch.
- `--limit` exists only on `shortlist`; passing it elsewhere is an argparse
  error (exit 2).
- `--token` exists only on `quote` and `watch`; `shortlist` and `cheapest`
  take ids. A rental token cannot be built from a Maps id, so a rental is
  reachable only through a pasted link.

### Bounds

Nights 1–30. Check-in today to 330 days out. Adults 1–8; child ages 0–17, at
most six children. `--limit` 1–10; `--days` 1–21; `--step` 1–21; visited
dates ≤ 21. `--max-requests` 1–40 (hard cap 40). `--currency` must be one of the 72
codes Google's own catalogue lists (refused before any request otherwise);
`--country` is two letters and changes formatting only. Zero or a negative
number on any flag is a usage error, never "no filter". **One room, always** —
there is no `--rooms`; to approximate two rooms, price two adults twice and
say it is an approximation.

## Things that will otherwise catch you out

**Google answers a bad stay with a real price for a different stay.** Past
dates, checkout on or before check-in, more than 30 nights and more than 330
days ahead all return a normal page priced for *today + 31, one night, two
adults*. The tool refuses those before asking, and then compares the page's
own echo of the stay (dates and occupancy) against the request; a mismatch is
exit `3` with both stays named — *"Google priced 2026-10-13→14 for 2 adults,
not 2026-12-31→2027-01-02 for 2 adults + child 5. Refusing to report it."* It
is never reported as a price and never as "no rates".

**Two prices, both real.** Every rate carries `ex_tax` and `incl_tax`. Lead
with incl-tax — it is what leaves the card — and show ex-tax in brackets:
*$1,452/night incl. tax ($1,283 before tax)*. Both come from the page; there
is no derived field, and nightly is never computed from the stay or the stay
from nightly.

**Some sellers show one figure with no stated basis.** Those rows carry
`basis: "single"` and an `amount`, with `ex_tax`/`incl_tax` null (two-basis
rows carry `basis: "both"` and no `amount`). The evidence says a single figure
is all-in, so it competes for "cheapest" on the incl-tax basis — but because
its basis is *inferred* while a two-basis row's is *stated*, it wins only when
it undercuts the best two-basis seller by **at least 1.00 in the page's
currency**; otherwise the two-basis row is cheapest and the single row is
listed as "≈ same price, basis not stated". When a single row does win, say
"single figure, treated as all-in" beside it — never "incl. tax". Under
`watch --basis ex` such rows are ignored.

**A page with no headline can still have sellers.** Google sometimes shows no
lead price while three sellers list the stay. `sellers[]` is the union of
every seller slot on the page, de-duplicated by partner id; "no rates listed"
is said only when that union is empty. `headline.seller` can be null while
`cheapest` is not.

**Google's headline rate is not the cheapest.** The hotel's own site was
cheaper than the headline in several captures. Report the cheapest seller
first, then name Google's headline and its seller when different.
`headline.seller` is found by matching the price to a seller row; when nothing
matches it is null and you say "seller not identified". When several sellers
carry exactly Google's lead figure (on the child-5 capture Booking.com and
Priceline both sit at 7,139.00), `headline.seller` is the one among them that
Google also lists in its headline rows, else the first in union order — the
price is identical either way, only the name differs. A seller is "the
hotel's own site" only when `own_site` is true (its name equals the hotel's)
— there is no "official site" label in the data.

**The stay total is Google's, not a multiplication.** `breakdown` is
`{base, taxes, fees, total}` from the page. Never compute nightly × nights —
rooms are priced unevenly across nights. If `breakdown` is null, say the total
was not provided.

**Fees.** `fees_share` is fees ÷ total. Above 0.10, put the fee amount in your
first sentence. For a vacation rental (`kind: "rental"`) quote the incl-tax
**stay** total only — the fees were a fifth of the total in the capture — and
say a rental nightly figure is not the price.

**Free cancellation is quoted as shown, with what is missing.** *"Free
cancellation until Nov 1, 4:00 PM, per the seller — year and time zone not
stated."* `shown: false` means no deadline was shown, which is **not**
"non-refundable".

**Occupancy is part of the price.** Two adults and a five-year-old took one
hotel from $1,283 to $7,139 a night and cut the sellers from 11 to 2. Every
report restates the party **from the echo** (`query.echoed`): "for 2 adults",
"for 2 adults and a child aged 5".

**There is no price verdict for hotels.** When asked whether a price is good,
say the tool has no verdict, then offer what it can establish: another seller
is $X cheaper (`quote`); another check-in day saves $Y (`cheapest`); this is
the Nth cheapest of N checked (`shortlist`). Never "typical", "usual" or
"rip-off" as the tool's judgement.

**`shortlist` is not a market search.** It prices exactly the hotels you
handed it — typically google-maps' top results near a place. Say "cheapest of
the 6 checked near Canmore", never "cheapest in Canmore"; the human header
already reads that way and `candidates_considered` is the N. A budget motel
Maps ranks 30th is invisible unless google-maps was asked for more. Use
google-maps' own filters first (rating, walking distance) — that is what
`--ids-from` is for.

**Amenities are three-state, and the tool always says which.** Google lists
each amenity as *has* (`has: true`), or *explicitly lacks* (`has: false` —
Samesun's pool renders as "No pools"), with `qualifier` `"free"`,
`"extra_charge"` or `"24h"` where it says so; an amenity a hotel does not list
at all is **unknown**, never "no". The name table was derived from two
captured properties and is marked provisional: ids it does not know are
carried with `name: null` and counted in `amenities_unnamed` ("+N unnamed" per
group in the human output). `--amenity NAME` is matched against the table
case-insensitively with spaces, hyphens and punctuation folded (`wifi`,
`wi-fi` and `Wi Fi` all mean "Wi-Fi"); `:free` requires the free qualifier;
a name the table does not know is exit `2` listing the known names.
`shortlist --amenity wifi:free` keeps hotels that
have it on those terms and always prints all four counts: *"6 checked: 3 have
free Wi-Fi, 1 has it on other terms, 1 is listed as not having it, 1 does not
list it."* Only `has` passes; `has_other_terms` (has it, but not free) and
`not_listed` are never described as lacking. An amenity Google lists as absent
is printed as "listed as not having: <name>" (or Google's own negated label
when the table records one). Four `highlights[]` per hotel are Google's own
chips. A rental lists its amenities by name, ungrouped.

**"Sold out" is never the answer.** The phrase exists nowhere in the data.
Empty rates with a matching echo are "no rates listed for these nights" —
exit `1` — and a watch keeps waiting on that, because a cancellation is
exactly what it is waiting for.

**Availability is not bookability.** A rate is what a seller advertised to
Google at fetch time. Say "listed at", never "available for". The tool prints
no booking links; the only URL it reports is the hotel's own website.

**A currency Google did not price in is refused, not relabelled.** The page's
own currency is checked against `--currency` after the fetch; a mismatch is
exit `2` naming the currency Google used.

**Nothing is cached — not prices, not ids.** The lead moved $1,283 → $1,198
→ $1,053 between fetches minutes apart. The tool writes nothing to disk.

**Request budget.** Defaults: `quote` and `watch` 2 (one page plus a spare),
`doctor` 1, `resolve` none (offline), `shortlist` 11 (up to ten pages plus a
spare — refused up front if `--limit` exceeds `--max-requests`),
`cheapest` one per visited date plus 3, capped at 40. Hard cap 40. Every
attempt is charged before it is made; 2.5 s between hotel pages, which are
1.6–4.3 MB each. A default `shortlist` is six pages and roughly 20 s, plus
whatever google-maps spent finding them; a maximum one is about 45 MB. A plan
over the ceiling is refused before the first request, exit `2`. Run at most
two shortlists per turn on claude.ai.

**Blocking looks like success.** An HTTP 200 without the page's currency
block is a block or consent page → exit `3`. A redirect is never followed →
exit `3` with the target named. (`recaptcha` appears on every healthy page,
so no phrase list is used.)

## Exit codes

Identical under `--json`. **Only `1` means "keep waiting".**

| Code | Meaning |
|---|---|
| `0` | found: `resolve` ≥1 id converted; `quote` ≥1 seller in the union; `shortlist` ≥1 hotel priced and passing the filters; `cheapest` ≥1 date priced; `watch` a seller at or under the threshold; `doctor` the fetch succeeded |
| `1` | the query worked and there is nothing: `quote` echo matched and no seller lists the stay; `shortlist` no row survived the filters (`rows == []`) and no fetch failed — hotels priced but filtered out still count in `priced`; `cheapest` every visited date empty, none failed; `watch` above the threshold **or** no rates listed. `resolve` and `doctor` never exit 1 |
| `2` | refused — before any request unless noted: check-in in the past, checkout on or before check-in, more than 30 nights, more than 330 days out, party out of bounds, a currency not in the catalogue, a bare name, `--limit` over the budget, a plan over `--max-requests`, a bad flag (argparse errors under `--json` are the JSON error object); **after** one request: an unknown id (Google served an error page), or a currency Google did not price in; on `shortlist`, no candidate carried an id; on `watch`, any of these — the job is dead, stop it |
| `3` | the check failed and says nothing about the price: network, a block or consent page, a redirect, a page whose layout changed (more than a quarter of the seller rows unparseable), **a page that priced a different stay than asked**, and a `shortlist` or `cheapest` with no surviving row and at least one failed fetch |

A watch loop keeps polling only on `1`. `3` says nothing about the price;
`2` means every future run fails the same way — stop the job. Ctrl-C exits
130. An unexpected exception is one stderr line and exit `3`, never a
traceback and never `1`.

On `shortlist` and `cheapest` a candidate or date whose page priced a
different stay counts as **failed** (reason `"priced a different stay"`),
exactly like a transport failure — never as empty.

## JSON output

Every command returns a single object: `{"schema_version": 1, "ok": true,
"command": "<name>", "requests_used": N, …}` on success — `requests_used` is
on every command — and on failure **only** `{"schema_version": 1, "ok":
false, "error": "…"}`, except `doctor`, whose failure object also carries every
report key it collected, so the diagnostics survive. No command returns a bare
array. Branch on `ok` (or the exit code) before reading anything else.

`query` carries `input` (what you typed) and `input_kind` (`"ftid"`,
`"place_id"`, `"cid"`, `"token"` or `"ids-from"`). On the commands that price,
`query.requested` is what you asked; `query.echoed` is what Google priced;
`query.echo_matched` is always `true` in a successful object (a mismatch never
produces one); `query.hotel` carries the ids (`name, ftid, place_id, cid,
token`), and on `shortlist` it is `query.hotels[]`, one per input. `resolve`'s
`query` is `{input, input_kind, files}` — no `requested`/`echoed`.

Price objects are `{"ex_tax": float|null, "incl_tax": float|null, "basis":
"both"|"single", "amount": float (single rows only), "currency": "CAD"}`.
There are no derived price fields.

| Command | Top-level keys beside `schema_version` / `ok` / `command` |
|---|---|
| `resolve` | `query`, `hotels[]` (`name`, `ftid`, `place_id`, `cid`, `token`, `kind`, `lat`, `lng`, `rating`, `reviews`, `address` — `rating` and `reviews` as carried by the gmaps file; google-maps publishes no review count, so `reviews` is null in phase 1), `skipped[]` |
| `quote` | `query`, `hotel`, `stay`, `occupancy`, `currency`, `cheapest`, `near_tie`, `headline`, `breakdown`, `fees_share`, `sellers[]`, `rating`, `highlights[]`, `amenities[]`, `amenities_unnamed`, `rental`, `unparsed_rows`, `requests_used` |
| `shortlist` | `query`, `source`, `place`, `candidates_available`, `candidates_considered`, `priced`, `filtered_out`, `amenity_counts`, `rows[]`, `unpriced[]`, `failed[]`, `skipped[]`, `sort`, `requests_used` |
| `cheapest` | `query`, `hotel`, `nights`, `occupancy`, `currency`, `window`, `rows[]`, `days_visited`, `days_priced`, `days_empty`, `days_failed`, `cheapest_is_reliable`, `best`, `requests_used` |
| `watch` | `query`, `hotel`, `stay`, `occupancy`, `currency`, `threshold`, `cheapest`, `compared`, `fired`, `observed_at`, `requests_used` |
| `doctor` | `python`, `requests`, `transport`, `tls_path`, `host_reachable`, `healthy_page`, `echo_matched`, `currency`, `hotel`, `requests_used`, `elapsed_s` |

Inside `quote`: `hotel` is `{name, kind: "hotel"|"rental", kind_code, ftid,
place_id, cid, token, star_class{label, stars}|null, address, phone, website,
checkin_time, checkout_time, lat, lng}`;
`stay` is `{checkin, checkout, nights, days_ahead}`; `occupancy` is `{adults,
child_ages, rooms: 1}`; `cheapest` is `{seller, partner_id, nightly, stay,
free_cancellation}`; `near_tie` is the single-figure seller within 1.00 of
`cheapest` ("≈ same price, basis not stated"), or null; `headline` is
`{seller|null, partner_id|null, nightly,
stay, matched_row}`; `breakdown` is `{base, taxes, fees, total}|null`; each of
`sellers[]` is `{seller, partner_id, own_site, nightly, stay,
free_cancellation{shown, deadline_text|null, raw}, rooms[]}`; `rating` is
`{score, reviews, histogram[[stars, pct, count]…], sources[{name, score,
scale, count}]}`; each of `highlights[]` is `{id, name, qualifier,
qualifier_raw}`; each of `amenities[]` is `{id, name|null, has, qualifier|null,
qualifier_raw|null, negated_label|null, group|null}`; `rental` is `{sleeps,
bedrooms, bathrooms, beds}|null`.

Inside `shortlist`: `source` says where the candidates came from —
`"ids-from"` or `"hotel"` (phase 2 adds `"search"`); `place` is the gmaps
file's `from` centre `{lat, lng, label}` (`label` is gmaps' resolved name,
else its query) or null; `candidates_available` is how many ids the inputs
held before `--limit`, `candidates_considered` how many were fetched;
`amenity_counts` is `{has, has_other_terms, lacks, not_listed, by_amenity}`
(`by_amenity` repeats the four counts per `--amenity` asked) or null when no
`--amenity` was given; each of `rows[]` is
`{hotel{name, cid, token, star_class, lat, lng}, distance_km|null,
cheapest{seller, own_site, nightly, stay}, near_tie, sellers_listed,
free_cancellation: bool|null, rating{score, reviews}, highlights[],
amenity_match}` — `sellers_listed` is the size of that hotel's seller union,
`near_tie` as on `quote` — with `amenity_match` one of `"has"`,
`"has_other_terms"`, `"lacks"`, `"not_listed"` or null;
`unpriced[]` is `{name, cid}` (echo matched, no rates); `failed[]` is `{name,
cid, reason}`.

Inside `cheapest`: `window` is `{start, end, step}`; each of `rows[]` is
`{checkin, checkout, weekday, cheapest|null, headline|null, breakdown|null,
failed, reason|null}` with `weekday` three-letter English (`"Tue"`); `best` is
`{checkin, checkout, weekday, stay_incl_tax, stay_basis, seller}` or null —
`stay_basis` is `"both"` or `"single"`, so a winning single-figure total is
labelled as such.

Inside `watch`: `threshold` is `{value, basis: "incl"|"ex", per:
"night"|"stay"}`; `cheapest` is `{seller, nightly, stay}` or null; `compared`
is the figure actually held against the threshold (null when no seller had a
comparable figure on the chosen basis); `observed_at` is ISO-8601 UTC.

Shape notes, each a place a naive reader breaks:

- `resolve` is the only command without `stay`, and the only one whose list
  is `hotels[]`; it never makes a request. It exits 2 with a failure object
  (`ok: false`) when no entry carries an id — `hotels[]` is never empty in a
  success object.
- `doctor` has no `query`, and its failure object is the one exception to the
  three-key rule (see above).
- A rental (`quote --token` on a kind-2 token) has `star_class: null`,
  `highlights: null`, `amenities[]` from its own named list with `group:
  null`, `rental` populated, and empty `sellers[].rooms`.
- Single-figure seller rows carry `amount` + `basis: "single"` and null
  `ex_tax`/`incl_tax`; two-basis rows carry `basis: "both"` and no `amount`.
  The same applies to each row's `stay` object.
- `breakdown` can be null while `cheapest` is not (a row priced, no total
  provided); the human total line then says "not provided".
- `headline.seller` is null when no seller row's price matches Google's lead;
  `headline.nightly.incl_tax` is null for a single-figure headline.
- `cheapest_is_reliable` is false whenever `days_failed > 0` — do not say
  "cheapest" then.
- `amenity_counts` and `rows[].amenity_match` are null unless `--amenity` was
  passed.

## Reporting back

- Lead with the number, its basis and who sells it: *"Cheapest: $1,452.38 a
  night including tax ($1,283 before tax) from dealbase.com, for two adults —
  $2,905 for your two nights with taxes and fees. Google's headline is the
  same seller. BusinessHotels.com shows $1,452.30 as a single figure, basis
  not stated — about the same."* A single-figure seller is named as cheapest
  only when it undercuts the best stated-basis seller by at least a dollar,
  and then as "single figure, treated as all-in".
- Name Google's headline when it differs from the cheapest, and the fee line
  when `fees_share > 0.10`.
- Quote the cancellation deadline as shown and say what is not stated.
- Restate the party from the echo, every time.
- For `shortlist`, say how many were checked, how many were filtered out and
  how many had no rates *before* saying which is cheapest — and say "of the N
  checked near X", never "in X". "Cheapest" means the cheapest **stay** (the
  default `--sort total`, and what the header line names); fees can make the
  cheapest night a different hotel, so say which basis you mean.
- For `cheapest`, volunteer the weekday pattern; withhold "cheapest" when
  `cheapest_is_reliable` is false and say how many days failed.
- Pass a refusal on as a refusal, with its reason; never soften a `3` into
  "nothing available", and never present a `2` as "no hotels found".
- Say when the answer is incomplete: `unparsed_rows > 0`, `failed[]`
  non-empty, or `skipped[]` non-empty all change what it means.
- Never paste JSON. Never print a booking link. Close with: the price is what
  was listed at the time; this tool cannot book.

## Self-check

```bash
cd <path-to-this-skill>/scripts
python3 test_hotels.py --offline   # logic only, against saved pages, no sockets
python3 test_hotels.py             # adds a live group against www.google.com
```

A `[network]` failure names the host, so "Google is blocking this IP" stays
distinguishable from "the skill is broken". Run it if results ever look
implausible: the page is a positional array with no field names, so the
failure to fear is a *plausible wrong answer* — a shifted index reads a
before-tax figure as after-tax — and the offline group asserts exact values
against real captured pages to catch exactly that.
