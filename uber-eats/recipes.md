# Recipes

Worked end-to-end workflows. `SKILL.md` is the interface contract; this file is
loaded when a task needs one of these shapes.

All examples assume:

```bash
cd <path-to-this-skill>/scripts
```

Every command here is relative to the delivery address in `--at`. The first
command that sees a new address spends two requests resolving it and caches
the result for 30 days; every later command with the same text spends none.
`locate` prints a **token** you can pass to `--at` instead of the text, which
pins the exact coordinates and is what a cron job should use.

---

## 1. "What deals are near me tonight — and on which dishes?"

Two steps: the feed says which stores carry a deal badge, the menus say which
dishes it applies to.

```bash
python3 ubereats.py deals --at "65 Front St W, Toronto" --json
```

`by_type` groups every (store, deal) pair as `bogo`, `percent`, `dollar`,
`free_delivery` or `other`; `stores[]` lists each store once with all its
deals, rating, ETA and distance. Narrow it the way the user did:

```bash
# BOGO only, from places rated 4.5+
python3 ubereats.py deals --at "65 Front St W, Toronto" --type bogo --min-rating 4.5 --json

# "$5 off $20+" is fine, "$5 off $50+" is not: keep minimum spends of $20 or less (and deals with none)
python3 ubereats.py deals --at "65 Front St W, Toronto" --type dollar --min-spend-at-most 20 --json
```

A badge like "20% off select items" names no dishes. Two ways to get them:

```bash
# One store: its menu, discounted or BOGO dishes only, with sale and was prices
python3 ubereats.py menu "Albert's Real Jamaican Foods" --at "65 Front St W, Toronto" --deals --json

# Every store at once: one extra request per store, so cap it
python3 ubereats.py deals --at "65 Front St W, Toronto" --type percent --limit 8 --items --json
```

`--items` is planned before the first request: 1 feed + 8 menus = 9 (plus 2
for a fresh address). With `--items` the default `--limit` drops from 30 to
10 so the plan stays under the hard cap of 25; ask for more and it exits 2
having sent nothing. Each store then carries `dishes[]` with `price`, `was`
and `deal`, and any menu that failed to load is in `failed[]`.

**Report it as:** the deal types found and how many stores carry each, then
the two or three best by rating or distance with the specific dishes and
their sale price against `was`. Always add that these are public deals only
— Uber One and account offers aren't visible — and that fees are added at
checkout. Say "of the N stores Uber showed near <address>", because the feed
is a page of a list, not the market.

---

## 2. "Where's the cheapest butter chicken I can get delivered here?"

There is no dish search on the API, so `compare` reads the nearest menus and
looks for the dish by name.

```bash
python3 ubereats.py compare "butter chicken" --at "65 Front St W, Toronto" --json
```

Default: the feed (1 request) plus the 10 nearest stores' menus (10 requests),
ranked by the price of one, sale price if discounted. About 15 seconds. To
read more menus or fewer:

```bash
python3 ubereats.py compare "butter chicken" --at "65 Front St W, Toronto" --stores 20 --max-requests 25 --json

# Only stores that carry a deal badge, only those rated 4+, 30 minutes or less
python3 ubereats.py compare "butter chicken" --at "65 Front St W, Toronto" --deals --min-rating 4 --max-eta 30 --json
```

Four things in the JSON change what you say:

- `rows[]` are dishes whose **title** contains every word of the query,
  ranked by `price`; `weaker[]` are matches in the description only — list
  them as "also mentions", never as the cheapest.
- A BOGO dish is ranked at its full price with `second_free: true`;
  `effective_each` is the per-item figure for two. Say "second one free".
- `stores_planned` vs `stores_checked` vs `stores_returned` — "cheapest
  among the 10 nearest of the 131 Uber showed". `failed[]` names any store
  whose menu didn't load (`stores_checked` excludes them); if it's non-empty
  say so before saying "cheapest". A Cloudflare challenge mid-run is not a
  failed store: the whole command exits 3.
- Exit 1 means none of the checked menus lists it; it does not mean nobody
  near the address sells it. Offer to widen `--stores` or try another name
  ("murgh makhani").

**Report it as:** the price, the dish as listed, the store, rating and ETA;
then the runner-up; then what was checked. Prices are as listed just now,
fees on top.

---

## 3. "Is Blondies Pizza on Uber Eats, and what does a large with bacon come to?"

`find` answers the first half; `item` the second. A name in the `STORE` slot
resolves itself when exactly one store matches, so `item` alone is enough
when the name is distinctive:

```bash
python3 ubereats.py find "Blondies Pizza" --at "65 Front St W, Toronto" --json
python3 ubereats.py item "Blondies Pizza" "Custom Pizza 16" --at "65 Front St W, Toronto" --json
```

