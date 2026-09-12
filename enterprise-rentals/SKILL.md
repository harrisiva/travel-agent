---
name: enterprise-rentals
description: Price Enterprise rental cars anywhere Enterprise operates - live availability, real make/model, seats, luggage, drivetrain, fuel type, mileage terms, trip totals and Enterprise Plus points prices. Use when the user wants to price or compare a car rental, find the cheapest dates across a range, compare airport vs downtown branches, check a one-way rental, work out a young-driver (under-25) surcharge, find a vehicle they can sleep in for a road trip, check branch opening or after-hours drop-off times, or watch a sold-out class. Read-only - it never books.
---

# Enterprise rentals

Prices rental cars through the same public JSON APIs the enterprise.ca
reservation flow uses. No API key, no account, no configuration. **Read-only:
it quotes and compares, and has no code path that can reserve anything.**

Worked end-to-end workflows live in **`recipes.md`** next to this file. Read it
when you need a full workflow rather than one command.

**`endpoints.md`**, also next to this file, documents the underlying API and the
traps in it. Read that only when a call behaves unexpectedly and the reason is
not obvious here.

## Setup and invocation

```bash
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
```

Preferred invocation - works from **any** working directory, so never `cd`
just to run a query:

```bash
python3 <path-to-this-skill>/scripts/enterprise.py <command> ...
```

Use `py` instead of `python3` on Windows. Do not use bare `python`.
`ModuleNotFoundError: requests` means run the install line above.

**If a pricing call fails, run `doctor` and report what it says. Never report
"no cars available" on a failure** - those are different answers:

```bash
python3 <path-to-this-skill>/scripts/enterprise.py doctor
```

**`doctor` itself makes one pricing call**, so during a volumetric block it
spends against the same limit - run it once, not in a loop.

`doctor` prints the Python and OpenSSL versions, the cache location, which
transport succeeded, and a live quote. The bundled self-check is broader:

```bash
python3 <path-to-this-skill>/scripts/test_availability.py --offline   # no network
python3 <path-to-this-skill>/scripts/test_availability.py             # full
```

## Read-only, precisely

The tool never books, reserves, pays or cancels: there is one POST, to
`reservations/initiate`, and it is a price quote. Worth stating exactly: that
call does open a **server-side quote session**, so it is not a pure read even
though it reserves no vehicle and creates no reservation. Nothing the tool can
do results in a booking.

## Choosing a command

| Question | Command |
|---|---|
| "Which branch do you mean?" / need an ID | `locations` |
| "What can I rent here, these dates, and for how much?" | `quote` |
| "Is a different week cheaper?" | `sweep` |
| "Airport or downtown? This city or that one?" | `compare` |
| "Tell me when the minivan frees up" | `watch` |
| "Can I collect at 6am? Drop off Sunday night?" | `branch` |
| Something failed and you need to know why | `doctor` |
| Clear or locate the branch cache | `cache` |

**Always resolve the branch first.** Names are ambiguous: `Halifax` matches
the airport, the train station, *and* an Exotic branch at the same airport
(`YHZ`, id 1054600) with a different fleet at much higher prices. `quote`
refuses to guess and lists the candidates; pass the numeric id to
disambiguate.

`locations` also lists **`city` rows** (`Halifax, GB`, `Halifax, VA, US`) -
geocoder place names, not branches. They have no code or currency, cannot be
quoted, and are skipped by name resolution; ignore them when choosing a
branch. The out-of-country warning counts bookable rows only, so a list of
foreign `city` rows alone does not trigger it.

## The filter flags

Every pricing command (`quote`, `sweep`, `compare`, `watch`) takes the same
filters. They run locally over the whole fleet the API returned, so combining
them costs nothing extra.

| Flag | Meaning |
|---|---|
| `--class` | `car`, `suv`, `van`, `minibus`, `cargo`, `truck` — or any word, matched against the sub-category |
| `--drive` | `awd` / `4wd` / `4x4`, or `2wd` |
| `--fuel` | `petrol`/`gas`, `diesel`, `hybrid`, `electric` |
| `--transmission` | `automatic` or `manual` |
| `--seats`, `--bags` | at least this many |
| `--max-price` | trip total at or below this |
| `--unlimited-mileage` | drop classes with a mileage cap |
| `--sleepable` | **the road-trip composite**: AWD/4WD, SUV/van/minibus, 5+ seats, unlimited mileage |

**Use `--sleepable` for "a car I can sleep in"** rather than assembling
`--class suv --drive awd --seats 5 --unlimited-mileage` by hand — it is the
same intent in one flag, and it is what the skill advertises.

`sweep` adds three of its own: `--nights` (length of each rental),
`--step` (days between consecutive windows — `--step 7` compares like-for-like
weekends rather than every date), and `--time` (pickup/return time of day,
default 10:00). `--workers` caps concurrency on `sweep` and `compare`.

