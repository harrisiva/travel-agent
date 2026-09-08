# travel-agent
Collection of skills I personally use to plan short trips (e.g., movies, weekend camping) and long trips (week long cross country camping)

## Skills

Each skill is self-contained under `.claude/skills/<name>/` — Claude Code picks
them up from this project, and the same directory zips up for upload to
claude.ai. No install step; the scripts are dependency-light Python 3.

| Skill | What it does |
| --- | --- |
| [`campsite-search`](.claude/skills/campsite-search/) | Campsite and cabin availability across nine Canadian Camis5 reservation systems (Parks Canada, Ontario, BC, GRCA, MB, NS, NB, NL, YT). Read-only — never books. |
| [`cineplex-showtimes`](.claude/skills/cineplex-showtimes/) | Cineplex showtimes, theatres, films, and live seat availability. |

Verify a skill still works against the live APIs:

```sh
cd .claude/skills/campsite-search/scripts && python3 test_availability.py
cd .claude/skills/cineplex-showtimes/scripts && python3 cineplex_showtimes.py locations
```
