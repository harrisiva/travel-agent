# Recipes

Multi-step workflows. `SKILL.md` has the interface; this file has the patterns
that are easy to get subtly wrong.

All examples assume `cd <path-to-this-skill>/scripts` first.

---

## 1. "Somewhere for dinner, open now, not far"

One command. Do not geocode first — `--near` takes free text and resolves it in
the same pass.

```bash
python3 gmaps.py nearby --near "The Drake Hotel, Toronto" \
    --query dinner --mode walk --within 15 --min-rating 4.3 --json
```

`results` is already sorted by travel time. Before reporting a short list, check
`hours_unknown` — non-zero means candidates were held back for lack of published
hours, not because they were shut. Check `hours_failed` and `network_errors`
too: those are lookups that broke, and they mean the list is incomplete for a
reason that has nothing to do with the query.

If it exits `1`, widen in this order, because each step costs requests:

1. drop `--min-rating`
2. raise `--within`
3. switch `--mode walk` to `drive`
4. raise `--span` (`--span 20000`)
5. broaden `--query` ("restaurants" rather than "dinner")

Never silently drop `--within`. If the answer is 25 minutes away, say so.

---

## 2. Walking vs driving — check both when it is close

In a city centre these give different answers, and driving is often the wrong
one: parking, one-ways and short hops make a 600 m drive slower than the walk.

```bash
HOTEL="The Drake Hotel, Toronto"
python3 gmaps.py nearby --near "$HOTEL" --query pharmacy --mode walk  --within 20 --limit 5 --json
python3 gmaps.py nearby --near "$HOTEL" --query pharmacy --mode drive --within 20 --limit 5 --json
```

Two searches, hence `--limit 5` on each. Report the mode that actually suits
the user — if they have no car, `drive` results are noise.

---

## 3. Comparing a shortlist the user already named

`travel` prices every destination in **one** request — a chained-waypoint
"star" route — then re-checks the five nearest individually for live traffic,
because the star response carries no traffic block. A five-way comparison is
one geocode per `--to` plus six routing calls, not one route lookup each.

```bash
python3 gmaps.py travel --from "Union Station, Toronto" \
    --to "Alo" --to "Canoe" --to "Edulis" --json
```

Each `--to` still costs a geocode, so pass `lat,lng` when a previous search
already gave you coordinates.

`travel` reports time and distance, and nothing about opening hours. For
whether they are also *open*, run `search` on the names and read `status`.

Results come back in `--to` order, not ranked — sort them yourself before
saying which is closest. Only the five nearest get a live-traffic figure; the
rest keep a free-flow number and `traffic_aware: false`, which is also why the
top-level `traffic_aware` reads `false` for a six-way comparison.

---

## 4. "Will it still be open when we land?"

`--open-at` is answered in the **place's** local time, which is what the
question means.

```bash
python3 gmaps.py nearby --near "Kensington Market, Toronto" \
    --open-at "Fri 21:00" --json
```

Two things to hold onto:

- **Do not add `--include-closed`.** `--open-at` already supersedes the
  open-now filter, so `nearby` will not first strip places that are shut right
  now but open on Friday. The payload's `open_now` reads `false` to confirm it.
- A bare hour is refused (`"Friday 7"` could be breakfast or dinner). Write
  `7pm` or `19:00`.

Quote the clock in the answer: "closes 10pm Toronto time, and you land at 9".

---

## 5. Lunch along a long drive — the `along` workaround

**There is no `along` command and no search-along-a-route endpoint.** This is a
known gap. The method below is the supported way to answer the question, and it
works well enough to use — but read the caveats, because it approximates the
route rather than following it.

Geocode both ends, interpolate a few points between them, search around each.

```bash
# A function, not a variable: "$GM --json ..." does not word-split in zsh.
gm() { python3 <path-to-this-skill>/scripts/gmaps.py "$@"; }

read -r flat flng < <(gm --json geocode "Toronto, Ontario" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["lat"],d["lng"])')
read -r tlat tlng < <(gm --json geocode "Kingston, Ontario" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["lat"],d["lng"])')

# Three points spread evenly between the two ends
python3 -c '
import sys
flat, flng, tlat, tlng = map(float, sys.argv[1:5]); n = int(sys.argv[5])
for i in range(1, n + 1):
    f = i / (n + 1)
    print("%.5f,%.5f" % (flat + (tlat-flat)*f, flng + (tlng-flng)*f))
' "$flat" "$flng" "$tlat" "$tlng" 3 > /tmp/waypoints.txt

while read -r pt; do
    echo "--- $pt ---"
    gm nearby --near "$pt" --query restaurants --span 15000 \
        --limit 3 --include-closed --max-requests 1 --sort distance
done < /tmp/waypoints.txt
```

Verified Toronto to Kingston, re-run 2026-09-08: the three points landed in
Oshawa (on the 401), Alnwick/Haldimand near Grafton (on the 401), and Picton —
which is deep in Prince Edward County, well south of the highway and reached by
a half-hour detour. Two out of three. That third point is the caveat below
happening in practice, not a hypothetical.

### Caveats, which matter

- **It interpolates a straight line, not the road.** Where the highway runs
  roughly straight this is fine. Where it curves — around a lake, through a
  pass, along a coast — the sampled points drift off the route and you get
  places nobody will drive past. Sanity-check the towns it names against the
  real route before reporting them.
- **Travel times are from each waypoint, not from the start.** That is the
  useful number: it says how far off the highway a place is. Never sum them,
  and never present one as trip time.
- **Use `--include-closed`.** Live status is answered when you ask, not when
  the traveller arrives three hours later. Report `hours_today` instead —
  "closes at 9pm" is what decides it. `hours_week` needs `--with-hours`, which
  costs another request per place; usually `hours_today` is enough.
- **Use `--sort distance`, not travel time.** You want what is nearest the
  route; a fast road can otherwise put a distant place first.
- **Budget it.** `--max-requests 1` caps each waypoint at one search page;
  `--limit 3` caps the routing calls that follow it. Together a three-stop
  sweep stays near a dozen requests. Unbounded, this is the shape that issues
  hundreds.

Deduplicate when merging — adjacent waypoints overlap. `place_id` is the right
key but it is `--full`-only, so either add `--full --json` or fall back to
`name` plus `address`.

## 6. Watching for something to open

Only exit `1` means "nothing yet". Exit `3` is a failure and must not reset the
loop's patience, or a watch reports "still closed" all night because egress
broke.

```bash
while true; do
    python3 gmaps.py nearby --near "43.6532,-79.3832" --query bar \
        --mode walk --within 15 --json > /tmp/open.json
    case $? in
        0) echo "something is open"; break ;;
        1) sleep 900 ;;                        # genuinely nothing — wait
        3) echo "network trouble; retrying"; sleep 120 ;;
        2) echo "bad query — fix the command"; break ;;
        *) echo "interrupted"; break ;;
    esac
done
```

Poll at 15 minutes or slower. **Run watches from Claude Code, not claude.ai** —
the sandbox container there is time-limited and will not survive a long loop.

---

## 7. Keeping output small

A twenty-place result with full weeks is large enough to be truncated by an
output cap, and middle-truncated JSON is *unparseable*, not merely short.

- Leave `--full` off unless you need coordinates or ids.
- `nearby` defaults to `--limit 8` deliberately; raise it only when needed.
- For an exploratory look, omit `--json` — measured over four queries the human
  table is about a third the size of the default JSON and a sixth the size of
  `--full`, and it is closer to what you will report anyway.
- If `output_truncated` is `true`, rows were dropped: `shown` and `matched` say
  how many. Narrow the query rather than reporting the partial list as complete.