**One-way rentals: `--dropoff <branch>`** on `quote`, `sweep` or `watch`.
Within one country this works normally. Across a border it is usually refused,
and the tool exits 2 saying so rather than reporting a sell-out — see the
cross-border section below.

## Flags that mean different things

- `--seats`, `--bags`, `--max-price` are **thresholds** - at least this many
  seats, at most this much money.
- **`--max-price` and `--below` are always in the branch's billing currency**,
  even under `--currency`. Passing `--currency USD --below 400` against a
  Canadian branch compares 400 against CAD, not against the USD estimate shown
  in the table.
- `--class`, `--drive`, `--fuel` and `--transmission` match the API's
  **locale-independent facet codes**, not its text, so they work unchanged at a
  German or Japanese branch where the labels come back translated. An
  unrecognised word falls back to a substring match on the (translated)
  sub-category, so `--class Jeep` still works. The table has a `TRANS` column; an unfiltered quote mixes
  automatics and manuals, so filter when the renter can only drive an auto.
- `--pickup-time` / `--return-time` accept `YYYY-MM-DD` (defaulting to 10:00)
  or `YYYY-MM-DDTHH:MM`.
- **`branch` takes `--date`, not `--pickup-time`**, and it defaults to *today*.
  Hours for today say nothing about a trip in October - always pass
  `--date YYYY-MM-DD` for the day the user actually collects or drops off.
- `--age` may be **repeated** (`--age 21 --age 25`) to price both side by
  side. Do this whenever the renter is under 25 - see below.
- `--country` picks which country's branches to search. `--residency` is the
  *renter's* country. `--currency` is display only.
- `--brand` works on `locations` (National and Alamo branches resolve fine) but
  **not on pricing** - only Enterprise's pricing host is known, so `quote`,
  `sweep`, `compare` and `watch` refuse a non-Enterprise brand rather than
  returning a misleading empty result.

## What the defaults hide

- **Age defaults to 25.** Under-25 renters pay more *and* cannot rent every
  class - at Halifax, 20 bookable classes at 25 becomes 13 at 21. Always pass
  `--age` when you know it, and prefer the repeated form so the vanished
  classes are visible.
- **`--limit` defaults to 20.** The footer says when rows were trimmed.
- **Sold-out classes are excluded from the table** but counted in the footer
  (`20 of 59 classes bookable`). "Every minivan is gone" is sometimes the
  answer. **But "sold out" and "not rentable at your age" cannot be told
  apart:** the API returns `SOLD_OUT` for age-restricted classes as well, so a
  22-year-old's forbidden classes are silently counted into the sold-out
  total. Do not tell an under-25 renter to try other dates on the strength of
  a sold-out count - re-run the same dates with `--age 25` first (see
  `--age`, repeated). Classes that reappear are age-restricted, and no change
  of date will free them. (A separate footer line lists classes the branch
  marks *restricted*, i.e. not bookable online. That line is real, but it does
  not catch the age case, so the three counts need not account for every
  class.)
- **Dates are checked locally before any pricing call** - a pickup in the
  past, a return not after the pickup, and anything beyond Enterprise's
  395-day booking horizon are refused as a usage error (exit 2) without
  touching the network, and **every fault is reported at once**, not one at a
  time. Fix them all in one pass; do not retry to discover the next one.
- **A refused age does not discard the ages that worked.** `--age 19 --age 25`
  reports the refusal for 19 and the full quote for 25.
- **`sweep` and `compare` refuse before sending** if the plan exceeds
  `--max-requests` (default 40). They print the plan first.
- **A fan-out where every request was refused exits 2, not 1.** If every date
  in a sweep (or every branch in a `compare`) is refused (age, booking
  horizon, refused route), no date in that range can work - do not retry it.
  If nothing priced but the failures are a *mix* of refusals and network/API
  errors, it exits 3 instead, and the message counts each kind: the failed
  requests say nothing about whether those dates would work.

## Coverage is worldwide

Verified live in Canada, the US, the UK, Germany, France, Spain, Ireland, New
Zealand and Japan, each priced in the branch's own currency. Pass `--country`
and a matching `--locale` (`--country DE --locale de_DE`).

**Check the country of the branch you resolve.** A plausible-looking match in
the wrong country is the commonest way this skill returns a confidently wrong
answer, and it is a class of bug, not one bad city name:

- `locations "Sydney Airport"` returns Sydney, **Nova Scotia** (`YQY`, CAD).
  Enterprise's catalogue has no Australian airport branches, and nothing in the
  name marks it as the wrong hemisphere.
- `locations "La Paz" --country BO` returns La Paz, **Mexico**.

