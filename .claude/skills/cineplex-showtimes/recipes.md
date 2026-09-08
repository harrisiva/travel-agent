# Recipes: monitoring & scheduled jobs

Copy-paste templates for watching Cineplex availability. All assume the tool at
`<path-to-this-skill>/scripts/` with `requests` installed. Adjust the
`THEATRE`, `SESSION`, film/date, and filters to the user's request, and confirm
the polling cadence with the user before starting anything recurring.

Resolve IDs first (see `SKILL.md`): `movies --name`, `locations --name`, then
`showtimes ... --json` to get the `sessionId`.

---

## 1. Short-lived polling loop (foreground watch)

Poll one session until a matching seat opens, then stop. Good for a bounded
"watch for the next ~30 min" request.

```bash
cd <path-to-this-skill>/scripts
THEATRE=7408; SESSION=535317; ROWS="G,H"

while true; do
  n=$(python3 cineplex_showtimes.py seats --theatre "$THEATRE" --showtime "$SESSION" \
        --rows "$ROWS" --middle --json | python3 -c "import sys,json;print(len(json.load(sys.stdin)['matched']))")
  if [ "$n" -gt 0 ]; then
    echo "FOUND $n matching seat(s) at $(date)"
    # macOS notification (optional):
    osascript -e 'display notification "Middle seats open!" with title "Cineplex"' 2>/dev/null
    break
  fi
  echo "$(date +%H:%M) — none yet, sleeping 5m"
  sleep 300
done
```

---

## 2. Recurring scheduled job (cron / launchd)

For "check every hour" style monitoring, use a wrapper script + the OS scheduler.

**Wrapper** `~/cineplex_watch.sh` (make executable with `chmod +x`):

```bash
#!/bin/bash
cd <path-to-this-skill>/scripts || exit 1
OUT=$(python3 cineplex_showtimes.py seats --theatre 7408 --showtime 535317 \
        --rows G,H --middle --json)
COUNT=$(echo "$OUT" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['matched']))")
if [ "$COUNT" -gt 0 ]; then
  echo "$(date): $COUNT seat(s) available" >> ~/cineplex_hits.log
  osascript -e 'display notification "Seats open!" with title "Cineplex"' 2>/dev/null
fi
```

**cron** (`crontab -e`) — every 30 minutes:

```
*/30 * * * * /Users/harri/cineplex_watch.sh
```

**macOS launchd** alternative — a plist in `~/Library/LaunchAgents/` with
`StartInterval` set to `1800` (seconds), pointing `ProgramArguments` at the
wrapper. Load with `launchctl load ~/Library/LaunchAgents/<name>.plist`.

> Keep the interval in minutes. These calls hit Cineplex's live servers; a
> tight loop is abusive and can get the key rate-limited.

---

## 3. Python monitor (diff + notify) using the library

Uses `cineplex_api` directly, so no subprocess/JSON round-trips. Tracks which
seats are newly available between polls.

```python
#!/usr/bin/env python3
import sys, time
sys.path.insert(0, "<path-to-this-skill>/scripts")
import cineplex_api as api

THEATRE, SESSION = 7408, 535317
WANT_ROWS, INTERVAL = ["G", "H"], 300  # seconds

seen = set()
session = api.new_session()
while True:
    layout = api.fetch_seat_layout(THEATRE, SESSION, session=session)
    avail = api.fetch_seat_availability(THEATRE, SESSION, session=session)
    open_seats = api.filter_seats(layout, avail, rows=WANT_ROWS, middle=True)
    labels = {s["seat"] for s in open_seats}
    new = labels - seen
    if new:
        print(f"NEW open seats: {sorted(new)}", flush=True)
        # hook a real notification here (email, webhook, osascript, etc.)
    seen = labels
    summary = api.summarize_seats(layout, avail)
    if summary["isSoldOut"]:
        print("Showtime sold out — stopping.")
        break
    time.sleep(INTERVAL)
```

---

## 4. Multi-session sweep (which showings still have good seats?)

Aggregate `matched` across every session for a film/date, e.g. to answer
"which Odyssey IMAX times still have middle seats this weekend?":

```bash
cd <path-to-this-skill>/scripts
THEATRE=7408
python3 cineplex_showtimes.py showtimes --location $THEATRE --film 37617 \
  --dates 7/19/2026,7/20/2026 --experiences 70mm,imax --json \
| python3 -c "import sys,json; print('\n'.join(str(s['sessionId'])+' '+s['date']+' '+s['time'] for s in json.load(sys.stdin)))" \
| while read SID DATE TIME; do
    N=$(python3 cineplex_showtimes.py seats --theatre $THEATRE --showtime $SID \
          --rows G,H --middle --json | python3 -c "import sys,json;print(len(json.load(sys.stdin)['matched']))")
    echo "$DATE $TIME  (session $SID): $N middle seat(s) in G/H"
  done
```

The agent can run this, collect the lines, and present a tidy table to the user.
