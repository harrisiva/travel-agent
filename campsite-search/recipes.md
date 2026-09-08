# Recipes: worked campsite searches

End-to-end examples for the `campsite-search` skill. Every command here was run
against the live API and works as written.

Set `CS` to the launcher **path** once and every snippet below is copy-pasteable
from any directory:

```bash
CS=<path-to-this-skill>/scripts/campsites.py
python3 "$CS" providers        # every command takes this shape
```

Keep the `python3` outside the variable. Putting it inside (`CS="python3
/path/..."`) works in bash but **fails in zsh**, macOS's default shell, which
does not word-split unquoted parameter expansions — you get
`no such file or directory: python3 /path/...`.

(Equivalently, `cd <path-to-this-skill>/scripts` and use `python3 -m campsites`.)

Read `SKILL.md` first for the two rules that cause wrong answers: **`--end` is
inclusive in `sweep`/`find`**, and **a default search only sees the Campsite
booking category**.

---

## 1. Resolving an ambiguous park name

The user says "Algonquin". Ontario Parks has 17 of them, and `search` refuses to
guess:

```bash
python3 "$CS" search ontario "Algonquin" --start 2026-09-04 --end 2026-09-06
# error: 17 parks match 'Algonquin'; be more specific:
#   Algonquin - Achray Campground / Sand Lake Gate
#   Algonquin - Basin Lake
#   Algonquin - Brent Campground
#   ...
# exit 2
```

Exit `2` is a usage error, not "nothing available". List the candidates:

```bash
python3 "$CS" parks ontario --search algonquin
#  -2147483627  Algonquin - Canisbay Lake Campground
#  -2147483470  Algonquin - Hwy 60 Corridor
#  ...
```

Then either ask the user which one, or — if they clearly meant the whole park —
skip disambiguation entirely and use `find` (recipe 2).

Park names must match closely enough to be unique. `"Killarney Provincial Park"`
resolves; `"Killarney"` may not.

---

## 2. Any weekend opening across a whole park group

`find` sweeps every park matching a pattern and ranks them by how much is open.
Use it whenever the user names a region rather than one campground.

```bash
python3 "$CS" find ontario "Algonquin" \
    --start 2026-09-04 --end 2026-09-07 --nights 2 --weekends --limit 6
# Searching 17 parks matching 'Algonquin' (~44 requests)...
#   · Algonquin - Achray Campground / Sand Lake Gate
#   · Algonquin - Basin Lake
#   ...
# Nothing available across 17 parks.
# 0 openings across 0 of 17 parks (44 requests).
```

It prints its request plan up front and **refuses before spending anything** if
the plan exceeds `--max-requests` (default 200). Start narrow and widen.

An empty pattern sweeps the entire tenant — fine on a small one like GRCA:

```bash
python3 "$CS" find grca "" --start 2026-09-01 --end 2026-09-30 --nights 2 --weekends
#   Guelph Lake Conservation Area
#     2361 openings on 12 dates: Sep 3, Sep 4, Sep 5, Sep 10, Sep 11, ... +4 more
#   Byng Island Conservation Area
#     1304 openings on 12 dates: Sep 3, Sep 4, Sep 5, Sep 10, Sep 11, ... +4 more
#   ...
# 8178 openings across 8 of 8 parks (55 requests).
```

Parks are ranked by how much is open, so the first line is usually the answer.
Report the top few with their best dates rather than dumping 8178 openings.

**Before reporting "nothing across 17 parks", re-read the category rule.** That
result covers only the default Campsite category. Several of those 17 are
backcountry-only locations — see recipe 5.

---

## 3. Filtering on site attributes

Never guess attribute values; the vocabulary is per-park. List the real ones
first, with live counts:

```bash
python3 "$CS" attrs ontario "Pinery Provincial Park"
# Pinery Provincial Park — filterable site attributes (use with --attr 'Name=Value')
#   Barrier Free               No (961), Yes (9)
#   Conditions                 Poison Ivy (822), ...
#   Electrical Service         15/30 Amps (524), 15 Amps (8)
#   Fee Type                   Premium Rate (801), Demand Rate (156), Regular Rate (16)
```

The counts tell you whether a filter is worth applying — `Barrier Free=Yes`
matches 9 sites at Pinery, so an empty result there means almost nothing.