That second one matters twice over: **`--country` does not constrain the
results.** It is a search hint - which country's catalogue to search first -
not a filter, so a branch in another country can and does come back under it.
Never treat a row as being in the country you asked for.

The tool guards this in three places, but only the first is silent-proof:

- `locations` prints a **`CTRY`** column next to `CUR`, a footer note telling
  you to check it before quoting, and a loud warning when any match falls
  outside `--country`.
- `quote`, `sweep`, `compare`, `watch` and `branch` print a `WARNING` **to
  stderr** when the resolved branch's country differs from `--country`. Under
  `--json` that warning is still on stderr, **and** the payload carries
  `country_requested` and `country_mismatch` on `quote`, `sweep` and
  `compare`, so a machine consumer can detect it without parsing stderr.
  `watch` and `branch` do **not** carry those fields under `--json` - read
  stderr for them.
- The `quote` header names the branch's city and country.

Read the `CTRY` column on the row you are about to quote, every time.

Vehicle categories come back translated (`Mietwagen`, `Kleinbusse`), but
filters match on codes, so `--class van` finds a `Kleinbusse`.

## `PER DAY` is derived, not the API's rate

The `PER DAY` column is **the trip total divided by the number of rental
days**. It is not the API's own rate line, and the footer says so on every
quote. This matters because Enterprise sometimes quotes a class at a **weekly**
rate: taking that number as a daily one understates a week's rental several
times over. A non-daily API rate is flagged in the footer.

Under `--json` the two are separate fields and must not be confused:

- `api_rate` / `api_rate_period` / `api_rate_quantity` - the raw figure exactly
  as Enterprise sent it, in whatever period it is quoted.
- `per_day_effective` - the derived per-day number the table shows.

Quote `total_charged` for the trip, and `per_day_effective` if a daily figure
is wanted. Never present `api_rate` as a daily price without checking
`api_rate_period`.

## `watch` blocks — know its pacing before you run it

`watch` polls in the foreground. Left at its defaults it makes `--checks 2`
checks `--every 30` minutes apart, giving up after `--for-minutes 60` — so a
bare `watch` can sit for half an hour between checks. `--every` has a 15-minute
floor because each check is a full ~700 KB pricing call against a
volumetrically limited host.

On claude.ai, prefer `--checks 1` and re-run later; a long block is a poor use
of a bounded session. Exit 1 means keep waiting; exit 2 means stop, because the
query can never succeed.

## Currency: charged vs estimate

`--currency` performs a **display conversion only**. The branch still bills in
its own currency. The tool prints both, and the charged figure always comes
first:

```
436.00 CAD (~315.10 USD est.)
```

In `--json` the charged amount is `total_charged`, and any conversion is
nested under `converted_estimate` with a note. **Never quote the estimate as
the price.** Telling someone a rental costs $315 when their card is billed
CAD 436 is the worst thing this skill can do.

## Pace yourself: there is a volumetric limit

The pricing host blocks an IP that makes too many quote calls in a short
period. **The threshold is unpublished and lower than it looks** — a block has
landed on the sixth call of a fresh session, because the limit is per IP and
anything else sharing that address spends the same budget. When it
does, **every** transport is refused at once and the tool exits 3.

- This is not "no cars available", and it is not a TLS problem, even though a
  block and a handshake rejection look identical from here.
- There is **no advance warning**: the API sends no `Retry-After` and no
  rate-limit headers, and latency does not rise before a block. You cannot
  pace by feedback, only by restraint.
- It clears on its own. Wait and retry; do not go hunting for OpenSSL
  configuration.
- Branch lookups keep working while pricing is blocked, so `locations` and
  `branch` still answer.

Practically: prefer one `quote` over a broad `sweep` when a single date will
do, keep `--max-requests` low, and do not loop `watch` tightly.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | found bookable vehicles |
| `1` | query worked, but nothing bookable matched (after filters and, for `watch`, `--below`) - **the only code that means keep waiting** |
| `2` | usage error, ambiguous or unknown branch, age refusal, refused route, ceiling exceeded, or every request in a fan-out refused |
| `3` | network or API failure, including a TLS block - and a fan-out where nothing priced and at least one failure was not a refusal |

Codes are identical under `--json`, where errors the tool raises come back as
a JSON object (`error`, `exit_code`) on stdout. **Argument-parsing errors are
the exception:** a missing required flag or a non-numeric `--age` is rejected
by argparse before `--json` takes effect, so it exits 2 with a plain-text
usage message on **stderr** and nothing on stdout. A `2` is never worth
retrying: the query as asked can never succeed.

## Output shapes