`find` folds case, accents and punctuation and scores each candidate `exact`,
`all_tokens` or `prefix`. **A miss is exit 1 with the sentence "not among the
M stores Uber's feed returned near <address> — this does not mean it isn't on
Uber Eats".** Ask the user for the restaurant's Uber Eats link; the URL's last
path segment is the store id and every `STORE` slot accepts it:

```bash
python3 ubereats.py item https://www.ubereats.com/ca/store/blondies-pizza/dsBvRefCUxCkYNXV2TL2eg "Custom Pizza 16" --at "65 Front St W, Toronto" --json
```

If the dish name matches several menu entries, `item` exits 2 listing them
with prices — pick one and re-run with its exact title. Then, in the JSON:

- `price` is the base; `was` the original if it's on sale.
- `groups[]` each say `required` and `min`–`max`. Blondies' 16" carries eight:
  a required free sauce choice, then extra meats (+$5.75 bacon, +$4.60
  sausage), cheeses, vegetables, dips, salads, drinks — each with a `price`
  and `sold_out`.
- `from_price` is base + the cheapest required picks: $23.00 here, because the
  only required group is free. There is no "max price" — most groups allow
  several of each.

**Report it as:** "$23.00 for the 16" large; bacon is +$5.75, so $28.75 as
built — before delivery and service fees, which Uber only shows at checkout."
Never total in a fee; say Uber's published restaurant service fee range
($2.50–$6.50) only if asked what fees look like.

---

## 4. Watch a restaurant and act when it opens (or a dish goes on sale)

`watch` is one check, designed for cron. Exit `0` means the condition is met.
Five conditions, exactly one per run:

```bash
python3 ubereats.py watch "Blondies Pizza" --at "65 Front St W, Toronto" --open --json
python3 ubereats.py watch "Blondies Pizza" --at "65 Front St W, Toronto" --deal --json
python3 ubereats.py watch "Tikka by LTH" --at "65 Front St W, Toronto" --item "Butter Chicken Hefty Bowl" --under 12 --json
python3 ubereats.py watch "Tikka by LTH" --at "65 Front St W, Toronto" --item "Butter Chicken Hefty Bowl" --deal --json
python3 ubereats.py watch "Himalayan Kitchen" --at "65 Front St W, Toronto" --back-in-stock "Samosa Chaat" --json
```

`--under` is compared against the dish's **sale** price and is a trigger, not
a filter — a threshold below today's price is the normal case. The human line
states what was observed and when: *"Still closed at 14:02 (opens 16:30)"*,
*"$14.99, threshold $12 — not yet"*.

Pin the job to a store id and a location token, not to names and text:

```bash
python3 ubereats.py locate "65 Front St W, Toronto" --json     # → "token": "eyJhZGRy…"
python3 ubereats.py find "Blondies Pizza" --at "65 Front St W, Toronto" --json   # → "uuid": "76c06f45-…"
```

A name costs a feed request on every run, only proceeds on an exact or
all-words match (a prefix-only match exits 2 listing it), and can start
matching two stores the day a second branch opens; the token pins the coordinates so distance,
ETA and `within_range` stay comparable. The job has to act on exit `0`; a job
that only appends to a log never tells anyone. `&&` runs the alert only on
`0`; put whatever reaches the user in a small script at the path shown:

```cron
*/30 16-23 * * * cd /path/to/uber-eats/scripts && /usr/bin/python3 ubereats.py watch 76c06f45-e7c2-5310-a460-d5d5d932f67a --at eyJhZGRy… --open --json >> "$HOME/ue-watch.log" 2>&1 && "$HOME/bin/ue-alert"
```

Four rules for that line:

- **One physical line.** Crontab has no continuation; anything longer goes in
  a wrapper script.
- **Absolute `python3`** (`command -v python3` shows it) — cron runs with a
  minimal `PATH`.
- **An id and a token, never a name or an address string.** See above.
- **Bound the hours.** `*/30 16-23` checks every half hour through the evening;
  a store that opens at 16:30 does not need polling at 3 a.m., and every check
  is a real request to Uber.

**The exit codes are the whole point.** Only `1` means "not yet, keep
waiting". `3` means the check failed — Cloudflare or Uber's reCAPTCHA
defense served a challenge, the network is down, the payload changed shape
— and says nothing about the
store; a loop that treats `3` as `1` will poll a challenge page all night. `2`
means the job is dead — the dish isn't on the menu, the id is unknown, the
address isn't served — and every future run fails the same way; **disable the
job on a `2`**. A wrapper that does that:

