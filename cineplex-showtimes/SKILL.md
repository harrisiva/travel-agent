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

A small Python client for Cineplex's public theatrical + ticketing APIs:

- `scripts/cineplex_showtimes.py` — the CLI, 5 subcommands, each supports `--json`.
- `scripts/cineplex_api.py` — the importable library behind it.
- `scripts/test_cineplex.py` — self-check (`--offline` for logic only).

**Read-only.** It queries showtimes and seat maps; it never holds, books or
pays for a seat. **Nothing volatile is cached** — every showtime and seat
answer is fetched live. The only thing cached is Cineplex's public API key
(`.cineplex_key`), which the tool scrapes from cineplex.com itself.

All commands below assume you first `cd` into the `scripts/` directory next to
this file. Watch/cron templates are in `recipes.md`, also next to this file.

## Setup (run once)

```bash
cd <path-to-this-skill>/scripts
python3 -c "import requests" 2>/dev/null || python3 -m pip install -q requests
```

No API key, login or configuration.

## Choosing a command

| Question | Command |
|---|---|
| Film title → `filmId` | `movies --name` |
| Theatre name → `theatreId` | `locations --name` |
| What's on at a theatre on a date; is a showing sold out | `showtimes` |
| Which theatres are showing a film | `theatres` |
| Exactly which seats are open; are the middle of G/H free | `seats` |

| Command | Flags |
|---|---|
| `movies` | `--name`, `--language`, `--json` |
| `locations` | `--name`, `--language`, `--json` |
| `theatres` | `--film` (req), `--city`, `--region`, `--region-code`, `--country` (default Canada), `--latitude`/`--longitude`, `--postal`, `--accuracy` (km, default 5), `--name`, `--experiences`, `--language`, `--json`. **No date** — it answers "is showing this film", not "on this day". |
| `showtimes` | `--location` (req, a `theatreId`), `--date` or `--dates d1,d2`, `--film`, `--experiences`, `--language`, `--json` |
| `seats` | `--theatre` (req), `--showtime` (req, a `sessionId`), `--rows G,H`, `--middle`, `--all`, `--map`, `--json` |

`--language en|fr` sets the language of the *response* (titles, labels). It
does not filter to French-language screenings.

## Canonical workflow (name → IDs → times → seats)

```bash
python3 cineplex_showtimes.py movies --name "Odyssey" --json       # -> [{"id": <FILM_ID>, ...}]
python3 cineplex_showtimes.py locations --name "Vaughan" --json    # -> [{"theatreId": 7408, ...}]
python3 cineplex_showtimes.py showtimes --location 7408 --film <FILM_ID> \
  --dates <YYYY-MM-DD>,<YYYY-MM-DD> --experiences 70mm --json       # -> [{"sessionId": <SESSION_ID>, ...}]
python3 cineplex_showtimes.py seats --theatre 7408 --showtime <SESSION_ID> --rows G,H --middle --json
```

Always resolve names; never reuse an ID from an earlier conversation. Session
IDs die when the showing starts.

## Exit codes

Identical with and without `--json`:

| Code | Meaning |
|---|---|
| `0` | found something |
| `1` | query worked, nothing found — no showtimes on those dates; `seats`: sold out, or no **open** seat matched `--rows`/`--middle` |
| `2` | usage or lookup error — bad date, unknown experience, a row not in that auditorium, unknown theatre/film/showtime id, a showtime that has already started |
| `3` | network or API error — including an empty or malformed answer (an empty seat map is never reported as sold out) |

Only `1` means "keep waiting" in a watch. `3` means try later; `2` means stop
and fix the command. Messages go to stderr; `--json` stdout stays clean (`[]`
on exit 1 — for `seats`, its usual object — and empty on 2/3).

## `--experiences`

Comma-separated; a session matches if it has **any** of them. Case, spaces and
punctuation don't matter, and age limits are ignored, so `70MM`, `vip`
(= `VIP 19+`/`VIP 18+`), `d-box`, `dolby atmos` all work. Short forms: `avx`,
`atmos`, `laser`, `standard`.

Labels Cineplex uses: Regular, Recliner, UltraAVX, Dolby Atmos, D-BOX, 3D,
Laser Projection, VIP 19+, VIP 18+, IMAX, ScreenX, 70mm, 4DX, Clubhouse.
One screening often carries several (`IMAX` + `70mm`, `UltraAVX` + `Dolby Atmos`).

- **`showtimes`** filters on the session's own labels, so every label above
  works. A token that is neither one of those labels nor on any session in
  the response exits 2 rather than returning nothing; a label Cineplex adds
  later works on any day it is actually playing.
  To mean "IMAX **and** 70mm", filter on `70mm` and check `experience` in the output.
- **`theatres`** can only filter server-side. It supports every label above
  **except Dolby Atmos and Clubhouse** (exit 2 — use `showtimes --experiences`
  per theatre instead).

## Gotchas

- `seats --json` returns an **object**; the other four return a **JSON array**.
- `matched` appears in `seats --json` only when `--rows`, `--middle` or `--all`
  is passed. `--all` includes taken seats in `matched`; the exit code still
  counts only open ones.
- `--middle` is the central third of each row, by seat count.
- `seatsRemaining` in `showtimes` is enough for "is it sold out?"; use `seats`
  only when rows or specific seats matter.
- `distanceKm` in `locations`/`theatres` is measured from wherever the request
  comes from (IP geolocation), not from `--city`. Don't report it as distance
  from the user.
- A stale API key is handled automatically: a 401 triggers one re-scrape and
  retry. Only if that also fails do you get exit 3.

## Reporting back

Lead with the answer: "Yes — 8 seats left for the 6:45 PM VIP, all in the
front rows AA and A", not a JSON dump. Quote the time, the format
(`IMAX · 70mm`), the auditorium and the rows that are open. For "is it sold
out", say how many seats are left. When a watch is set up, say what it checks,
how often, how it stops, and that it never books. A cron/launchd job stops
itself after the first hit (a sentinel file, see `recipes.md`) — tell the user
to remove the job afterwards, or once the showing has started.

## Scheduled jobs / monitoring

Every command is one-shot with stable exit codes, so it composes into polling
loops and cron/launchd jobs. See **`recipes.md`**. Always confirm the cadence
with the user and keep polling gentle (minutes, not seconds) — these are
Cineplex's live servers.
