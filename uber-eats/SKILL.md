---
name: uber-eats
description: >-
  Check Uber Eats restaurants near an address via a bundled Python CLI — what's
  open and how fast it delivers, a restaurant's full menu with prices and sale
  prices, one dish's options and add-on prices, every public deal nearby (BOGO,
  % off, $ off, $0 delivery), and where a dish is cheapest across the nearest
  restaurants. Use whenever the user asks what's on a menu, what something
  costs on Uber Eats, what deals or BOGOs are near them, whether a place is
  open or delivering, or wants a watch on a restaurant opening, a deal
  appearing or a dish dropping in price. It cannot see delivery/service fees
  for an order, Uber One or account-only deals, or order anything. No key, no
  login. Read-only.
---

# Uber Eats menus, prices and deals

Reads Uber Eats' own web JSON API — the calls the website makes when you set
a delivery address and open a restaurant — with no account, no key and no
browser. Everything is relative to a **delivery address** (`--at`):
restaurants, ETAs, distance and deals all change with it, so say which
address you used.

**Tool location:** the `scripts/` directory next to this SKILL.md. Commands
below assume you `cd` there first; `ubereats.py` also works by absolute path
from any working directory. Worked workflows and the cron template are in
`recipes.md`; how the API was reverse-engineered is in `NOTES.md`.

## Setup

Nothing to set up. No API key, no login, no configuration.

`requests` is used when present but is **not required** — the client falls
back to `curl` and then to Python's own `urllib`. Install it only if you want
its retry handling:

```bash
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
python3 scripts/ubereats.py --help
```

**claude.ai (web):** needs Code execution *and* network egress enabled in
Settings → Capabilities, with `www.ubereats.com` allowed (or "All domains").
Start a new conversation after changing it. The API `code_execution` tool has
no outbound network and can never run this skill.

## Choosing a command

| The user asks… | Run |
|---|---|
| "What's near me / open now / fast?" | `nearby --at "<address>"` (+ `--max-eta 30`, `--min-rating 4.5`, `--deals`, `--max-km 3`) |
| "Is <restaurant> on Uber Eats here?" | `find "<name>" --at "<address>"` |
| "What's on <restaurant>'s menu / what does X cost there?" | `menu "<name or link>" --at "<address>"` (+ `--match`, `--under`, `--deals`, `--section`) |
| "How much with extra cheese / as a large?" | `item "<restaurant>" "<dish>" --at "<address>"` |
| "Any deals / BOGOs near me?" | `deals --at "<address>"` (+ `--type bogo`, `--items` to list the dishes) |
| "Where's the cheapest <dish>?" | `compare "<dish>" --at "<address>"` |
| "Tell me when it opens / goes on sale / is back" | `watch "<restaurant>" --at "<address>" --open` (or `--deal`, `--item X --under N`, `--item X --deal`, `--back-in-stock X`) |
| "Where exactly is that address?" / a token to reuse | `locate "<address>"` |
| Anything returns exit 3 | `doctor` |

Every command takes `--json`. **Always pass `--json` when you are going to
parse the output** — the human format is for showing the user.

There is **no cuisine or keyword search** — Uber's search endpoint is behind
its bot protection and the feed ignores a query. For "Thai food near me", run
`nearby` and pick by name, or `compare "pad thai"` to read the nearest menus.

## The nine commands

Shared flags: `--at ADDRESS|TOKEN` (delivery address as text, or a location
token printed by `locate`), `--json`, `--max-requests N` (lowers the
ceiling; hard cap 25), `--locale CC` (default `ca`; every other Uber Eats
country is attempted but **unverified**). `--pickup` (price and ETA for
pickup instead of delivery) exists on `menu`, `item`, `watch` and `compare`
only — `nearby`, `find` and `deals` read the delivery feed and report
`mode: "delivery"`. `STORE` is a store UUID, an Uber Eats store link or its
22-character id segment (both convert offline, no request), or a restaurant
**name** — a name runs `find` first and proceeds only on a confident match:
exactly one `exact` match, or no exact and exactly one `all_tokens` match. A
single prefix-only match is **not** confident. Anything else exits 2 listing
the candidates with their UUIDs.