```bash
#!/bin/sh
# ue-watch.sh — run by cron; disables itself on a dead configuration.
cd /path/to/uber-eats/scripts || exit 3
/usr/bin/python3 ubereats.py watch 76c06f45-e7c2-5310-a460-d5d5d932f67a --at eyJhZGRy… --open --json >> "$HOME/ue-watch.log" 2>&1
status=$?
if [ "$status" -eq 0 ]; then "$HOME/bin/ue-alert"; fi
if [ "$status" -eq 2 ]; then crontab -l | grep -v ue-watch.sh | crontab -; fi
exit "$status"
```

Every 30 minutes is plenty for an opening; every few hours for a price;
**never more often than every 15 minutes.** Uber's own bot defense blocked
the feed and store endpoints for hours after roughly 110 requests from one
IP in a day, and each check is one of those requests. Hammering it is how
you start getting `3`s that last all afternoon.

---

## 5. Pickup or delivery?

`--pickup` re-runs any priced command in pickup mode: the store's pickup ETA
and pickup availability, and the menu as Uber prices it for pickup.

```bash
python3 ubereats.py menu "Himalayan Kitchen" --at "65 Front St W, Toronto" --json > delivery.json
python3 ubereats.py menu "Himalayan Kitchen" --at "65 Front St W, Toronto" --pickup --json > pickup.json
```

On the one store this was tested on, all 98 dishes were priced identically in
both modes and the difference was the ETA: 18–28 minutes to collect against
52–73 to deliver, 4.6 km away. Two things to keep straight:

- **Don't promise equal prices.** One store is evidence, not a rule; compare
  the two files' `price` per `uuid` and say what you found for *this* store.
- **Fees are the real difference and they are invisible here.** Pickup has no
  delivery fee; the API shows neither fee in either mode. Say "pickup avoids
  the delivery fee, which Uber only shows at checkout" rather than a number.

`compare --pickup`, `item --pickup` and `watch --pickup` work the same way;
`nearby`, `find` and `deals` are delivery-only. `within_range: false` on a
delivery menu means the store won't deliver to this address; re-run `menu`
with `--pickup` before telling the user it's unavailable.

**Report it as:** the two ETAs side by side, whether the menu prices differed,
the distance, and the fee caveat.

---

## 6. Reading a shifting feed honestly

The feed behind `nearby`, `find` and `deals` is a page of stores Uber chose
for that address at that moment. Two captures 17 minutes apart, same address:

| | First | Second |
|---|---|---|
| Stores with a deal | 143 | 131 |
| Stores in both captures | 106 | |
| Deals unchanged among those | 130 of 131 | |
| Order of the first 20 stores | different | |

So deals are stable on a minutes scale; **membership and order are not**.
What that means in practice:

- **Say "of the N Uber showed near X", never "near X there are N".**
  `stores_returned` is N; `exhaustive` is always false. The human header
  already reads "N of the M stores Uber returned".
- **A `find` miss is not absence.** Before saying a restaurant isn't on Uber
  Eats: try `--pages 3` (each page is one more request and roughly 80 more
  stores), try a shorter name, then ask for the link.

  ```bash
  python3 ubereats.py find "Rorschach" --at "65 Front St W, Toronto" --pages 3 --json
  ```

- **"No BOGOs near you" is never the answer.** Say "none among the N stores
  Uber showed just now" and offer to page further or check again later.
- **Don't diff two runs as if they were the same list.** A store that
  appeared in the first run and not the second did not close; it fell off the
  page. If the user wants to track one store, use `menu` or `watch` with its
  id — the store page is stable; the feed's window onto it is not.
- **Distances are the one stable thing.** `distance_km` is computed from the
  address to the store's pin, so "the 10 nearest" is reproducible even when
  Uber's order is not — which is why `compare` sorts by it, not by feed order.

Two feeds do not tell you how deals move across a whole day. Until that is
measured, "as listed just now" is the honest tense for every deal.

---

## Notes for Claude on the web

The skill needs outbound HTTPS to `www.ubereats.com`. If the environment has
no network egress, the first command fails with exit `3` and a message naming
the host — that is the environment, not a bug in the skill, and no retry or
different flag will fix it. Say so plainly.

Uber's website sits behind Cloudflare's JavaScript challenge; its JSON API
under `/_p/api/` did not challenge 44 requests from a residential IP, but a
datacenter IP may be treated differently. A challenge is exit `3` with a
message naming Cloudflare, and `doctor` shows which of the three steps
(address, feed, store) is blocked. Retrying immediately does not help; waiting
sometimes does. The skill never tries to defeat the challenge.

`requests` is optional: the client falls back to `curl` and then to `urllib`,
and all three share the same budget and 1 s throttle. A feed page is about
2.5 MB and a store page 250–500 KB, parsed in memory. A `nearby` is a few
seconds; a default `compare` about 15 s; `deals --items` over 20 stores about
25 s. Run at most two `compare`s per turn.