Then filter. `--attr` is repeatable and values must match exactly as printed:

```bash
python3 "$CS" sweep ontario "Pinery Provincial Park" \
    --start 2026-09-01 --end 2026-09-30 --nights 2 --weekends \
    --attr "Electrical Service=15/30 Amps" --attr "Barrier Free=No"
```

Note the `Conditions` field — Ontario flags `Poison Ivy`, `Above Avg. Vehicular
Traffic`, `Poor Drainage`. Worth surfacing to the user unprompted when you quote
a site.

---

## 4. Finding roofed accommodation (cabins, yurts, oTENTiks)

Roofed units are a **different booking category**. Searching for them with the
default category returns `INVALID` for every unit, which is indistinguishable
from "sold out".

Step 1 — see what the park actually has:

```bash
python3 "$CS" stays pc "Banff - Two Jack Lakeside"
#   onsite  roofed: Cabin, Equipped Camping, MicrOcube, Prospector Tent,
#                   Rustic Cabin, Teepee, Yurt, oTENTik (10), Ôasis
# In Banff - Two Jack Lakeside: Campsite (64), oTENTik (10)
```

The counts in parentheses are what exists **in this park**: 64 campsites and 10
oTENTiks. Anything without a count is a type the tenant supports elsewhere.

Step 2 — search with the alias, never a raw ID (`roofed` is `1` on Parks Canada
but `2` on Ontario):

```bash
python3 "$CS" sweep pc "Banff - Two Jack Lakeside" --booking-category roofed \
    --start 2026-08-20 --end 2026-10-10 --nights 1
# Nothing available.
```

Step 3 — **verify that "nothing" is real** before reporting it. The histogram is
a `search -v` feature only; `sweep -v` prints nothing extra. Re-run one night as
a `search`:

```bash
python3 "$CS" search pc "Banff - Two Jack Lakeside" --booking-category roofed \
    --start 2026-08-20 --end 2026-08-21 -v
# status counts: {'INVALID': 62, 'UNAVAILABLE': 10, 'CLOSED': 2}
```

Read it: `UNAVAILABLE: 10` matches the 10 oTENTiks exactly, so they are
genuinely all booked — a true and useful answer. The 62 `INVALID` are the
campsites, which don't accept this category. Had *everything* been `INVALID`,
the category or equipment would be wrong instead.

Across a whole group, combine with `find` and `--type`:

```bash
python3 "$CS" find pc "Banff" --booking-category roofed --type oTENTik \
    --start 2026-09-01 --end 2026-09-30 --nights 2
```

---

## 5. Backcountry and paddle-in sites (invisible by default)

The Massasauga looks completely sold out on a default sweep:

```bash
python3 "$CS" sweep ontario "The Massasauga Provincial Park" \
    --start 2026-08-22 --end 2026-08-24 --nights 2 --json
# { ... "dates": [] }
```

`dates: []` means **"nothing in the Campsite category"** — it does not mean the
park is full. Check what categories the park really has:

```bash
python3 "$CS" stays ontario "The Massasauga Provincial Park"
# Booking categories (pass to --booking-category):
#    11  Backcountry Registration   [alias: backcountry]
#     5  Hiking                     [alias: hiking]
#     4  Paddling                   [alias: paddling]
#     0  Campsite                   [alias: campsite]
#     2  Roofed Accommodation       [alias: roofed]
# In The Massasauga Provincial Park: Paddling Backcountry (137), Canoe (2), ...
```

137 paddle-in sites the default query never touched. Re-run against them:

```bash
python3 "$CS" sweep ontario "The Massasauga Provincial Park" \
    --start 2026-08-22 --end 2026-08-24 --nights 2 --booking-category paddling
# The Massasauga Provincial Park — 2-night stays between 2026-08-22 and 2026-08-24
#
#   2026-08-22 Sat -> 2026-08-24   6 sites
#     The Massasauga/107 - North Arm, The Massasauga/110 - North Arm, ...
#
#   2026-08-23 Sun -> 2026-08-25   30 sites
#     The Massasauga/5 - Spider-Clear L., The Massasauga/18 - Spider-Clear L., ...
#
# 36 openings across 2 check-in dates (1 requests).
```

