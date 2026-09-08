# travel-agent

Collection of skills I personally use to plan short trips (e.g., movies, weekend
camping) and long trips (week long cross country camping).

Each skill lives in `.claude/skills/<name>/` and is fully self-contained — the
Python it needs is bundled alongside it. Claude Code picks them up automatically
in this repo, and the same directory zips up for upload to claude.ai, so the
desktop and web copies stay identical.

Requires Python 3. No install step, no third-party packages.

---

## `campsite-search`

Campsite and cabin availability across the **Camis5** reservation platform,
which nine Canadian park systems share: Parks Canada, Ontario Parks, BC Parks,
Grand River Conservation Authority, Manitoba, Nova Scotia, New Brunswick,
Newfoundland & Labrador, and Yukon.

**Read-only — it never books anything.** Availability is never cached, because a
stale availability answer is worse than none; reference data (park lists,
equipment, site metadata) is cached since it changes rarely.

| Command | What it answers |
| --- | --- |
| `search` | Is this park free on these exact dates? |
| `sweep` | Any opening at all across a date range? |
| `find` | Sweep *every* park matching a pattern — all of Algonquin at once |
| `site` | Day-by-day calendar for one named site |
| `stays` | What's bookable that isn't a tent pad — cabins, yurts, oTENTiks, huts |
| `window` / `horizon` | Operating season, when booking opens, how far ahead you can book |
| `attrs` / `equipment` | Filterable site attributes (electric, pull-through, private, barrier-free) and booking category IDs |
| `alerts` | Park alerts and closures |
| `parks` / `providers` | Park lists per system; which systems are supported |
| `cache-clear` | Empty the on-disk reference cache |

Alberta, Saskatchewan, PEI, Québec and NWT are deliberately **not** supported —
they run different platforms behind Queue-it waiting rooms or CAPTCHA. Run
`providers` for the current list and the reason for each.

```sh
cd .claude/skills/campsite-search/scripts
python3 -m campsites providers
python3 -m campsites sweep --help
```

Worked end-to-end workflows live in `campsite-search/recipes.md`.

## `cineplex-showtimes`

Showtimes, theatres, films and **live seat availability** from Cineplex Canada's
public theatrical and ticketing APIs.

| Command | What it answers |
| --- | --- |
| `showtimes` | Times for a theatre across one or more dates, filterable by film, experience (IMAX, UltraAVX, 3D, Dolby Atmos) or language |
| `seats` | Live seat map for one showtime — how many are left, which rows, whether the good middle seats are gone |
| `theatres` | Which theatres near a place are showing a given film |
| `movies` | Every film currently listed, with its ID |
| `locations` | Every theatre, with its ID and distance |

Useful for the sold-out question specifically: `showtimes` reports a seat count
per screening, and `seats` breaks one screening down row by row.

```sh
cd .claude/skills/cineplex-showtimes/scripts
python3 cineplex_showtimes.py locations --name Waterloo
python3 cineplex_showtimes.py showtimes --location 7268
```

Every subcommand takes `--json` for scripting. Recipes — including watching a
sold-out screening on a schedule — are in `cineplex-showtimes/recipes.md`.

---

## Checking a skill still works

The APIs are public and undocumented, so they can move without warning.
`campsite-search` ships a self-check; `cineplex-showtimes` is verified by any
live call.

```sh
cd .claude/skills/campsite-search/scripts
python3 test_availability.py              # 16 checks, 10 offline + 6 live
python3 test_availability.py --offline    # logic only, no network

cd .claude/skills/cineplex-showtimes/scripts
python3 cineplex_showtimes.py locations
```

## Keeping copies in sync

These skills also exist in their own source repos (`campsite-searcher`,
`cineplex-scalper`) and as uploads on claude.ai. The bundles here are the
canonical, verified copies — when editing, change the bundle and propagate
outward, and re-run the self-check before uploading anywhere.
