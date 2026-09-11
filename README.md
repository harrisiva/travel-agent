# travel-agent

Skills for planning trips with Claude — short ones (what's playing tonight,
a weekend campsite) and long ones (a week of cross-country camping).

Each skill is one directory at the root of this repo. They are self-contained:
Python 3, no API keys, no configuration. Most need only `requests`, which each
skill installs itself if it's missing; `google-maps` needs nothing at all
beyond the standard library. Skills that reach the network need egress enabled
on claude.ai — see each skill's section.

| Skill | What it does |
| --- | --- |
| [**campsite-search**](campsite-search/) | Campsite and cabin availability across nine Canadian park systems |
| [**cineplex-showtimes**](cineplex-showtimes/) | Cineplex showtimes, theatres, films and live seat availability |
| [**google-maps**](google-maps/) | Places near a location — live status, full opening hours, and travel time on foot, by car, bike or transit |
| [**enterprise-rentals**](enterprise-rentals/) | Enterprise rental car prices worldwide — live availability, real make/model, mileage terms and trip totals |
| [**kayak-browse**](kayak-browse/) | Rental cars, hotels and cheapest flight dates across hundreds of providers via KAYAK's affiliate API — **needs an approved API key; never run live** |
| [**google-flights**](google-flights/) | Live flight prices and schedules, plus Google's own "is this a good price?" verdict and 60 days of fare history |

### Where these work

