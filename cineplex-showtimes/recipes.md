# Recipes: monitoring & scheduled jobs

Copy-paste templates for watching Cineplex availability. Replace every
`<PLACEHOLDER>` with IDs resolved fresh (see `SKILL.md`: `movies --name`,
`locations --name`, then `showtimes ... --json` for the `sessionId`), and
confirm the polling cadence with the user before starting anything recurring.

Every recipe keys off the exit code, never off "the JSON was empty":

| Code | In a watch |
|---|---|
| `0` | found it — notify and stop |
| `1` | nothing yet — keep waiting |
| `2` | the command is wrong (bad id, row not in the room, showing already started) — stop |
| `3` | Cineplex unreachable — retry later, but give up after a few in a row |

---

## 1. Short-lived polling loop (foreground watch)

Poll one session until an open seat appears in the middle of G or H.

```bash
cd <path-to-this-skill>/scripts
THEATRE=<THEATRE_ID>; SESSION=<SESSION_ID>; ROWS="G,H"; FAILS=0

while true; do
  python3 cineplex_showtimes.py seats --theatre "$THEATRE" --showtime "$SESSION" \
      --rows "$ROWS" --middle
  rc=$?
  case $rc in
    0) osascript -e 'display notification "Middle seats open!" with title "Cineplex"' 2>/dev/null
       break ;;
    1) FAILS=0 ;;
    2) echo "command rejected — fix it before watching again"; break ;;
    3) FAILS=$((FAILS + 1)); [ "$FAILS" -ge 5 ] && { echo "Cineplex down 5 polls running"; break; } ;;
    *) echo "unexpected exit $rc (python3 missing?)"; break ;;
  esac
  sleep 300
done
```

---

## 2. Recurring scheduled job (cron / launchd)

cron runs with a minimal `PATH` and no working directory, so the wrapper uses
absolute paths throughout. Get the python path with `command -v python3` and
paste it in — a bare `python3` may be a different interpreter (without
`requests`) or not found at all under cron.

**Wrapper** `$HOME/cineplex_watch.sh` (then `chmod +x "$HOME/cineplex_watch.sh"`):

```bash
#!/bin/bash
PY=<output of: command -v python3>
CLI=<absolute-path-to-this-skill>/scripts/cineplex_showtimes.py
LOG="$HOME/cineplex_watch.log"
DONE="$HOME/.cineplex_watch.done"   # "notify and stop": one hit, then silent
[ -e "$DONE" ] && exit 0

"$PY" "$CLI" seats --theatre <THEATRE_ID> --showtime <SESSION_ID> \
    --rows G,H --middle >/dev/null 2>>"$LOG"
rc=$?
case $rc in
  0) echo "$(date): seats open" >> "$LOG"; touch "$DONE"
     osascript -e 'display notification "Seats open!" with title "Cineplex"' 2>/dev/null ;;
  1) ;;                                                      # still full
  2) echo "$(date): command rejected, remove this job" >> "$LOG"; touch "$DONE" ;;
  3) echo "$(date): Cineplex unreachable" >> "$LOG" ;;
  *) echo "$(date): unexpected exit $rc (python3 path wrong?)" >> "$LOG" ;;
esac
```

**cron** (`crontab -e`) — every 30 minutes. cron does not expand `~` reliably
and has no `$HOME` in some setups, so write the full path (`echo $HOME` to get it):

```
*/30 * * * * /full/path/to/home/cineplex_watch.sh
```

**macOS launchd** alternative — a plist in `~/Library/LaunchAgents/` with
`StartInterval` set to `1800`, `ProgramArguments` pointing at the wrapper's
full path. Load with `launchctl load ~/Library/LaunchAgents/<name>.plist`.

After a hit (or a rejected command) the wrapper goes quiet via the `$DONE`
sentinel; remove the job then, and `rm "$HOME/.cineplex_watch.done"` before
reusing the wrapper for another showing.

> Keep the interval in minutes. These calls hit Cineplex's live servers.

---

## 3. "Tell me when tickets go on sale"

Before a showing is listed, `showtimes` exits 1 (the API answers 204, "nothing
on"). Watch for the first 0, using the same wrapper shape as recipe 2:

```bash
PY=<output of: command -v python3>
CLI=<absolute-path-to-this-skill>/scripts/cineplex_showtimes.py
"$PY" "$CLI" showtimes --location <THEATRE_ID> --film <FILM_ID> \
    --date <YYYY-MM-DD> --experiences imax
# 0 = listed now, 1 = not yet, 2 = bad id/date/experience, 3 = retry later
```

---

## 4. Python monitor (diff + notify) using the library

Reports seats newly opened between polls. Network errors are caught so one
outage doesn't kill the monitor.

```python
#!/usr/bin/env python3
import sys, time
sys.path.insert(0, "<absolute-path-to-this-skill>/scripts")
import requests
import cineplex_api as api

THEATRE, SESSION = <THEATRE_ID>, <SESSION_ID>
WANT_ROWS, INTERVAL, MAX_FAILS = ["G", "H"], 300, 5  # seconds

seen, fails = set(), 0
session = api.new_session()
while True:
    try:
        layout = api.fetch_seat_layout(THEATRE, SESSION, session=session)
        avail = api.fetch_seat_availability(THEATRE, SESSION, session=session)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print("Showtime no longer exists — stopping.")
            break
        fails += 1
    except requests.RequestException:
        fails += 1
    else:
        fails = 0
        if (avail or {}).get("isPostShowtime"):
            print("Showing has started — stopping.")
            break
        labels = {s["seat"] for s in api.filter_seats(layout, avail, rows=WANT_ROWS, middle=True)}
        if labels - seen:
            print(f"NEW open seats: {sorted(labels - seen)}", flush=True)
            # hook a real notification here (email, webhook, osascript, ...)
        seen = labels
    if fails >= MAX_FAILS:
        print(f"Cineplex unreachable {fails} polls running — stopping.")
        break
    time.sleep(INTERVAL)
```

---

## 5. Multi-session sweep (which showings still have good seats?)

"Which IMAX times this weekend still have middle seats in G/H?"

```bash
cd <path-to-this-skill>/scripts
THEATRE=<THEATRE_ID>
# Capture first: piped straight into json.load, an exit 1/2/3 is lost and
# empty stdout (2/3) becomes a traceback.
OUT=$(python3 cineplex_showtimes.py showtimes --location $THEATRE --film <FILM_ID> \
  --dates <YYYY-MM-DD>,<YYYY-MM-DD> --experiences imax --json)
rc=$?
# if/elif rather than `exit`, so pasting this into a terminal can't close it.
if [ $rc -eq 1 ]; then
  echo "no IMAX showings on those dates"
elif [ $rc -ne 0 ]; then
  echo "showtimes failed (exit $rc)"
else
  printf '%s' "$OUT" \
  | python3 -c "import sys,json; [print(s['sessionId'], s['date'], s['time'].replace(' ', '')) for s in json.load(sys.stdin)]" \
  | while read SID DATE TIME; do
      python3 cineplex_showtimes.py seats --theatre $THEATRE --showtime $SID \
          --rows G,H --middle >/dev/null
      case $? in
        0) echo "$DATE $TIME (session $SID): middle seats in G/H" ;;
        1) echo "$DATE $TIME (session $SID): none in G/H" ;;
        *) echo "$DATE $TIME (session $SID): could not check" ;;
      esac
    done
fi
```

When the `showtimes` step exits 1 there is nothing to sweep — report "no
showings", not "no seats".