| Command | Adds | Requests (the plan) |
|---|---|---|
| `locate "<address>"` | `--pick N` (which candidate, default the first) | 2 |
| `nearby --at A` | `--deals`, `--deal-type bogo\|percent\|dollar\|free-delivery\|other`, `--min-rating`, `--max-eta` (on the ETA's upper bound), `--max-km`, `--name TEXT`, `--sort feed\|rating\|eta\|distance`, `--limit` (default 25), `--pages N` (default 1, max 5) | location + 1 per page |
| `find "<restaurant>" --at A` | `--pages` | location + 1 per page |
| `menu STORE [--at A]` | `--pickup`, `--under P`, `--match TEXT`, `--section TEXT`, `--deals`, `--sold-out` (include; hidden but counted by default) | location + name resolution (1 if STORE is a name) + 1 |
| `item STORE "<dish>" [--at A]` | `--pickup` | location + name resolution + 2 (menu, then options) |
| `deals --at A` | `--type`, `--min-rating`, `--max-eta`, `--max-km`, `--min-spend-at-most P`, `--limit` stores (default 30; **10 with `--items`**), `--items`, `--pages` | location + 1 per page; `--items` adds 1 per store shown |
| `compare "<dish>" --at A` | `--pickup`, `--stores N` (default 10, max 24), `--name`, `--min-rating`, `--max-eta`, `--deals` | location + 1 + N menus |
| `watch STORE [--at A] <condition>` | `--pickup`; exactly one of `--open`, `--deal`, `--item "<dish>" --under P`, `--item "<dish>" --deal`, `--back-in-stock "<dish>"` | location + name resolution + 1 (one menu read; the dish is matched on it, never a second request) |
| `doctor` | — | 3 (place search, feed, one menu) |

"Location" costs 0 requests when `--at` is a token or an address resolved in
the last 30 days, else 2 (address candidates, then coordinates). `menu`,
`item` and `watch` work **without** `--at`: the menu and prices come back, but
`distance_km`, `eta`, `pickup_eta` and `within_range` are null and the header
says "no address given — ETA and distance not shown". A **name** in `STORE`
does need `--at` (it is looked up in the feed); a UUID or link does not.

`watch` makes one check; cron owns the loop (`recipes.md`). `--under` is a
trigger, never a filter: a threshold below today's price is the normal case.

## Things that will otherwise catch you out

- **The restaurant list is not the whole market.** `nearby`, `find` and
  `deals` see the stores Uber's feed returned for that address (about 100–150
  on the first page, 80 more per `--pages`), and both the membership and the
  order of that set shift between calls minutes apart. The human header
  already reads "N of the M stores Uber returned near <address> — Uber's
  list, not the whole market"; keep that framing. A `find` miss is **not** proof the restaurant isn't on Uber
  Eats — ask for its Uber Eats link, which `menu`, `item` and `watch` accept
  directly.
- **A sale price is the price.** On a discounted dish the number shown is
  already reduced; `was` is the original, taken from the struck-through figure
  in Uber's own markup. Quote both: "$10.88 (was $14.50, 25% off)". A row with
  `price_unclear: true` had two figures and no strikethrough — quote `price`
  and say the original is unclear.
- **BOGO is not half price.** `compare` ranks a BOGO dish at its full price
  and flags `second_free`; say "second one free" and mention `effective_each`
  only if the user wants two.
- **"Select items" deals apply to specific dishes.** `menu --deals` shows
  which; `deals --items` does it for every store (one request each, planned
  and refused up front if over budget).