| Command | `--json` returns |
|---|---|
| `locations` | array of branch objects |
| `quote` | array with **one object per `--age`**, each holding a `vehicles` array |
| `sweep` | array of window rows — **three different shapes**, see below |
| `compare` | array of branch rows |
| `watch` | array of **vehicle** objects (flat — not quote's per-age shape) |
| `branch` | a single object |
| `doctor`, `cache` | a single object |

`sweep`'s array mixes three row kinds, and a consumer keying on `cheapest` will
break on exactly the rows that mean something went wrong:

- **priced** — `branch`, `pickup_date`, `return_date`, `cheapest`, `error: null`
- **checked but empty** — `cheapest: null`, `note: "checked; nothing matched…"`
- **failed** — `error` set, `return_date: null`

Always test `error` first, then `cheapest`.

## Cross-border one-way: a real trap

Enterprise refuses cross-border one-way routes (Toronto to Denver, say) with
the *same* message it uses for a genuinely sold-out branch: "no vehicles
available during the selected dates". The two cannot be told apart from
outside.

The tool therefore exits **2** with an explicit warning when a cross-border
one-way comes back empty, and `watch` refuses such a route outright. **A
one-way where either branch's country is unknown is treated as cross-border**
(the resolver could not confirm both ends are in one country), so it gets the
same exit 2 and the same `watch` refusal; resolve both ends with `locations`
and check `CTRY` if that seems wrong. Report it
as a probable route restriction, **not** as "no cars available" - otherwise
the user hunts for dates that will never work.

## What this tool cannot tell you

Say so plainly rather than guessing - these gaps change decisions:

- **No commercial terms at all.** Deposit and hold amount, insurance and
  waiver pricing, fuel policy, cancellation and change rules, and
  additional-driver fees are **not available from this API**. `branch` prints
  an `age_policy` line, but it is a boilerplate string, not the branch's
  terms. Send the user to Enterprise's own terms for any of this.
- **Whether a sold-out class would have met the filters.** `--seats`,
  `--bags`, `--class`, `--drive`, `--fuel` and `--max-price` are applied to
  *bookable* classes only. "No 7-seater available" therefore means "none among
  the bookable classes" - a sold-out minivan is never tested against the
  filter, and never mentioned as a near miss.
- **Why an age-blocked class is missing** - see the sold-out caveat above.
- **One-way drop fees.** No drop-fee field exists anywhere in the response, so
  a one-way total may not be the whole cost. Do **not** treat the gap against a
  round trip as the "premium" — measured, a one-way came back $157 *cheaper*.
  What is real and worth reporting is the inventory collapse: 8 of 59 classes
  bookable one-way against 20 round-trip.
- **Whether an empty facet means an empty fleet.** `--fuel electric` returning
  nothing may mean the facet is unpopulated for that market rather than that
  the branch has no EVs. Report it as "none listed as electric", not "no
  electric cars", and check the fleet without the filter before concluding.

## Reporting back

Lead with the answer, not the table.

- Give the cheapest vehicle meeting the constraints, its **trip total in the
  billing currency**, and one line on why it fits ("5 seats, 3 bags, AWD,
  unlimited mileage"). Then two or three alternatives.
- **Name the exact branch quoted, with its airport code and id.** If a
  similarly-named branch was rejected, say which - Exotic branches carry a
  different fleet at very different prices.
- **Say "or similar" out loud.** Every class is "or similar" - Enterprise
  returned `guaranteed_model: false` (the CLI's JSON key; the raw API field is `guaranteed_vehicle`) on all 393 classes sampled across four
  countries, so a specific model is never promised. Say it, and
  **say the mileage cap out loud** whenever mileage is not unlimited. Both
  change which car you would recommend.
- When classes are sold out, say how many and which categories.
- When the renter is under 25, give the surcharge **and** name the classes
  that disappear between `--age 25` and their age - that diff is the only way
  to see them, since the tool reports them as sold out.
- For points-vs-cash, give the points price and the cash price as raw
  numbers. Do **not** turn points into a cash comparison - what the
  figure measures is unverified (see `recipes.md` 8).
- Close by saying plainly that this tool cannot book, and that a quote is good
  only for the moment it was taken.

## Caching

Branch lookups cache for 7 days, branch hours for 1 day (holiday hours move),
and age rules for 30 days. **Prices and availability are never cached at any
TTL** - a stale "available" is worse than no answer.
`--no-cache` bypasses the branch cache; it does not change pricing behaviour
because pricing is never cached.

## On claude.ai

Everything is bundled; there is nothing to set up. TLS handling is automatic -
the tool negotiates a working transport by itself and reports which one it
used under `doctor`. A `sweep` is capped and may return partial results, which
it labels `PARTIAL`; report partial results as partial. A "watch" here means
re-running the query, not a background daemon - `watch` makes a couple of
checks and exits 1 if nothing matched, which means run it again later.
