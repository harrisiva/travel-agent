# travel-agent

Skills for planning trips with Claude — short ones (what's playing tonight,
a weekend campsite) and long ones (a week of cross-country camping).

Each skill is one directory at the root of this repo. They are self-contained:
Python 3, no API keys, no configuration. The one dependency is `requests`, and
each skill installs it itself if it's missing — on claude.ai there is nothing
for you to set up.

| Skill | What it does |
| --- | --- |
| [**campsite-search**](campsite-search/) | Campsite and cabin availability across nine Canadian park systems |
| [**cineplex-showtimes**](cineplex-showtimes/) | Cineplex showtimes, theatres, films and live seat availability |

### Where these work

**Claude** — claude.ai, Claude Code, and the desktop and mobile apps. `.skill` is
Claude's Skills format, and in Claude Code installing them is a single sentence
you paste — see [below](#installing-them--ask-claude-to-do-it).

**Not ChatGPT**, and not only because the format differs. Both skills are thin
clients over live HTTP APIs — they hold no local data, so every useful command
makes an outbound request. ChatGPT's Python sandbox has no network access, so
the scripts would fail on the first call even if pasted in directly. Porting
them would mean rebuilding on Custom GPT Actions, which do get network but
can't run this Python.

Outside Claude entirely, both are ordinary CLIs — see
[plain command-line tools](#using-them-as-plain-command-line-tools) below.

---

## Installing them — ask Claude to do it

If you have **Claude Code** (terminal or the desktop app), don't install these
by hand. Open it anywhere and paste:

> *Install the travel skills from https://github.com/harrisiva/travel-agent for
> me globally — clone it somewhere sensible, symlink each skill directory into
> `~/.claude/skills/`, and tell me what you installed.*

That's the whole thing. Global means every project and every conversation, not
just the folder you happened to be in. Ask it to install just one skill, or to
put them in a single project's `.claude/skills/` instead, if you'd rather.

To remove them later, ask for that too — *"uninstall the travel skills"*.

### On claude.ai (web and mobile)

This is the one route Claude can't do for you: uploading a skill is a settings
action in your account, so it takes two clicks.

1. Download the skill you want from [**dist/**](dist/) —
   `campsite-search.skill` or `cineplex-showtimes.skill`. (On GitHub: open the
   file, then **Download raw file**.)
2. In Claude, open **Settings → Capabilities → Skills** and upload the file.

Nothing else — no Python, no clone, no keys.

### Then just ask

However you installed it, the skill activates on its own when a question
matches it — there is no command to remember and nothing to invoke. See
[sample prompts](#sample-prompts) for what each one can answer.

### Working on the skills themselves

Clone the repo and open Claude Code in it — `.claude/skills/` symlinks to the
directories at the root, so they're picked up with no install step and there is
only ever one copy of each file.

```sh
git clone https://github.com/harrisiva/travel-agent.git
cd travel-agent
claude
```

## Sample prompts

Copy one, or ask in your own words — these are just the shapes each skill
handles well.

### campsite-search

> *Are there any campsites left at Bon Echo the last weekend of July?*
>
> *Anything at all open in Algonquin on a Friday or Saturday night between now
> and the end of September?*
>
> *I want an electrical site at Pinery for three nights in August — which ones
> are free, and are any of them private or barrier-free?*
>
> *Find me a cabin, yurt or oTENTik somewhere in Ontario Parks for the October
> long weekend.*
>
> *When do 2027 reservations open for Banff, and how far ahead can I book?*
>
> *Site 412 at Sandbanks — show me its calendar for July.*
>
> *Bon Echo is sold out for that weekend. Watch it and tell me if anything
> cancels.*
>
> *Any alerts or closures I should know about for Killarney right now?*

### cineplex-showtimes

> *What's playing at the Waterloo Cineplex tonight?*
>
> *Is the 7pm IMAX showing of The Odyssey in Waterloo sold out?*
>
> *Which theatres near Toronto are showing Dune in 70mm this weekend?*
>
> *I want two seats together in the middle of the theatre — which showing this
> Saturday still has them?*
>
> *Anything in Dolby Atmos in Mississauga on Friday after 6pm?*
>
> *Book-club night is Thursday — find a showing where eight of us can sit in
> one row.*
>
> *Tickets aren't out yet for the Friday IMAX. Check every morning and tell me
> when they are.*

## Using them as plain command-line tools

Neither skill needs Claude at all — both are ordinary CLIs, and every command
takes `--json`.

```sh
cd campsite-search/scripts    && python3 -m campsites --help
cd cineplex-showtimes/scripts && python3 cineplex_showtimes.py --help
```

---

## campsite-search

Availability across the **Camis5** reservation platform, shared by nine
Canadian park systems: Parks Canada, Ontario Parks, BC Parks, Grand River
Conservation Authority, Manitoba, Nova Scotia, New Brunswick, Newfoundland &
Labrador, and Yukon.

**Read-only — it never books anything.** Availability is never cached, because a
stale availability answer is worse than none. Reference data (park lists,
equipment, site metadata) is cached, since it changes rarely.

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
they run different platforms, behind Queue-it waiting rooms or CAPTCHA. Run
`providers` for the current list and the reason for each.

Worked end-to-end workflows are in [`campsite-search/recipes.md`](campsite-search/recipes.md).

## cineplex-showtimes

Showtimes, theatres, films and **live seat availability** from Cineplex Canada's
public theatrical and ticketing APIs.

| Command | What it answers |
| --- | --- |
| `showtimes` | Times for a theatre across one or more dates, filterable by film, experience (IMAX, UltraAVX, 3D, Dolby Atmos) or language |
| `seats` | Live seat map for one showtime — how many are left, which rows, whether the good middle seats are gone |
| `theatres` | Which theatres near a place are showing a given film |
| `movies` | Every film currently listed, with its ID |
| `locations` | Every theatre, with its ID and distance |

For the sold-out question specifically: `showtimes` gives a seat count per
screening, and `seats` breaks one screening down row by row.

Recipes — including watching a sold-out screening on a schedule — are in
[`cineplex-showtimes/recipes.md`](cineplex-showtimes/recipes.md).

---

## Checking a skill still works

These APIs are public but undocumented, so they can change without warning.
`campsite-search` ships a self-check; `cineplex-showtimes` is verified by any
live call.

```sh
cd campsite-search/scripts
python3 test_availability.py              # 16 checks, 10 offline + 6 live
python3 test_availability.py --offline    # logic only, no network

cd cineplex-showtimes/scripts
python3 cineplex_showtimes.py locations
```

A `[network]` failure names the host, so a tenant being down is easy to tell
apart from the skill being broken.

## Contributing

The directories at the repo root are the source of truth. Edit those, re-run the
self-check above, then rebuild the uploadable archives:

```sh
./build.sh          # regenerates dist/*.skill
```

If you change a skill, please rebuild `dist/` in the same commit — that's what
non-technical users download, and it's easy to leave behind.

[**CLAUDE.md**](CLAUDE.md) documents the conventions for adding a new skill and
the patterns worth copying from the existing two — CLI shape, exit codes,
caching policy, what to put in a `SKILL.md`, and the traps these two hit.
Coding agents pick it up automatically (`AGENTS.md` symlinks to it); it is
worth reading first if you're adding a skill by hand.

## License

[Apache 2.0](LICENSE)