- **No fees.** Delivery and service fees exist only once there is a cart, and
  the anonymous API returns null for both. Never estimate a total. You may say
  Uber publishes a restaurant service fee of "$2.50–$6.50 per order" and quote
  a `$0 Delivery Fee` deal if one is listed; `delivery_fee_text` on a feed row
  is rare and null means "not shown", not "free".
- **Public deals only.** Uber One pricing and account offers (first order,
  member-only discounts) are invisible logged out; say so whenever you talk
  about deals. A dish `note` like "Earn $7 Uber Cash for photo" is a review
  reward, not a discount, and never a `was` price.
- **Pickup** (`--pickup`, on `menu`, `item`, `watch` and `compare`) changes
  the ETA and the store's pickup availability; prices matched delivery on the
  one store tested, but don't promise that. The feed commands are
  delivery-only.
- **A closed restaurant still has a menu** — `menu` exits 0 with
  `is_open: false` and today's hours; lead with "it's closed until 16:30".
  `within_range: false` means it won't deliver to this address; pickup may
  still work.
- **Uber counts your requests, and the limit is a day, not a second.**
  Besides Cloudflare on the HTML pages, Uber's own bot defense on the JSON
  API answered HTTP 403 with a reCAPTCHA challenge on the feed and store
  endpoints after roughly 110 requests from one IP within a day. The block
  lasts **hours**, and there is no workaround — none will be added. So keep
  runs small: `compare --stores` and `deals --items` cost a request per
  store; never poll a `watch` more often than every 15 minutes; and if
  `doctor` reports the reCAPTCHA block, stop and tell the user to try later
  rather than retrying with other flags.
- **Deals move through the day.** Two feeds 17 minutes apart agreed on 130 of
  131 deals, but a day-long sweep was not done — say "as listed just now".
- **Prices, deals, menus and open/closed are never cached.** Only resolved
  addresses are: `locations.json` for 30 days under `$UBEREATS_CACHE_DIR`,
  else `~/.cache/ueats`, else the system temp dir, else in memory only.
  Nothing else is ever written to disk.
- **Cuisine is not in the feed.** `nearby` has no `--cuisine`; `cuisines` is
  on `menu` only and mixes in tags like "Exclusive to Eats".
- **Only Canada is verified.** `--locale` is passed through for other
  countries with no evidence it works; label any such answer unverified.

## Flags that mean different things

- `--under` — `menu`: keeps dishes whose **sale** price is ≤ P; `watch --item`:
  the trigger threshold, exit 0 when the dish's sale price is ≤ P.
- `--deals` — `nearby`: stores carrying any deal badge; `menu`: only
  discounted/BOGO dishes; `compare`: narrow to stores with a deal *before*
  reading menus (spends fewer requests); `watch`: a condition (any dish on the
  menu carries a deal), or with `--item`, that dish carries one.
- `--type` vs `--deal-type` — `deals` filters with `--type`; `nearby` uses
  `--deal-type` so it can't be confused with a store type. Same values:
  `bogo`, `percent`, `dollar`, `free-delivery`, `other` (a badge the grammar
  didn't recognise, kept verbatim).
- `--limit` — `nearby`: rows shown; `deals`: stores shown. `compare` has no
  `--limit`; it takes `--stores` (menus *fetched*, each one a request).
- `--name` — `nearby`: a client-side filter on the rows; `compare`: narrows
  which stores' menus are read. `find` takes the name as its argument.
- `--max-eta` compares against the ETA's **upper** bound ("10 to 20 min" is
  20), inclusive; `--min-rating` is inclusive.
- `--pages` — on `nearby`, `find` and `deals` only; each page is one request,
  planned up front; `menu`, `compare` and `watch` do not take it.

## Budgets