**Claude** — claude.ai, Claude Code, and the desktop and mobile apps. `.skill` is
Claude's Skills format, and in Claude Code installing them is a single sentence
you paste — see [below](#installing-them--ask-claude-to-do-it).

**Not ChatGPT**, and not only because the format differs. These skills are thin
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

### google-flights

> *What does it cost to fly Toronto to Halifax on the 25th and back on the 27th?*
>
> *Is that a good price, or should I wait? What's it normally?*
>
> *I can leave any day in the next three weeks — which departure date is
> cheapest for a two-night trip?*
>
> *What's the airport code for Toronto, and does London have more than one?*
>
> *Nonstop only, leaving after 8am, and I'd rather not connect through
> Toronto — what's left?*
>
> *Which flight to London has the lowest emissions, and which has the most
> legroom?*
>
> *Business class for two adults and a child to Lisbon in March — what's the
> damage?*
>
> *Watch the Toronto–Halifax fare over Christmas and tell me if it drops under
> $600.*

### google-maps

> *What's open near me right now for dinner, within about a ten minute drive?*
>
> *Find me a highly rated ramen place around Kensington Market that's still
> serving.*
>
> *How long does it take to drive from Union Station to Pearson, with traffic?*
>
> *What's within a 15 minute walk of my hotel — I don't have a car.*
>
> *Can I get to the ROM by transit from here, and how long does it take?*
>
> *What are Richmond Station's hours for the whole week?*
>
> *Anywhere near Kensington Market still serving at 11pm on Saturday?*
>
> *Is that place still open, and what time does it close tonight?*

### kayak-browse

> *What's the cheapest car I can pick up at Toronto Pearson on Dec 20 and drop back on the 23rd?*
>
> *I need an SUV big enough to sleep in for a week out of Denver — automatic, unlimited mileage, free cancellation.*
>
> *Same car, same city — is it any cheaper if I shift the pickup a day either way?*
>
> *Pick up in Vancouver, drop off in Calgary. What does a one-way run me?*
>
> *The cheapest one hides the agency until you've paid — is that worth it, or should I take the Hertz one?*
>
> *When in March is it cheapest to fly from New York to Lisbon?*
>
> *Find me hotels in Austin for the nights of the 14th and 15th.*
>
> *What should I search for "Newark" — does KAYAK list a downtown pickup point as well as the airport?*

### enterprise-rentals

> *I need a car at Halifax airport from Oct 15 to 18 — cheapest thing that
> fits five people and their luggage?*
>
> *Is any week in October cheaper than the others for a rental in Vancouver?*
>
> *We're driving Halifax to Moncton one way in November — can I drop it there,
> and how many fewer cars can I choose from?*
>
> *My son is 22 and wants to rent in Denver for a week — how much extra, and
> are there cars he isn't allowed to take?*
>
> *Something I can sleep in for a two-week road trip. AWD, and it has to be
> unlimited mileage.*
>
> *Is it cheaper to pick up at Heathrow or somewhere in central London for the
> same four days?*
>
> *I need an automatic in Frankfurt — half the cheap ones there are manuals.*
>
> *If my flight lands at 11pm on the 15th, can I still collect the car — and
> can I drop it back on Sunday night?*

## Using them as plain command-line tools

None of these needs Claude at all — they are ordinary CLIs, and every command
takes `--json`.

```sh
cd campsite-search/scripts    && python3 -m campsites --help
cd cineplex-showtimes/scripts && python3 cineplex_showtimes.py --help
cd enterprise-rentals/scripts && python3 enterprise.py --help
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

## google-maps

Places near a location, with **live** open/closed status, the full seven-day
schedule, and travel time by car, foot, bike or transit — including live
traffic. Read-only: it never books or contacts anywhere.

Everything comes from Google's own endpoints. No API key, no login, and **no
dependencies at all** — standard library only.

| Command | What it answers |
| --- | --- |
| `nearby` | What's open around here, ranked by how long it takes to get there |
| `search` | Places matching a query, with hours, rating and address (phone is in `--json`) |
| `hours` | One place's full week |
| `travel` | Time and distance to one or more places, in one request |
| `geocode` | A place name or address to coordinates |

**`--mode` is the flag that matters most.** `walk`, `bike`, `transit` and
`drive` give genuinely different answers: a place 600 m away is "8 minutes" by
car and a 7-minute walk, and in a city the walk is usually the real answer.
Transit returns actual departure and arrival times.

`--open-at "Fri 21:00"` is interpreted in the *place's* local time — what you
mean when you ask whether somewhere will still be open when you land.

Places that publish no hours are reported as *unknown*, never as closed. Travel
times say whether they include live traffic. There is no price data on these
endpoints, and the skill says so rather than guessing.

### On claude.ai, enable network access first

claude.ai's code-execution sandbox blocks all domains except package managers by
default, so this skill fails until you change that: **Settings → Capabilities →
Code execution → network access for all domains** (or allowlist
`www.google.com`), then **start a new chat** — the setting does not apply to the
conversation you changed it in. Needs a paid plan with code execution. In Claude
Code it works with no setup. (These claude.ai instructions are derived
from Anthropic's documentation and have not been tested inside that sandbox.)

### Known gaps

No **search-along-a-route** command — the "where can we stop on the way?"
question is answered by interpolating waypoints and searching around each
(recipe 5, tested, with its caveats). No automatic radius widening when nothing
is open; retry at a larger `--span`. **No price data** exists on these
endpoints, so the skill says so rather than guessing. Reviews, photos, busyness,
accessibility and reservation links are not surfaced.

These are Google's own internal endpoints rather than a supported API, so they
can change without notice; a shape change exits `3`, never "nothing found".

Recipes — walking vs driving, the along-a-route workaround, watching for an
opening — are in [`google-maps/recipes.md`](google-maps/recipes.md).

---

## enterprise-rentals

Live rental car prices from Enterprise, anywhere Enterprise operates.
Read-only — it quotes and compares, and has no code path that can book.

Verified live in **nine countries on four continents** — Canada, the US, the
UK, Germany, France, Spain, Ireland, New Zealand and Japan — each priced in the
branch's own currency. Vehicle labels come back translated (`Mietwagen`,
`Kleinbusse`); filters match the API's numeric facet codes instead, so
`--class van` finds a German minibus.

One request returns the **whole fleet** for a branch and date pair, so a quote
carries the real make and model, seats, luggage capacity, drivetrain, fuel
type, transmission, mileage terms, the trip total and the Enterprise Plus
points price — all at once.

**The `PER DAY` column is the trip total divided by the rental days**, not
Enterprise's own rate line, and the footer says so. Enterprise quotes some
classes at a weekly rate, which read as a daily price would understate a week
several times over; `--json` keeps the raw figure (`api_rate`,
`api_rate_period`) and the derived one (`per_day_effective`) apart.

| Command | What it answers |
| --- | --- |
| `locations` | Which branch do you mean, and what is its id |
| `quote` | What can I rent here on these dates, and what does it cost |
| `sweep` | Is a different week cheaper |
| `compare` | Airport or downtown; this city or that one |
| `watch` | Tell me when a sold-out class frees up |
| `branch` | Counter hours and after-hours drop-off, for any date |
| `doctor` | Why did that fail — environment and transport check |
| `cache` | Show or clear the cached branch lookups |

**Prices are what the branch charges.** `--currency` adds a labelled estimate
(`436.00 CAD (~315.10 USD est.)`) but never replaces the billed figure, because
quoting a converted number as the price is the worst mistake this skill could
make.

**Age is not cosmetic.** Under-25 renters pay more *and* cannot rent every
class — at Halifax, 20 bookable classes at 25 becomes 13 at 21. Pass
`--age 21 --age 25` to price both at once and see the classes that disappear.

**Sold-out classes stay visible in the count**, never silently dropped: the
footer reads `20 of 59 classes bookable` with a per-category breakdown, because
"every minivan is gone" is sometimes the answer. The API reports age-restricted
classes as sold out too, so for an under-25 renter the two are indistinguishable
— the skill says so rather than sending you hunting for better dates.

**Commercial terms are not in this API.** Deposit, insurance, fuel policy,
cancellation and additional-driver fees are not available from any command; the
skill says it cannot tell you rather than guessing.

Branch names are ambiguous — `Halifax` matches the airport, the train station
and two **Exotic** branches with a different fleet at much higher prices. The
tool refuses to guess and lists the candidates.

**`--country` is a search hint, not a filter** — asking for Australia returns
Sydney, *Nova Scotia*, and `--country BO` returns La Paz, *Mexico*. Every
listing carries a `CTRY` column, and a resolved branch outside the country you
asked for triggers a loud warning before it is priced.

Cross-border one-way rentals are refused by Enterprise using the *same* message
as a genuine sell-out. The skill exits with a usage error and says the route is
probably not permitted, rather than sending you hunting for dates that will
never work.

Recipes — cheapest-week sweeps, young-driver pricing, road-trip vehicles,
points-vs-cash — are in
[`enterprise-rentals/recipes.md`](enterprise-rentals/recipes.md).

---

## kayak-browse

Rental cars, hotels and cheapest-flight-dates from **KAYAK's affiliate APIs** —
one search covers hundreds of providers. This exists because the direct
supplier sites are closed: avis.ca and budget.ca both answer `403` behind
DataDome on their pricing endpoint, from `curl` and from a real browser alike,
so an aggregator API is the only way in.

**Read-only — it never books, holds or pays for anything.** Prices are never
cached; place lookups are cached for days.

| Command | What it answers |
| --- | --- |
| `cars` | What can I rent here on these dates, and on what terms? |
| `sweep` | Which pickup day in this range is cheapest? |
| `places` | What id does KAYAK use for this airport or city? |
| `when` | What's the cheapest date to fly this route? |
| `hotels` | What's available for these nights? |
| `check` | Is my API key alive? |
| `login` | Validate and store a key once |

Filters run client-side, so `cars` takes the questions the website won't:
`--sleepable` (an SUV or van with room for four), `--unlimited-mileage`,
`--free-cancellation`, `--no-credit-card`, `--exclude-opaque`, `--max-price`,
and the usual class, seat, bag, transmission and fuel constraints. When a
filter empties the result, the tool reports *which* one did it.

**This is the one skill in this repo that is not self-contained.** It needs a
KAYAK affiliate API key, which KAYAK emails to the partner on signup — set
`KAYAK_API_KEY`, or run `login` once with a key file. Two consequences worth
knowing before you install it:

- **Sandbox prices are mock data.** The tool refuses to print a price column or
  rank a sweep against the sandbox host unless you pass `--sandbox-ok`, and
  every row is marked `priceIsReal: false`. A fabricated price presented as a
  real quote is the worst thing this skill could do, so the guard is structural
  rather than a warning in the docs.
- **Sandbox keys expire every three months.** An expired key exits `4`, which
  never means "nothing available".

**Not yet verified against the live API.** No key existed when this was built,
so every test runs against fixtures written from KAYAK's published RAML spec.
That is a real limitation, not a formality: the worst bug found during
development was a misspelled request field that silently disabled two critical
settings, and the test written to catch it asserted the same misspelling.
Re-recording the fixtures against a production key is the first job once access
exists. Three things stay open until then — whether sandbox signup is
self-serve or needs business approval, what production rate limits look like,
and whether the cars API exposes Canadian inventory usefully (the sandbox is
US-only).

Worked workflows — sweeps, one-ways, watch loops, `--json` — are in
[`kayak-browse/recipes.md`](kayak-browse/recipes.md).

## google-flights

Live search against **Google Flights**, which server-renders its whole result
set into the page — so there is no JSON endpoint to call, no API key, and no
browser needed. The client parses that payload directly.

| Command | What it answers |
| --- | --- |
| `airports` | What's the airport code for Toronto? Does London have several? |
| `search` | What flies this route on these dates? |
| `price-check` | Is this fare good, or is it worth waiting? |
| `cheapest` | Which departure date in a window is cheapest? |
| `route` | Who flies this route, and what can I filter on? |
| `watch` | Has the fare dropped under my threshold yet? |

```sh
cd google-flights/scripts
python3 flights.py airports Toronto
python3 flights.py search YYZ YHZ --depart 2026-09-25 --return 2026-09-27
python3 flights.py price-check YYZ YHZ --depart 2026-09-25 --return 2026-09-27
python3 flights.py cheapest YYZ YHZ --depart +7 --return +9 --days 21
```

Dates are `YYYY-MM-DD` or `+N` days from today. Filters that Google applies
server-side (`--max-stops`) change what is searched; the rest — `--max-price`,
`--airlines`, `--avoid-layovers`, `--depart-after`, `--max-co2` and friends —
are applied to the results and cost no extra requests.

`price-check` is the one worth knowing about. Google publishes its own verdict
on a route's current fare along with about 60 days of history, so the tool can
answer *"is $231 good?"* with **"typical — against a usual $228, in a
$190–$345 range"**, which no listing scraper can do.

Three things to keep straight. On a round trip the price is the **whole trip**
but the legs shown are the **outbound only** — every itinerary says which via
`price_covers`. Prices cover the **whole party**, so `--adults 2` roughly
doubles them. And on `cheapest`, `--return` sets the trip *length* rather than
a fixed return date, so the sweep slides both ends together — each row reports
the return it actually priced.

Fares are never cached — a stale "cheap" answer is the one that costs the user
money. Exit `1` means the query worked and nothing matched; exit `2` means the
question as asked cannot be answered, and the tool says why rather than
returning an empty list (a past date, a `--max-price` below the route's
cheapest fare, an airline that does not fly it, a metro code that already
contains the destination, or a currency Google did not actually price in); exit
`3` means the check itself failed, including Google serving a captcha instead
of results. Only `1` is safe for a watch loop to keep polling on.

Request fan-out is capped: 5 requests for a single search, 40 at the absolute
most. `cheapest` sets its own budget — one request per date it will visit, plus
a few spare for retries — so a 21-day sweep runs without raising anything by
hand.

Worked workflows — flexible-date hunts, watch cron jobs, emissions and layover
filters — are in [`google-flights/recipes.md`](google-flights/recipes.md). The
reverse-engineering behind it is in
[`google-flights/NOTES.md`](google-flights/NOTES.md).

## Checking a skill still works

These APIs are public but undocumented, so they can change without warning.
`campsite-search`, `enterprise-rentals`, `google-flights` and `google-maps`
ship self-checks; `cineplex-showtimes` is verified by any live call.

```sh
cd campsite-search/scripts
python3 test_availability.py              # 16 checks, 10 offline + 6 live
python3 test_availability.py --offline    # logic only, no network

cd enterprise-rentals/scripts
python3 test_availability.py              # 138 checks, 132 offline + 6 live
python3 test_availability.py --offline    # logic only, no network
python3 enterprise.py doctor              # environment + transport check

cd cineplex-showtimes/scripts
python3 cineplex_showtimes.py locations

cd kayak-browse/scripts
python3 test_kayak.py --offline           # 60 checks, no network and no key needed

cd google-maps/scripts
python3 test_gmaps.py --offline           # 77 checks, no network needed

cd google-flights/scripts
python3 test_flights.py --offline         # logic only, against a saved payload
python3 test_flights.py                   # adds a live group against www.google.com
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
