# Recipes

Worked end-to-end workflows. `SKILL.md` is the interface contract; this file is
loaded when a task needs one of these shapes.

All examples assume:

```bash
cd <path-to-this-skill>/scripts
```

and that google-maps is installed beside this skill — `gmaps.py` below means
`<path-to-google-maps>/scripts/gmaps.py`. Every hotel here is priced by **id**;
google-maps is how a name becomes one. `--full` on `gmaps.py search` is not
optional: the ids exist only in the full payload.

---

## 1. "What do hotels near Canmore cost for those nights?"

Two commands: google-maps decides *which* hotels, this skill prices them.

```bash
python3 gmaps.py search --near Canmore --query hotels --full --json > canmore.json
python3 hotels.py shortlist --ids-from canmore.json --checkin +30 --checkout +32 --json
```

`shortlist` prices the first `--limit` entries of the file (default 6, max
10), in Maps' own order, one page each — six pages is six requests and about
20 seconds. The file's `from` centre gives each row a `distance_km`.

**This is not a market search.** It prices exactly what google-maps returned,
so the answer is *"cheapest of the 6 checked near Canmore"*, never *"cheapest
in Canmore"*. To change who is in the running, change the google-maps side:

```bash
# Higher-rated only, ten of them, ranked by Google's rating
python3 gmaps.py search --near Canmore --query hotels --min-rating 4.3 --sort rating --limit 10 --full --json > canmore.json
python3 hotels.py shortlist --ids-from canmore.json --limit 10 --checkin +30 --checkout +32 --json
```

Filters on `shortlist` itself run after the fetches, cost nothing, and can only
shrink the list: `--max-price 250` (incl-tax per night), `--max-total 600`
(incl-tax for the stay), `--min-rating 4`, `--min-stars 3`,
`--free-cancellation`, and `--amenity`:

```bash
# Remote work: free Wi-Fi, and say what the others have
python3 hotels.py shortlist --ids-from canmore.json --checkin +30 --checkout +32 --amenity wifi:free --json
```

The output always carries all four counts — *"6 checked: 3 have free Wi-Fi, 1
has it on other terms, 1 is listed as not having it, 1 does not list it"* — and
only the first group passes. A hotel that does not list an amenity is
*unknown*, not lacking; say so rather than dropping it silently from the
story.

**Report it as:** how many were checked and how many were filtered out or had
no rates, then the cheapest by incl-tax stay total with its seller, then the
runner-up. Entries in `skipped[]` were in the file without ids (the gmaps run
lacked `--full`); `failed[]` were fetched and could not be priced — neither is
"no rates".

---

## 2. One named hotel: tax-inclusive, who sells it, can I cancel

```bash
python3 gmaps.py search --near Banff --query "Fairmont Banff Springs" --limit 1 --full --json > fairmont.json
python3 hotels.py quote --ids-from fairmont.json --checkin 2026-12-31 --checkout 2027-01-02 --json
```

`--limit 1` matters: `quote` prices one building and refuses a file holding
several, so it never quietly picks the first of twenty. Check the name that
came back before quoting — Maps returns its best match, which is not always
the place asked for.

The same hotel can be priced by any id straight off a Google Maps link, with
no file and no google-maps request:

```bash
python3 hotels.py quote 0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce --checkin 2026-12-31 --checkout 2027-01-02 --json
```

What to read out of the JSON, in this order:

- `cheapest` — the lowest **incl-tax** figure across every seller, and who.
  On the 2026-12-31 capture that was dealbase.com at 1452.38 incl. tax
  (1282.75 before tax), 2904.75 for the two nights.
- `headline` — Google's own lead rate and its seller when it differs from
  `cheapest`; it is not always the minimum.
- `breakdown` — `{base, taxes, fees, total}` for the stay, Google's own
  figures (2435.63 / 339.25 / 129.88 / 2904.75 on that capture). Never
  multiply nightly by nights; if `breakdown` is null say the total was not
  provided.
- `sellers[]` — each with `free_cancellation.shown` and `deadline_text`. On
  that page the hotel's own site (`own_site: true`) showed *"Nov 1, 4:00 PM"*
  and dealbase showed none. `shown: false` means no deadline was displayed;
  it does **not** mean non-refundable.
- `sellers[].basis` — a row with `"single"` shows one figure with no stated
  basis; the tool treats it as all-in but says so, and it beats a stated-basis
  seller only by a full dollar or more.

Add `--adults 2 --child-age 5` for a family; the party is echoed back in
`query.echoed` and the price can change by a multiple, not a percentage
(1,283 → 7,139 a night on the same page). Restate the party every time.

---

## 3. "Which check-in day is cheapest?" / "Which weekend?"