Every command counts its plan before the first request (the table above);
its ceiling is that plan **plus 2 spare for retries**, capped at 25.
`--max-requests N` only *lowers* the ceiling: a plan over it is refused up
front, exit 2, with nothing sent. The JSON reports both as
`requests_planned` and `requests_ceiling`. Hard cap 25 per command, so
`compare --stores 24` fits only with a token or cached address, and `deals
--items` defaults to 10 stores for the same reason. `doctor` ignores
`--max-requests` (always three requests) and exits 0 or 3 only. Requests
are 1 s apart; a default `compare` is 10 menus of 250–500 KB each and about 15 s;
feed pages are 2.5 MB. Inside `compare` and `deals --items` a store whose
menu fails to load (bad id, payload drift, a 5xx after retries) lands in
`failed[]` and the run continues; when **every** store fails the command
exits 3. A **Cloudflare challenge** mid-run is different: it aborts the whole
command with exit 3 at once, because every further request would be
challenged too. Never follows a redirect, never retries a 403; a 5xx or a
timeout is retried (up to 3 attempts) only while the ceiling leaves room —
that is what the 2 spare are for.

**Read-only by construction.** The transport refuses any endpoint outside a
five-name allowlist (address search, address coordinates, feed, store, item)
before opening a socket. There is no cart, order, favourite or promo path.

## Exit codes

Identical under `--json`. **Only `1` means "keep waiting".**

| Code | Meaning |
|---|---|
| `0` | answered: rows, a menu, a dish's options, deals, a ranked list; for `watch`, the condition is met; `doctor` all three steps healthy |
| `1` | the query worked and there is nothing: filters excluded every row or dish; `find` matched nothing among the stores returned; `compare` found the dish on no menu; `deals` found none; for `watch`, not yet — keep polling. Under `--json` this is still a success object (`ok: true`) with empty `rows[]`/`candidates[]`/`sections[]`, or `fired: false`. `locate`, `item` and `doctor` never exit 1 |
| `2` | usage or lookup, before any request unless noted: a bad flag, a plan over `--max-requests`, `locate` with no candidates, an address Uber does not serve (`isInServiceArea: false`, after the feed), an unknown store id (Uber answered `invalid_store_uuid`, after one request), a name matching zero or several stores, a dish not found or ambiguous on `item`, a watched dish that doesn't exist — on `watch` the job is dead, stop it |
| `3` | the check failed and says nothing about the food: network error, Uber's bot protection served a challenge instead of JSON — Cloudflare's HTML page, or Uber's own bot defense answering 403 with a reCAPTCHA `botdefense` body after too many requests in a day (anywhere in a run, including mid-`compare`) — a 5xx after retries, a `failure` status other than a bad id, a payload without the fields the parser needs; `compare` or `deals --items` with every store failed; `doctor` with any step unhealthy — **never** "nothing found" |

A watch loop keeps polling only on `1`. Ctrl-C exits 130. An unexpected
exception is one stderr line and exit `3`, never a traceback and never `1`.
A Cloudflare block reads: *"Uber Eats' bot protection (Cloudflare) blocked
this request — this is not 'nothing found'. Try again later; `doctor` shows
which step is blocked."* A reCAPTCHA block names "bot defense (reCAPTCHA)"
instead, same exit 3 and `kind: "blocked"`, and means the day's quota from
this IP is spent: it clears in hours, not seconds. Pass either on as a
block, never as an empty result, and do not retry within the same turn.

## JSON output

Every command returns a single object: `{"schema_version": 1, "ok": true,
"command": "<name>", "requests_used": N, …}` on exit 0 **and on exit 1**
(nothing matched is a success object with empty lists), and on exit 2/3
`{"schema_version": 1, "ok": false, "error": "<one line>", "kind":
"usage|lookup|network|blocked|payload|internal"}` (`internal` is a
non-allowlisted endpoint — a programming error, exit 3, never sent) — an
ambiguous name adds
`candidates[]`, and `doctor`'s failure object carries its whole report so the
diagnostics survive; `compare`/`deals --items` with every store failed carry
theirs too. No command returns a bare array. Branch on `ok` (or the exit
code) before reading anything else.

