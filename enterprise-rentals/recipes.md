# Enterprise rentals - worked workflows

End-to-end examples. `SKILL.md` has the interface contract; this file has the
sequences. `E=` below stands in for
`python3 <path-to-this-skill>/scripts/enterprise.py`.

---

## 1. "Cheapest car at Halifax airport, Oct 15-18, for five people"

Resolve first - `Halifax` alone is ambiguous and includes an Exotic branch.

```bash
E locations "Halifax"
```

```
ID        CODE  NAME                            KIND     CITY     CTRY  CUR
1019286   YHZ   Halifax International Airport   airport  Enfield  CA    CAD
1054600   YHZ   Halifax Airport Exotic [EXOTIC] airport  Enfield  CA    CAD
1030356   -     Halifax Train Station           rail     Halifax  CA    CAD
```

Check `CTRY` before anything else - `--country` is a hint, not a filter, so a
same-named branch in another country sits in this list looking perfectly
correct. `locations` warns when a match falls outside `--country`, and
`quote`/`sweep`/`compare` repeat that warning on **stderr** - which `--json`
consumers must still read (see *Coverage is worldwide* in `SKILL.md`). Then
quote the one you meant. `YHZ` resolves to the mainstream airport branch;
pass `1019286` if you want to be explicit.

```bash
E quote YHZ --pickup-time 2026-10-15 --return-time 2026-10-18 --seats 5
```

Read the footer, not just the table: `20 of 59 classes bookable` plus a
sold-out breakdown, and the reminder that `PER DAY` is the trip total divided
by the rental days rather than Enterprise's own rate line. If the footer flags
a non-daily rate, quote the trip total, not a per-day figure. If the user wanted a minivan and vans are sold out, that
is the answer.

---

## 2. "Is another week cheaper?"

`sweep` slides a fixed-length rental across a range. It prints its plan before
sending anything.

```bash
E sweep YHZ --start 2026-10-10 --end 2026-10-22 --nights 3
```

```
  plan: 10 window(s) of 3 night(s) = 10 request(s), ~7.0 MB, ~8s
PICKUP      RETURN      CLASS  VEHICLE            MILEAGE       TOTAL
2026-10-19  2026-10-22  CFAR   Nissan Kicks       unlimited  285.08 CAD
2026-10-15  2026-10-18  PPAR   Ram 1500           unlimited  436.00 CAD
```

A three-night rental starting four days later was **$151 cheaper** here. Add
`--step 7` to compare like-for-like weekends, and `--nights 7` for weeks.

If the footer says `PARTIAL: swept 9 of 10 windows`, say so - some windows
failed and the cheapest may not have been seen.

---

## 3. "Airport or downtown?"

```bash
E compare 1019286 1030356 --pickup-time 2026-10-15 --return-time 2026-10-18
```

Airport branches carry concession fees; downtown is often cheaper but has
narrower hours. Pair this with recipe 6 before recommending downtown.

Comparing branches in **different countries** prints a mixed-currency warning.
Do not rank across currencies without converting - 302.80 USD is not smaller
than 436.00 CAD just because the number is.

---

## 4. "My 22-year-old wants to rent"

Pass both ages in one invocation. Two separate runs give you the surcharge but
hide the bigger story.

```bash
E quote YHZ --pickup-time 2026-10-15 --return-time 2026-10-18 --age 21 --age 25
```

```
  Age comparison:
    age 21: 13 of 59 classes, cheapest 516.01 CAD
    age 25: 20 of 59 classes, cheapest 436.00 CAD
```

Report both halves: about $80 more **and** seven classes they cannot rent at
all. The diff between the two ages is the *only* evidence of those seven - the
API reports age-restricted classes as `SOLD_OUT`, so at age 21 alone they are
indistinguishable from a sell-out. Never suggest different dates for them.

An age below the branch's own minimum is refused outright (exit 2), not merely
expensive - and the tool sets no threshold of its own, so at a branch requiring
21, age 20 is refused too.

---

## 5. "Something I can sleep in for a two-week road trip"

```bash
E quote YHZ --pickup-time 2026-10-15 --return-time 2026-10-29 \
    --sleepable --limit 5
```

`--sleepable` is AWD/4WD SUV or van, 5+ seats, unlimited mileage. The mileage
term is the point: a capped rate on a 3,000 km trip is not the cheap option it
looks like. Build it by hand if you need something different:

```bash
E quote YHZ --pickup-time 2026-10-15 --return-time 2026-10-29 \
    --class suv --drive awd --seats 5 --unlimited-mileage --transmission automatic
```

---

## 6. "Can I collect at 6am and drop off Sunday night?"

`branch` defaults to **today's** hours, which answers the wrong question for a
trip in October. Pass `--date` for the day the user actually arrives:

```bash
E branch YHZ --date 2026-10-15 --limit 7
```

```
DATE        COUNTER OPEN              AFTER-HOURS DROP
2026-10-15  00:00-02:00, 06:30-23:59  24 hours
```

Counter hours and drop-off hours are different questions. A late flight can
usually *return* a car when it cannot *collect* one - which decides whether a
cheap downtown branch is actually usable.

A `-` under **AFTER-HOURS DROP** is ambiguous: it means the branch published
nothing for that day, which may be "no after-hours return" or may be "not
reported". Say it is unknown and point the user at the branch; do not read it
as a closed key-box.

---

## 7. One-way

Within one country, just name a different dropoff:

```bash
E quote YHZ --dropoff YQM --pickup-time 2026-10-15 --return-time 2026-10-18
```

**There is no drop-fee field**, and the difference against the round-trip total
is not a reliable "one-way premium" - the two quotes are drawn from different
inventory, and measured here the one-way came out **$157 cheaper** than the
round trip, not dearer. Quote both totals as two prices; do not present the gap
as a fee.

What is worth reporting is the **inventory collapse**: the same branch and
dates offered 20 bookable classes round-trip and only **8 of 59** one-way. The
one-way constraint costs choice far more visibly than it costs money, so check
the wanted class exists before promising the route.

**Cross-border is different.** Toronto to Denver returns "no vehicles
available during the selected dates" - the same message as a genuine
sell-out - because the route is almost certainly not permitted. The tool exits
2 and says so. Report it as a probable route restriction, not as bad luck with
dates, and do not send the user hunting for other dates.

---

## 8. "Worth using my points?"

Points appear **only under `--json`** - the human table does not show them.
Each class carries `points_per_day`:

```bash
E quote YHZ --pickup-time 2026-10-15 --return-time 2026-10-18 --json \
  | python3 -c "import json,sys; [print(v['class_code'], v['points_per_day']) for v in json.load(sys.stdin)[0]['vehicles'] if v['points_per_day']]"
```

**Points are a PER-DAY figure, and the trip cost is not derivable.**
`charges.REDEMPTION`'s "total" equals its own per-day rate line in every
response checked, and the API never states how many days a redemption covers -
so no cents-per-point figure can honestly be computed, and the tool no longer
reports one. What the number measures is
unverified. `points_per_day` runs 1,650-2,400 for a Halifax fleet; the
withdrawn cents-per-point figure ran 26-54, far from the ~1 cent a redemption rate
would imply, which suggests it may be per-day rather than per-trip, or scaled
differently again. Any "above X favours points" rule built on it would say
*burn points* every single time.

Report the raw numbers instead - the cash total, the points price, and the
per-day points figure as exactly that - and tell the user to check
the value against Enterprise's own redemption terms before deciding.

---

## 9. Watching a sold-out class

```bash
E watch YHZ --pickup-time 2026-12-24 --return-time 2026-12-28 \
    --class minivan --below 900
```

Exit 1 means keep waiting; run it again later. Exit 2 means stop - the query
can never succeed. On claude.ai this is a repeated query, not a daemon;
schedule it if the user wants ongoing monitoring.

---

## 10. Working outside Canada

```bash
E quote DEN --country US --locale en_US --pickup-time 2026-10-15 --return-time 2026-10-18
E quote LHR --country GB --locale en_GB --pickup-time 2026-10-15 --return-time 2026-10-18
```

Check the branch's `CTRY` even here: `--country` is a hint, so a European
search can still resolve to a branch elsewhere, and the tool warns on stderr
when it does. Outside North America, check the `TRANS` column or pass
`--transmission automatic` if the renter cannot drive a manual - an unfiltered
quote mixes both.

Each branch prices in its own currency. `--currency` adds a labelled estimate
alongside the charged amount; it never replaces it.

---

## When something goes wrong

```bash
E doctor
```

Exit 3 with a TLS message means the environment's handshake is being rejected,
not that there are no cars. The tool tries several transports automatically;
`doctor` reports which one worked. Never turn a failure into "nothing
available" - they are different answers and only one of them means try again
later.