`cheapest` slides a fixed stay length across a window, one page per visited
date. `--days` is the window (default 14, max 21), `--step` the stride:

```bash
# Every check-in day for the next three weeks, three nights each
python3 hotels.py cheapest 0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce --checkin +1 --nights 3 --days 21 --json
```

That is 21 pages — 21 requests plus 3 spare, about a minute at the 2.5 s
throttle. For "which weekend", step a week at a time so every visited date is
the same weekday. `+N` counts from today, so work out N for the next Friday
first:

```bash
# Days until the next Friday (date +%u: Monday=1 … Sunday=7, Friday=5).
fri=$(( (5 - $(date +%u) + 7) % 7 )); [ "$fri" -eq 0 ] && fri=7
python3 hotels.py cheapest 0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce --checkin +$fri --nights 2 --days 21 --step 7 --json
```

`ceil(21/7)` = 3 Fridays, 3 requests. Every row carries the `checkout` it
actually priced, its `weekday`, and either a `cheapest` price or `failed:
true` with a reason. `best` is the winning `{checkin, checkout,
stay_incl_tax}`.

**Withhold the word "cheapest" when `cheapest_is_reliable` is false.** That
flag drops whenever any visited date failed (a transport error, or a page that
priced a different stay); say *"of the 21 days checked, 2 could not be
priced"* and give the best of the rest as provisional. Dates with no rates
listed count as empty, not failed, and do not affect it.

**Report it as:** the cheapest check-in, what it saves against the date they
asked for, and the weekday pattern — mid-week vs weekend is the finding
people act on.

---

## 4. Watch a hotel and act when it drops

`watch` is one check, designed for cron. Exit `0` means the threshold was met.

```bash
python3 hotels.py watch 6286624707044140820 --checkin +27 --checkout +29 --under 60 --json
```

`--under` is per night, incl-tax, against the cheapest seller on the page;
`--under-total 120` watches the stay total instead; `--basis ex` compares the
before-tax figure. The threshold is a trigger, never a filter: a value below
today's price is the normal case and is never refused.

A daily job. It has to act on exit `0`: a job that only appends to a log never
tells anyone the price dropped. `&&` runs the alert only when the watch exits
`0`; put whatever reaches the user — `mail`, a webhook `curl` — in a small
script at the path shown and make it executable:

```cron
0 9 * * * cd /path/to/google-hotels/scripts && /usr/bin/python3 hotels.py watch 6286624707044140820 --checkin 2026-10-10 --checkout 2026-10-12 --under 60 --json >> "$HOME/hotel-watch.log" 2>&1 && "$HOME/bin/hotel-alert"
```

Four rules for that line:

- **One physical line.** Crontab has no continuation; anything longer goes in
  a wrapper script.
- **Absolute `python3`** (`command -v python3` shows it) — cron runs with a
  minimal `PATH`.
- **Absolute dates.** `+N` is relative to the day the job runs, so the stay
  being watched would slide forward a day with every run.
- **An id, never a name.** `watch` accepts only ids, so the job is pinned to
  one building by construction; the CID above is the Samesun Banff. Get it
  from `resolve --ids-from` (offline) once, and paste it.

**The exit codes are the whole point.** Only `1` means "not yet, keep
waiting" — and that includes *"no rates listed for these nights"*, because a
cancellation is exactly what the watch is waiting for. `3` means the check
failed — Google blocked the request, the network is down, or the page priced
a different stay — and says nothing about the price. A loop that treats `3`
as `1` will poll a captcha page until the stay begins. `2` means the job is
dead — the check-in has slipped into the past, the id is unknown — and every
future run fails the same way; **disable the job on a `2`** rather than let it
fail daily. A wrapper that does that:

```bash
#!/bin/sh
# hotel-watch.sh — run by cron; disables itself on a dead configuration.
cd /path/to/google-hotels/scripts || exit 3
/usr/bin/python3 hotels.py watch 6286624707044140820 --checkin 2026-10-10 --checkout 2026-10-12 --under 60 --json >> "$HOME/hotel-watch.log" 2>&1
status=$?
if [ "$status" -eq 0 ]; then "$HOME/bin/hotel-alert"; fi
if [ "$status" -eq 2 ]; then crontab -l | grep -v hotel-watch.sh | crontab -; fi
exit "$status"
```

Twice a day is plenty. Hotel rates do not move minute to minute, and hammering
Google is how you start getting `3`s.

---

## 5. Hotel or campsite for the same nights

"Killarney's full for the long weekend — what would a hotel near the park
cost instead?" Three skills, in order: campsite-search for the park,
google-maps for the hotels near it, this one for the prices. campsite-search
takes `YYYY-MM-DD` only, so its line needs the absolute equivalent of the
`+N` dates the hotels line uses — the same three nights, spelled two ways.

