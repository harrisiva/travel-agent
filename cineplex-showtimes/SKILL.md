---
name: cineplex-showtimes
description: >-
  Check Cineplex (Canada) movie showtimes, theatres, films, and LIVE seat
  availability through a bundled Python CLI. Use whenever the user asks about
  Cineplex movie times, whether a screening (IMAX, 70mm, UltraAVX, etc.) is
  sold out, which/where seats are still open, finding a theatre or film ID, or
  setting up a scheduled job that watches for tickets or specific seats.
---

# Cineplex Showtimes & Seat Availability

A small, dependency-light Python client for Cineplex's public theatrical +
ticketing APIs. Two files do everything:

- `scripts/cineplex_api.py` — importable library (pure data functions, all return JSON).
- `scripts/cineplex_showtimes.py` — CLI driver with 5 subcommands, each supports `--json`.

**Tool location:** the `scripts/` directory next to this SKILL.md. All commands
below assume you first `cd` into that directory. Detailed recipes and
scheduled-job templates are in `recipes.md`, also next to this file.

## Setup (run once)

The only dependency is `requests`. Check, and install if missing:

```bash
cd <path-to-this-skill>/scripts
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
```

No API key or login is needed — the tool auto-scrapes Cineplex's public API key
at runtime and caches it in `.cineplex_key`.

## The 5 commands

Always add `--json` when you (the agent) are going to parse the output.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `movies` | List films / resolve a title → `filmId` | `--name`, `--json` |
| `locations` | List every theatre / resolve a name → `theatreId` | `--name`, `--json` |
| `theatres` | Theatres showing a film near a place (with distances) | `--film` (req), `--city`/`--latitude`/`--longitude`/`--postal`, `--name`, `--experiences`, `--json` |
| `showtimes` | Screening times for a theatre + date(s) | `--location` (req), `--date`/`--dates`, `--film`, `--experiences`, `--json` |
| `seats` | Live seat availability for one showtime | `--theatre` (req), `--showtime` (req), `--rows`, `--middle`, `--all`, `--map`, `--json` |

Run `python3 cineplex_showtimes.py <command> --help` for exact flags.

## Canonical workflow (name → IDs → times → seats)

The user usually gives names, not IDs. Resolve them first:

```bash
cd <path-to-this-skill>/scripts

# 1. Film name -> filmId
python3 cineplex_showtimes.py movies --name "Odyssey" --json
#    -> [{ "id": 37617, "name": "The Odyssey", ... }]

# 2. Theatre name -> theatreId
python3 cineplex_showtimes.py locations --name "Vaughan" --json
#    -> [{ "theatreId": 7408, "name": "Cineplex Cinemas Vaughan", ... }]

# 3. Screenings (one or many dates) -> each session's `sessionId`
python3 cineplex_showtimes.py showtimes --location 7408 --film 37617 \
  --dates 7/19/2026,7/20/2026 --experiences 70mm,imax --json
#    -> [{ "sessionId": 535317, "time": "11:00 AM", "seatsRemaining": 5, ... }]

# 4. Live seat availability for a session
python3 cineplex_showtimes.py seats --theatre 7408 --showtime 535317 --json
#    -> { "available": 179, "isSoldOut": false, "availableSeats": [...], ... }
```

Dates are `M/D/YYYY` or `YYYY-MM-DD`. `--experiences` is comma-separated
(`70mm`, `imax`, `ultraavx`, `dolby atmos`, `3d`, `vip`, `dbox`, ...).

## Common tasks

- **"Is the 7pm IMAX sold out?"** → `showtimes` gives `seatsRemaining` /
  `isSoldOut` per session directly; for exact open seats, follow with `seats`.
- **"Find 2 middle seats in row G or H."** →
  `seats --theatre T --showtime S --rows G,H --middle --json`, then read the
  `matched` array (only `status: "Available"` entries unless `--all`).
- **"Show me the seat map."** → `seats --theatre T --showtime S --map`
  (`.` = open, `#` = taken).
- **"Which theatres near me have it in 70mm?"** →
  `theatres --film <id> --latitude <lat> --longitude <lon> --experiences 70mm --json`.

## Scheduled jobs / monitoring

The user may want to *watch* for tickets or specific seats (e.g. "tell me when
G/H middle seats open up for the opening-night IMAX"). Because every command is
one-shot and JSON-clean, it composes into polling loops and cron/launchd jobs.
See **`recipes.md`** for ready-to-use templates:

- a short-lived bash polling loop,
- a recurring `cron` / macOS `launchd` job,
- a Python monitor that diffs availability and fires a notification.

When setting up a recurring job, always confirm the cadence with the user and
keep polling gentle (minutes, not seconds) — this hits Cineplex's real servers.

## Gotchas

- `seats --json` returns an **object**; the other four return a **JSON array**.
- `matched` only appears in `seats --json` when `--rows`, `--middle`, or `--all`
  is passed.
- `locations`/`theatres` records use `theatreId`; `showtimes` session records
  use `sessionId` (the vistaSessionId) — that's what `seats --showtime` wants.
- IDs are stable but the catalogue changes; always re-resolve names to IDs
  rather than hard-coding, especially in scheduled jobs.
- If a call ever fails with an auth error, delete `.cineplex_key` and retry —
  the tool re-scrapes a fresh key automatically.