Money is `{"cents": 1087.5, "amount": "10.88", "currency": "CAD"}` — raw
cents kept because Uber sends fractions; `amount` is rounded **half-even**
(Uber's own rule: 1012.5 → `"10.12"`, 1087.5 → `"10.88"`), so use it and
never re-round `cents` yourself. A deal is `{text, type, percent, amount,
min_spend, select_items, raw_type}` with `type` one of `bogo`, `percent`,
`dollar`, `free_delivery`, `other` (`other` keeps Uber's text verbatim and is
never dropped); `amount` and `min_spend` are strings in currency units.
`address` on every located command is `{line1, line2, reference,
reference_type, latitude, longitude, label}` — Uber's resolution of `--at`,
which is what the prices are relative to; `label` is the "<line1>, <line2>"
string the human header prints. It is null on `menu`/`item`/`watch` without
`--at`. `exhaustive` on `nearby`, `find` and `deals` is **always false**; it
exists so nothing treats the list as complete. `requests_planned`,
`requests_ceiling` and `requests_used` are on every success object.

| Command | Top-level keys beside `schema_version` / `ok` / `command` |
|---|---|
| `locate` | `query`, `picked` (the 1-based candidate used), `candidates[]` (`id, provider, line1, line2, location` — the token on the picked one, null on the rest), `location` (the picked candidate's full address with coordinates), `token`, `requests_*` |
| `nearby` | `address`, `mode` (always `"delivery"`), `exhaustive`, `stores_returned`, `filtered_out`, `pages`, `has_more`, `rows[]`, `requests_*` |
| `find` | `address`, `query`, `exhaustive`, `stores_returned`, `pages`, `candidates[]`, `requests_*`; on a miss (exit 1) also `message` |
| `menu` | `address`, `mode`, `store`, `matched_by_name`, `deals[]`, `sections[]` (`title`, `dishes[]`), `dishes_shown`, `dishes_total`, `dishes_sold_out`, `sold_out_hidden`, `duplicates_removed`, `hours_today[]`, `opens`, `requests_*` |
| `item` | `address`, `mode`, `store` (`uuid`, `title`, `is_open`), `matched_by_name`, `dish` (`uuid, title, price, was, deal, sold_out, section`), `groups[]`, `from_price`, `required_groups`, `requests_*` |
| `deals` | `address`, `public_only: true`, `exhaustive`, `stores_returned`, `stores_with_deals`, `pages`, `by_type` (`bogo[], percent[], dollar[], free_delivery[], other[]`), `stores[]`, `failed[]`, `requests_*` |
| `compare` | `address`, `query`, `mode`, `stores_planned`, `stores_checked`, `stores_returned`, `exhaustive`, `rows[]`, `weaker[]`, `failed[]`, `requests_*` |
| `watch` | `address`, `store` (`uuid`, `title`), `matched_by_name`, `condition`, `condition_text`, `fired`, `observed`, `checked_at`, `requests_*` |
| `doctor` | `transport`, `tls_path`, `requests_module`, `python`, `steps[]` (`name, ok, blocked, detail, transport_used, tls_path`), `requests_used` |

`requests_*` stands for `requests_planned`, `requests_ceiling` and
`requests_used`. `matched_by_name` is the store name a `STORE` argument was
matched to, or null when an id was given. A **store row** — the shape of
`nearby.rows[]`, `find.candidates[]` and `deals.stores[]` — is `{uuid, name,
rating|null, rating_count_text, eta_min, eta_max, distance_km|null, deals[],
exclusive, delivery_fee_text|null, url_id}`; `find` adds `match` (`exact`,
`all_tokens`, `prefix`); on `deals` the row's `deals[]` holds only the deals
that passed the filters, and `--items` adds `dishes[]` (null for a store
whose menu failed — see `failed[]`; an empty list means no dish carries a
deal marker). Each entry of a `by_type` list is a deal object plus
`store_uuid` and `store`. Inside `menu`: `store` is `{uuid, title, slug, address, is_open,
is_orderable, closed_message, hours{<day range>: [{start, end}]}, rating,
rating_count_text, eta, pickup_eta, distance_km, within_range, cuisines[],
phone, currency, has_store_promotion, deals[], dishes_total, dishes_sold_out,
duplicates_removed}` (the three counts are repeated top-level); `hours_today[]`
is `[{start, end}]` for the day of the check and `opens` the next opening
time `"16:30"` or null; each dish is `{uuid, title, description, section,
price, was|null, deal|null, sold_out, has_options, note|null,
price_unclear}`. Inside `item`: each of `groups[]` is `{uuid, title,
required, min, max, options[]}` and each option `{uuid, title, price,
sold_out, min, max, default, groups[]}` — nested groups recurse. Inside
`compare`: each row is `{price, was, deal, second_free, effective_each, dish,
dish_uuid, sold_out, store, store_uuid, rating, eta, distance_km}`,
`weaker[]` has the same shape for description-only matches, and `failed[]`
(also on `deals --items`) is `{uuid, name, reason}`. Inside `watch`:
`condition` is `{kind, dish|null, under|null}` with `kind` one of `open`,
`deal`, `item-under`, `item-deal`, `back-in-stock`, and `condition_text` the
human label (`"item 'X' under 12"`); `observed` is `{is_open,
is_orderable, hours_today[], opens}` plus `deals[]` for `--deal`, or `dish`
(a full dish object) and, for `--under`, `threshold`; `checked_at` is local
ISO-8601 with offset.

Shape notes, each a place a naive reader breaks:

- `menu` hides sold-out dishes from `sections[]` unless `--sold-out`, but
  `dishes_total` and `dishes_sold_out` always count them; `sold_out_hidden`
  says how many the filters hid and `dishes_shown` how many are in
  `sections[]`.
- `rating` is null for a store shown as "New"; `eta_min`/`eta_max` are null
  when Uber shows no ETA — the row is kept.
- `distance_km` on feed rows is computed from the address to the store's map
  pin; on `menu` it is Uber's own badge. Both are null without `--at`.
- `has_store_promotion` can be true while `deals[]` is empty — Uber's
  store-level promotion object is null even then; the deal is on the dishes.
- `from_price` on `item` is base plus the cheapest required picks; there is
  deliberately no "max price".
- `watch` under `--json` still exits `1` when `fired` is false.

## Reporting back

- Lead with the answer: *"Cheapest butter chicken of the 10 closest places
  Uber showed near Union Station: $14.99 at Tikka by LTH (4.6★, 25 min) — and
  it's buy one, get one free."*
- Name the address Uber resolved, and say the list covers what Uber showed,
  not every restaurant.
- For a menu, don't paste it: answer the question, then the 3–6 dishes that
  matter with price, sale price and deal.
- Quote sale prices as "$10.88 (was $14.50, 25% off)"; a BOGO as "second one
  free", never half price.
- Say what isn't included when it matters: fees, account deals, and that a
  `find` miss isn't proof of absence.
- Pass a refusal on as a refusal, with its reason; never soften a `3` into
  "nothing nearby", and never present a `2` as "not on Uber Eats".
- Never paste JSON, never print add-to-cart links, never suggest the tool can
  order. Close price answers with: prices as listed on Uber Eats just now;
  fees are added at checkout.

## Self-check

```bash
cd <path-to-this-skill>/scripts
python3 test_ubereats.py --offline   # logic only, against saved responses, no sockets
python3 test_ubereats.py             # adds a live group against www.ubereats.com
```

A `[network]` failure names the host, so "Cloudflare is challenging this IP"
stays distinguishable from "the skill is broken" — `doctor` says which step is
blocked. The failures worth fearing are the plausible wrong answers: a sale
price read as the list price, a `was` price backwards, a dish counted three
times, a BOGO ranked at half. The offline group asserts exact values against
real captured menus to catch exactly those.