```bash
# 1. Is the park really full? (campsite-search; absolute dates = today+26 → today+29; --end is the departure date)
python3 <campsite-search>/scripts/campsites.py search ontario "Killarney Provincial Park" --start 2026-10-09 --end 2026-10-12 --json

# 2. Hotels near the park (google-maps; --full for the ids)
python3 gmaps.py search --near "Killarney, Ontario" --query hotels --span 40000 --full --json > killarney.json

# 3. Price them for the same nights
python3 hotels.py shortlist --ids-from killarney.json --checkin +26 --checkout +29 --json
```

Step 2's `--span` is the width of the search viewport in metres; a park in the
middle of nowhere needs a wide one, and the answer should say which span
produced the list. Compare incl-tax stay totals against the campsite's fee,
and note what each includes: a hotel's `breakdown.fees` is Google's figure,
while campsite fees come from the park system's own listing.

---

## 6. A whole trip on a budget

"Flight from Toronto, a small car, three nights near Halifax airport — am I
anywhere near $800 all in, tax included?" Three skills, one sum. google-flights
takes `+N` dates; enterprise-rentals takes `YYYY-MM-DD`.

```bash
# Flight (google-flights): round trip, one adult
python3 <google-flights>/scripts/flights.py search YYZ YHZ --depart +30 --return +33 --limit 1 --json > flight.json

# Car (enterprise-rentals): the airport branch; enterprise takes YYYY-MM-DD only, so these are the absolute equivalent of +30 → +33
python3 <enterprise-rentals>/scripts/enterprise.py quote YHZ --pickup-time 2026-10-13 --return-time 2026-10-16 --class car --json > car.json

# Hotels near the airport (google-maps → google-hotels), same three nights
python3 gmaps.py search --near "Halifax Stanfield International Airport" --query hotels --full --json > yhz-hotels.json
python3 hotels.py shortlist --ids-from yhz-hotels.json --checkin +30 --checkout +33 --json > hotels.json
```

Sum three figures, each with its basis stated: the flight's round-trip fare
(a bare fare — no bags), the car's trip total in the branch's billing
currency, and the hotel's **`cheapest.stay.incl_tax`** — the incl-tax stay
total, so taxes and fees are already in. Say the flight excludes baggage and
that the car quote carries no insurance or deposit; those are the two lines
that push a real trip past the estimate.

---

## 7. A rental, or anything else, from a pasted Google Hotels link

A Google Hotels URL looks like
`https://www.google.com/travel/hotels/entity/<TOKEN>?…`. The token is the
path segment after `entity/`; pass it with `--token`. It is the **only** way to
price a vacation rental — rentals have no Maps id, so google-maps cannot find
them for this skill.

```bash
python3 hotels.py quote --token ChkQp5T1uba_qKpQGg0vZy8xMXo5cmh6MG03EAI --checkin 2026-12-31 --checkout 2027-01-02 --json
```

A rental comes back with `hotel.kind: "rental"`, `star_class: null`,
`highlights: null`, its amenities by name (`group: null`), and `rental{sleeps,
bedrooms, bathrooms, beds}`. **Quote the incl-tax stay total, not a nightly
figure** — on the captured listing fees were a fifth of the total
(`breakdown` 716.09 / 102.09 / 212.00 / 1030.19), so "$464 a night" is not what
the guest pays. `fees_share` above 0.10 belongs in the first sentence.

`watch --token` works the same way, and `resolve <token>` (offline) tells you
whether a token is a hotel or a rental before you spend a request.

---

## Notes for Claude on the web

The skill needs outbound HTTPS to `www.google.com`. If the environment has no
network egress, the first command fails with exit `3` and a message naming the
host — that is the environment, not a bug in the skill, and no retry or
different flag will fix it. Say so plainly and offer to work from prices the
user supplies instead.

`requests` is optional: the client falls back to `curl` and then to `urllib`,
and all three paths share the same budget and throttle. The self-check's
`[offline]` group exercises all three, in four cases: `requests` absent, curl
absent, curl present but unspawnable, and curl behind a proxy. So a missing
`requests` is not a reason to spend a turn installing anything before trying a
command.

Each hotel page is 1.6–4.3 MB of HTML and is parsed in memory, with 2.5 s of
deliberate throttling between pages. A single `quote` is a few seconds; a
default `shortlist` (six pages) is about 20 s; a 21-day `cheapest` is about a
minute. Prefer `--step 7` for "which weekend" over a 21-day daily sweep, and
run at most two shortlists per turn. google-maps' own request budget is
separate from this skill's — `gmaps.py search --full` spends its per-place
ceiling on the hours and ids it attaches.