36 openings where the default sweep said nothing. Note the second row: check-in
`2026-08-23`, checkout `2026-08-25` — **past the `--end` of `2026-08-24`**. That
is the inclusive-`--end` rule in action.

The default `--equipment tent` is fine for paddle-in sites; you do not need to
change it. Kawartha Highlands behaves the same way.

---

## 6. Checking one site's calendar (the authoritative check)

`sweep` finds candidates; `site` confirms them. Use it before you tell a user a
specific date is open — its output is one explicit line per date, with no
inclusive/exclusive ambiguity to misread.

```bash
python3 "$CS" site ontario "Killarney Provincial Park" --site 16 \
    --start 2026-08-21 --end 2026-08-25 --party 1
# Site 16 — George Lake A
#   Service Type: Non-Electric | Privacy: Average | Site Shade: Full Shade | Pull-through: No
#
#   2026-08-21  Fri  unavailable
#   2026-08-22  Sat  unavailable
#   2026-08-23  Sun  free
#   2026-08-24  Mon  unavailable
#   2026-08-25  Tue  unavailable
#
# 1 free nights of 5 dates.
```

This is the exact pairing worth internalising. The sweep that surfaced this site
reported a check-in row of `2026-08-23`, and the calendar confirms: the 23rd is
free, the **22nd is not**. Reading that row as "the 22nd is open" is the mistake
the `--end` rule exists to prevent.

`--site` takes the site name as the park lists it, which is not always numeric —
`Y3` and `C1` at Killarney, `A209` at Sandbanks, `RA 6` at Pinery, `W2` at Windy
Lake. Quote it: `--site "RA 6"`.

---

## 7. Watching a sold-out campground

`search` and `sweep` exit `0` on a hit and `1` on "nothing yet", so they drop
straight into a polling loop. **Only `1` means keep waiting** — `2` (bad park,
ambiguous name) and `3` (network) mean the loop itself is broken and must stop,
or it will spin forever against a query that can never succeed.

```bash
#!/bin/bash
CS=<path-to-this-skill>/scripts/campsites.py
INTERVAL=900   # 15 minutes — the documented minimum. Do not go lower.

while true; do
  OUT=$(python3 "$CS" search ontario "Killarney Provincial Park" \
          --start 2026-08-22 --end 2026-08-24 --party 1 2>&1)
  CODE=$?
  case $CODE in
    0) echo "FOUND at $(date)"; echo "$OUT"
       osascript -e 'display notification "Campsite open!" with title "Killarney"' 2>/dev/null
       break ;;
    1) echo "$(date +%H:%M) — nothing yet, sleeping ${INTERVAL}s" ;;
    *) echo "ABORT: exit $CODE — the query is broken, not just empty" >&2
       echo "$OUT" >&2; exit $CODE ;;
  esac
  sleep $INTERVAL
done
```

For a recurring job, put that body (without the loop) in a wrapper script and
schedule it with `cron` — `*/15 * * * *` at the fastest. Confirm the cadence
with the user before starting anything recurring.

**Check whether waiting is even the right advice first.** If the park has not
opened bookings yet, a watch loop is pointless:

```bash
python3 "$CS" window ontario "Killarney Provincial Park"
# Killarney Provincial Park (America/New_York)
#   Camp Cabins: ... opens: —
#   Backcountry Hiking: 2023-04-28T04:00:00Z .. 9999-12-30T05:00:00Z   opens: —
```

`horizon` answers the related question of how far ahead this park can be booked
at all. Never set up a loop that races other users at a launch-day opening.

---

## Quick reference

| Situation | Command |
|---|---|
| Name matches many parks | `parks <prov> --search <text>` |
| User named a region | `find <prov> "<pattern>"` |
| Vague about dates | `sweep ... --nights N --weekends` |
| Confirming a specific date | `site <prov> "<park>" --site N` |
| "Nothing available" — is it real? | `search ... -v`, read the histogram |
| Park looks sold out | `stays <prov> "<park>"`, try other categories |
| Filter values unknown | `attrs <prov> "<park>"` |
| Equipment name unknown | `equipment <prov>` |
| Site list looks stale | add `--no-cache`, or `cache-clear` |
