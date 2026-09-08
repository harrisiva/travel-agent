# Working in this repo

This repo packages **Claude Skills** for trip planning. Each skill is a Python
CLI wrapping a live public API, plus a `SKILL.md` telling Claude how to drive it.

Read this before adding or changing a skill. The conventions below are not
stylistic — most of them exist because the alternative already broke something.

## Layout

```
<skill-name>/
├── SKILL.md          # how Claude drives the tool — the interface contract
├── recipes.md        # worked end-to-end workflows, loaded on demand
└── scripts/          # the CLI itself
```

Skill directories live at the **repo root**. `.claude/skills/` holds symlinks to
them, so Claude Code discovers them with no second copy on disk.

### One source of truth, always

The root directory is the only copy. `build.sh` zips it into `dist/*.skill`; it
never writes back. If you add tooling, keep that property.

This is load-bearing. A sibling repo had a script that copied a source package
over the skill bundle, the bundle had been edited directly, and the copy
silently reverted three bug fixes — including one that crashed on real park
data. Any script that copies between two live copies will eventually run in the
wrong direction. Don't create the second copy.

## Adding a new skill

1. Create `<skill-name>/` at the root with the layout above.
2. Symlink it: `ln -sfn ../../<skill-name> .claude/skills/<skill-name>`
3. Write a self-check (see below) and run it.
4. `./build.sh` to regenerate `dist/`.
5. Add it to the README table and give it a section.

Commit `dist/` in the same commit as the source change. That's what
non-technical users download, and it is easy to leave stale.

## The pattern that works here

Both skills follow the same shape, and it's a good default for anything that
answers questions about a live website.

**Find the JSON API, don't scrape the HTML.** Every site with a date picker or
a seat map is calling its own endpoint. Open devtools, watch the network tab,
find the request that returns the data. Scraping rendered DOM breaks on every
redesign; the JSON endpoint behind it rarely changes shape.

**Subcommands that map to questions, not to endpoints.** `sweep` ("any opening
in this range?") is a real question; a thin wrapper over one endpoint is not.
Let one command make several calls if that's what answering takes.

**Name → ID resolution is its own command.** Claude gets a park name or a
theatre name from the user, and the API wants an integer. `movies`, `locations`,
`parks` exist so the agent can chain: resolve, then query. Never make Claude
hard-code an ID — catalogues change.

**`--json` on every command.** Human-readable tables by default, structured
output for chaining. Both are used constantly.

**Stable exit codes.** `campsites` uses:

| Code | Meaning |
|---|---|
| `0` | found something |
| `1` | query worked, nothing available |
| `2` | usage/lookup error |
| `3` | network or API error |

The distinction between `1` and `3` is what makes a polling loop safe: only `1`
means keep waiting. Without it, an agent watching a sold-out campground will
happily loop forever on a network outage. Keep codes identical under `--json`.

**Read-only by default.** `campsite-search` queries reservation systems and
never books. If a skill could take an action with real-world consequences,
don't add it without asking — and never put it behind a flag that's easy to
pass accidentally.

**Cache the stable thing, never the volatile thing.** Park lists and site
metadata cache for days; availability is never cached, because a stale
"available" is worse than no answer. Whatever your skill's equivalent of
"availability" is, don't cache it, and say so in `SKILL.md`.

**Refuse rather than return a false negative.** The Camis5 API returns an empty
body — not an error — for spans over 367 days. Reported naively that reads as
"nothing available". The CLI rejects the span instead. Look for these: an API
that answers "nothing" when it means "bad request" will make your skill
confidently wrong.

**Put a ceiling on request fan-out.** `find` can sweep every park in a region.
It refuses up front if the plan exceeds `--max-requests` (default 200). An agent
will cheerfully issue thousands of requests otherwise.

**Assume a hostile environment.** The skill runs wherever Claude is, which may
be a container with no `HOME`, a read-only filesystem, no `curl`, and a stale CA
bundle. `campsites` degrades to an in-memory cache when no directory is
writable, and tries truststore → system CA bundle → curl before giving up on
TLS. It never disables certificate checking. Absolute-path invocation from a
foreign working directory must work — that's what the `campsites.py` launcher
shim is for.

## Self-checks

Ship one. `campsite-search/scripts/test_availability.py` is the model:

- Two labelled groups: `[offline]` (pure logic, no sockets) and `[network]`
  (live calls). `--offline` runs only the first.
- A `[network]` failure names the host, so "the tenant is down" is
  distinguishable from "the skill is broken".
- Exits non-zero on failure so it can gate an install.

Test the things that fail *silently*. The bug worth catching is not a crash —
it's the answer that looks plausible and is wrong.

One real example: site names sort naturally, and the original comparator mixed
`int` and `str`, so any park with lettered sites (Killarney's `Y3`, Sandbanks'
`A209`) raised `TypeError` and took the whole search down. It passed every test
until someone tested a park with letters in its site names. Use real fixtures
from the actual API.

## Writing SKILL.md

It is the interface contract, not a README. Assume Claude is competent and
skip the obvious; spend the space on what it would otherwise get wrong.

- **Lead with when to use each command.** A "choosing a command" section
  prevents most misuse.
- **Document flags that mean different things in different commands.**
  `--end` in `search` vs `sweep` has its own section for exactly this reason.
- **Document shape inconsistencies.** `seats --json` returns an object; the
  other four commands return arrays. Undocumented, that's a guaranteed bug.
- **Say what the defaults hide.** A default search only sees one booking
  category — a caveat that changes answers.
- **Include a "reporting back" section.** Tell Claude to lead with the answer
  and to quote the details that matter to a human (site descriptions, seat
  rows). Without it you get a wall of JSON.
- **Keep long workflows in `recipes.md`.** `SKILL.md` is always in context;
  recipes are loaded when needed.

## Before you commit

```sh
cd <skill-name>/scripts && python3 test_availability.py   # or a live call
./build.sh
```

Check the built archive has exactly one top-level directory and no
`__pycache__`, `.pyc`, `.DS_Store`, or cached credentials. `build.sh` excludes
these; verify anyway if you added a new artifact type.

If you change a skill's scripts, the copy uploaded to claude.ai is now stale.
Say so — it needs re-uploading by hand.

## Dependencies

Python 3 and `requests`. Each `SKILL.md` opens by installing `requests` if it's
missing. Keep it there; don't add dependencies without a strong reason, and
vendor nothing — these bundles are downloaded and uploaded by hand, and every
megabyte is friction.
