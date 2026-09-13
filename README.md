# travel-agent

> Eight Claude Skills for planning trips — what's playing tonight, whether a
> campsite opened up, what that flight actually costs right now, what the
> hotel costs once tax is in.

![Python 3](https://img.shields.io/badge/python-3-3776AB?logo=python&logoColor=white)
![No API keys](https://img.shields.io/badge/setup-none_(1_exception)-success)
![Read only](https://img.shields.io/badge/read--only-never_books-important)
![License](https://img.shields.io/badge/license-Apache_2.0-blue)

Each skill is a small Python CLI over a live public API, plus a `SKILL.md`
telling Claude how to drive it. **All read-only** — none of them books, holds
or pays for anything.

| Skill | Answers | Setup |
| --- | --- | :---: |
| [**campsite-search**](#campsite-search) | Campsite and cabin availability across nine Canadian park systems | — |
| [**cineplex-showtimes**](#cineplex-showtimes) | Cineplex showtimes, theatres, films and live seat availability | — |
| [**google-flights**](#google-flights) | Live fares and schedules, plus Google's own "is this a good price?" verdict | — |
| [**google-maps**](#google-maps) | What's open nearby, full opening hours, travel time by car/foot/bike/transit | — |
| [**google-hotels**](#google-hotels) | What a hotel costs for real nights — every seller, with and without tax, cancellation deadlines; relies on google-maps to find the hotels | — |
| [**enterprise-rentals**](#enterprise-rentals) | Enterprise rental prices worldwide — real make/model, mileage terms, trip totals | — |
| [**uber-eats**](#uber-eats) | Uber Eats menus, prices and sale prices near an address, every public deal, and where a dish is cheapest | — |
| [**kayak-browse**](#kayak-browse) | Cars, hotels and cheapest flight dates across hundreds of providers | 🔑 **key** |

**[Quick start](#quick-start)** · **[Sample prompts](#sample-prompts)** ·
**[Contributing](#contributing)** · **[Skill reference](#skill-reference)** ·
**[Self-checks](#self-checks)**

> [!IMPORTANT]
> **On claude.ai, turn on network access first.** The code sandbox blocks every
> domain by default, so every skill here fails until you do:
> **Settings → Capabilities → Code execution → network access for all domains**
> (or allowlist just the hosts you need — `www.google.com` covers google-maps,
> google-flights and google-hotels; `www.ubereats.com` covers uber-eats). Then **start a new chat**; the setting doesn't apply to the
> conversation you changed it in. Needs a paid plan with code execution. In
> Claude Code it works with no setup.

> [!WARNING]
> **`kayak-browse` needs a KAYAK affiliate API key** and has never been run
> against the live API. Without a key every command exits `4` and answers
> nothing. Skip it unless you have one — the other seven need no setup at all.

---

## Quick start

### Claude Code — just ask

Open it anywhere and paste:

> *Install the travel skills from https://github.com/harrisiva/travel-agent for
> me globally — clone it somewhere sensible, symlink each skill directory into
> `~/.claude/skills/`, and tell me what you installed. Skip kayak-browse.*

Global means every project and every conversation. Ask for one skill, or for a
single project's `.claude/skills/`, if you'd rather — and *"uninstall the travel
skills"* when you're done.

### claude.ai — two clicks

Uploading a skill is an account setting, so this is the one route Claude can't
do for you.

1. Download a `.skill` file from [**dist/**](dist/) *(on GitHub: open the file →
   **Download raw file**)*
2. **Settings → Capabilities → Skills** → upload it
3. Turn on network access (see above), then start a new chat

No Python, no clone, no keys.

### Then just ask

Skills fire on their own when a question matches — nothing to invoke, no command
to remember. See [sample prompts](#sample-prompts) for the shapes each one
handles.

<details>
<summary><b>As plain command-line tools</b> — none of this needs Claude</summary>

Every command takes `--json`.

```sh
cd campsite-search/scripts    && python3 -m campsites --help
cd cineplex-showtimes/scripts && python3 cineplex_showtimes.py --help
cd google-flights/scripts     && python3 flights.py --help
cd google-maps/scripts        && python3 gmaps.py --help
cd google-hotels/scripts      && python3 hotels.py --help
cd enterprise-rentals/scripts && python3 enterprise.py --help
cd uber-eats/scripts          && python3 ubereats.py --help
cd kayak-browse/scripts       && python3 kayak.py --help
```

</details>

<details>
<summary><b>Working on the skills themselves</b></summary>

`.claude/skills/` symlinks to the directories at the root, so a clone is picked
up with no install step and there is only ever one copy of each file.

```sh
git clone https://github.com/harrisiva/travel-agent.git
cd travel-agent
claude
```

</details>

<details>
<summary><b>Why these don't work in ChatGPT</b></summary>

Not just the format. These are thin clients over live HTTP APIs — they hold no
local data, so every useful command makes an outbound request. ChatGPT's Python
sandbox has no network access, so the scripts would fail on the first call even
pasted in directly. Porting them would mean rebuilding on Custom GPT Actions,
which do get network but can't run this Python.

</details>

---

## Sample prompts

Copy one, or ask in your own words.

<details>
<summary><b>🏕️ campsite-search</b></summary>

> *Anything at all open in Algonquin on a Friday or Saturday night in the next
> three weeks?*
>
> *I want an electrical site at Pinery for three nights next month — which ones
> are free, and are any of them private or barrier-free?*
>
> *Find me a cabin or yurt at Pinery, Killarney or Algonquin's Mew Lake for the
> next long weekend.*
>
> *When did booking open at Two Jack Lakeside in Banff, and how far ahead can I
> book right now?*
>
> *Site 412 at Sandbanks — show me its calendar for the next two weeks.*
>
> *Any paddle-in sites free at The Massasauga next weekend? A normal search
> says it's full.*
>
> *Bon Echo is sold out for the long weekend. Watch it and tell me if anything
> cancels.*
>
> *Any alerts or closures I should know about for Killarney right now?*

</details>

<details>
<summary><b>🎬 cineplex-showtimes</b></summary>

> *What's playing at the Waterloo Cineplex tonight?*
>
> *Is the 7pm IMAX showing of The Odyssey in Waterloo sold out?*
>
> *Which Cineplex theatres are showing The Odyssey in 70mm?*
>
> *I want two seats together in the middle of the theatre — which showing this
> Saturday still has them?*
>
> *Anything in Dolby Atmos in Mississauga on Friday after 6pm?*
>
> *Watch the 70mm IMAX Odyssey at Vaughan on Friday and tell me the moment two
> middle seats open up in row G or H.*
>
> *Tickets aren't out yet for the Friday IMAX. Check every morning and tell me
> when they are.*

</details>

<details>
<summary><b>✈️ google-flights</b></summary>

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
> *Before I pick a flight to Halifax next month — where might I connect, and
> what's the quickest and cheapest it gets on that day?*
>
> *Watch the Toronto–Halifax fare over Christmas and tell me if it drops under
> $600.*

</details>

<details>
<summary><b>📍 google-maps</b></summary>

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
> *Anywhere near Kensington Market still serving at 11pm on Saturday?*
>
> *What's the address and phone number for Richmond Station in Toronto, and
> what are its hours for the whole week?*
>
> *From the Drake Hotel, which is quickest by transit — the ROM, the CN Tower or
> Casa Loma?*

</details>

<details>
<summary><b>🛏️ google-hotels</b></summary>

> *What does the Fairmont Banff Springs cost for New Year's Eve and the night
> after, two of us — and is that with tax?*
>
> *Who's selling that cheapest, and can I still cancel it free the week
> before?*
>
> *Price me the hotels around Canmore for Oct 14–16, cheapest first — I'm
> working remotely so it has to have free wifi.*
>
> *Same three nights at the Samesun in Banff — is any check-in day in the first
> three weeks of October cheaper, and is mid-week better than the weekend?*
>
> *Of the Alpine Club, the Samesun and the Banff Y, which is cheapest for
> those nights with two adults and a six-year-old?*
>
> *Killarney's full for the long weekend. What would the hotels near the park
> cost for the same nights instead?*
>
> *Flight from Toronto, a small car, and three nights at a hotel near Halifax
> airport — am I anywhere near $800 all in, tax included?*
>
> *Watch the Samesun for Oct 10–12 and tell me when it goes under $60 a night
> including tax.*

</details>

<details>
<summary><b>🚗 enterprise-rentals</b></summary>

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
> *If my flight lands at 11pm on the 15th, can I still collect the car — and
> can I drop it back on Sunday night?*
>
> *The minivans at Halifax airport are sold out for Dec 24–28. Can you keep
> checking and tell me if one comes up for under $900?*

</details>

<details>
<summary><b>🍜 uber-eats</b></summary>

> *What's open near Union Station right now that can get here in under 30
> minutes?*
>
> *Show me Himalayan Kitchen's menu — anything under $15?*
>
> *What BOGO deals are there near me tonight, and on which dishes?*
>
> *Where's the cheapest butter chicken I can get delivered to 65 Front St W?*
>
> *Is Blondies Pizza on Uber Eats, and what does a large with bacon come to
> before fees?*
>
> *Any places with $0 delivery near me rated 4.5 or better?*
>
> *Tell me when Blondies Pizza opens.*
>
> *Albert's has 25% off select items — which dishes, and what do they cost
> now?*

</details>

<details>
<summary><b>🏨 kayak-browse</b> — needs an API key</summary>

> *What's the cheapest car I can pick up at JFK on Dec 20 and drop back on the 23rd?*
>
> *I need an SUV big enough to sleep in for a week out of Denver — automatic, unlimited mileage, free cancellation.*
>
> *Same car, same city — is it any cheaper if I shift the pickup a day either way?*
>
> *Pick up in Los Angeles, drop off in San Francisco. What does a one-way run me?*
>
> *The cheapest one hides the agency until you've paid — is that worth it, or should I take the Hertz one?*
>
> *When in March is it cheapest to fly from New York to Lisbon?*
>
> *Find me hotels in Austin for the nights of the 14th and 15th.*
>
> *What should I search for "Newark" — does KAYAK list a downtown pickup point as well as the airport?*

</details>

---

## Contributing

The directories at the repo root are the source of truth — edit those, re-run
that skill's [self-check](#self-checks), then rebuild the uploadable archives:

```sh
./build.sh          # regenerates dist/*.skill
```

> [!NOTE]
> Rebuild `dist/` **in the same commit** as the source change. It's what
> non-technical users download, and it's easy to leave behind.

[**CLAUDE.md**](CLAUDE.md) documents the conventions for adding a skill and the
patterns worth copying — CLI shape, exit codes, caching policy, what belongs in a
`SKILL.md`, and the traps these hit. Coding agents pick it up automatically
(`AGENTS.md` symlinks to it).

---

## Skill reference

### campsite-search

Availability across the **Camis5** platform, shared by nine Canadian park
systems: Parks Canada, Ontario Parks, BC Parks, Grand River CA, Manitoba, Nova
Scotia, New Brunswick, Newfoundland & Labrador, and Yukon.

| Command | What it answers |
| --- | --- |
| `search` | Is this park free on these exact dates? |
| `sweep` | Any opening at all across a date range? |
| `find` | Sweep *every* park matching a pattern — all of Algonquin at once |
| `site` | Day-by-day calendar for one named site |
| `stays` | What's bookable that isn't a tent pad — cabins, yurts, oTENTiks, huts |
| `window` / `horizon` | Operating season, when booking opens, how far ahead you can book |
| `attrs` / `equipment` | Filterable attributes (electric, pull-through, private, barrier-free) and booking category IDs |
| `alerts` | Park alerts and closures for a whole system — filter to one park by its id |
| `parks` / `providers` | Park lists per system; which systems are supported |
| `cache-clear` | Empty the on-disk reference cache |

<details>
<summary>Caveats and coverage</summary>

Availability, booking schedules and alerts are **never cached**, because a stale
answer is worse than none. Reference data (park lists, equipment, site metadata)
is cached, since it changes rarely.

Alberta, Saskatchewan, PEI, Québec and NWT are deliberately **not** supported —
they run different platforms, behind Queue-it waiting rooms or CAPTCHA. Run
`providers` for the current list and the reason for each.

Worked workflows are in [`campsite-search/recipes.md`](campsite-search/recipes.md).

</details>

---

### cineplex-showtimes

Showtimes, theatres, films and **live seat availability** from Cineplex Canada's
public theatrical and ticketing APIs.

| Command | What it answers |
| --- | --- |
| `showtimes` | Times for a theatre across one or more dates, filterable by film and experience (IMAX, 70mm, UltraAVX, Dolby Atmos, 3D, VIP, D-BOX, ScreenX, 4DX, …) |
| `seats` | Live seat map for one showtime — how many are left, which rows, whether the good middle seats are gone |
| `theatres` | Which theatres near a place are showing a given film |
| `movies` | Every film currently listed, with its ID |
| `locations` | Every theatre, with its ID |

For the sold-out question: `showtimes` gives a seat count per screening, and
`seats` breaks one screening down row by row.

<details>
<summary>Exit codes and caching</summary>

`0` found · `1` nothing (no showtimes, or no open seat matching the rows you
asked for) · `2` bad input or unknown ID · `3` network error. **A watch only
keeps waiting on `1`.**

Showtimes and seat maps are never cached; only the API key the site's own
frontend uses is. Read-only — it never books.

Recipes, including watching a sold-out screening on a schedule, are in
[`cineplex-showtimes/recipes.md`](cineplex-showtimes/recipes.md).

</details>

---

### google-flights

Live search against **Google Flights**, which server-renders its whole result
set into the page — so there's no JSON endpoint, no API key and no browser
needed. The client parses that payload directly.

| Command | What it answers |
| --- | --- |
| `airports` | What's the airport code for Toronto? Does London have several? |
| `search` | What flies this route on these dates? |
| `price-check` | Is this fare good, or is it worth waiting? |
| `cheapest` | Which departure date in a window is cheapest? |
| `route` | Fare and duration bounds for these dates, and where it can connect |
| `watch` | Has the fare dropped under my threshold yet? |

```sh
cd google-flights/scripts
python3 flights.py airports Toronto
python3 flights.py search YYZ YHZ --depart +15 --return +17
python3 flights.py price-check YYZ YHZ --depart +15 --return +17
python3 flights.py cheapest YYZ YHZ --depart +7 --return +9 --days 21
```

**`price-check` is the one worth knowing about.** Google publishes its own
verdict on a route's fare plus ~60 days of history, so it answers *"is $231
good?"* with **"typical — against a usual $228, in a $190–$345 range"**. No
listing scraper can do that.

<details>
<summary>Three things to keep straight</summary>

- Every price is a **bare fare**: Google publishes no baggage allowance, so a
  ticket with no carry-on and one with a checked bag included look identical.
  `search` returns `baggage_links` — the carriers' own policy pages for the
  route — which say where to check, never what a given fare includes.
- On a round trip the price is the **whole trip** but the legs shown are the
  **outbound only** — every itinerary says which, via `price_covers`.
- Prices cover the **whole party**, so `--adults 2` roughly doubles them.
- On `cheapest`, `--return` sets the trip *length* rather than a fixed return
  date, so the sweep slides both ends together — each row reports the return it
  actually priced.

Dates are `YYYY-MM-DD` or `+N` days from today. Only `--max-stops` is applied by
Google server-side; `--max-price`, `--airlines`, `--avoid-layovers`,
`--depart-after`, `--max-co2` and friends filter the results at no extra cost.

</details>

<details>
<summary>Exit codes and request budget</summary>

Fares are never cached — a stale "cheap" answer is the one that costs money.

`1` the query worked and nothing matched · `2` the question as asked can't be
answered, and it says why rather than returning an empty list (a past date, an
airline that doesn't fly the route, a `--max-price` below a single search's own
cheapest fare, a metro code that already contains the destination, a currency
Google didn't price in) · `3` the check itself failed, including a captcha.

**Only `1` is safe to keep polling on** — and a `watch --under` below today's
fare is the normal case, so it exits `1` until the fare falls, never `2`.

Fan-out is capped: 5 requests per command, except `cheapest`, which budgets one
per date it visits (`ceil(days/step)`, max 40) plus 3 spare for retries.
`--max-requests` overrides either, up to a hard 40.

Recipes in [`google-flights/recipes.md`](google-flights/recipes.md); the
reverse-engineering in [`google-flights/NOTES.md`](google-flights/NOTES.md).

</details>

---

### google-maps

Places near a location with **live** open/closed status, the full seven-day
schedule, and travel time by car, foot, bike or transit — including live
traffic. No API key, no login, and **no dependencies at all** — standard library
only.

| Command | What it answers |
| --- | --- |
| `nearby` | What's open around here, ranked by how long it takes to get there |
| `search` | Places matching a query, with hours, rating and address (phone is in `--json`) |
| `hours` | One place's full week, plus its address and phone number |
| `travel` | Time and distance to one or more places, in one request |
| `geocode` | A place name or address to coordinates (for chaining — no address or phone) |

**`--mode` matters most.** `walk`, `bike`, `transit` and `drive` give genuinely
different answers: a place 600 m away is "8 minutes" by car and a 7-minute walk,
and in a city the walk is usually the real answer. Transit also gives departure
and arrival clock times for the next journey leaving now, for the nearest few
results.

`--open-at "Fri 21:00"` is read in the *place's* local time — what you mean when
you ask whether somewhere is still open when you land.

<details>
<summary>Known gaps</summary>

Places that publish no hours are reported as *unknown*, never as closed. Travel
times say whether they include live traffic.

- No **search-along-a-route** command — "where can we stop on the way?" is
  answered by interpolating waypoints and searching around each (recipe 5,
  tested, with caveats).
- No automatic radius widening when nothing is open; retry at a larger `--span`
  (default 10000 m, then 25000, then 50000).
- **No price data** exists on these endpoints, so the skill says so rather than
  guessing. Reviews, photos, busyness, accessibility and reservation links
  aren't surfaced.

These are Google's internal endpoints, not a supported API, so they can change
without notice; a shape change exits `3`, never "nothing found".

Recipes in [`google-maps/recipes.md`](google-maps/recipes.md).

</details>

---

### google-hotels

Live prices from **Google Hotels'** own per-hotel page, which server-renders
every seller's rate for the dates you ask — with and without tax, the stay
total broken into base, taxes and fees, and each seller's free-cancellation
deadline. No API key, no browser.

| Command | What it answers |
| --- | --- |
| `quote` | What does this hotel cost for these nights, from whom, and is that with tax? |
| `shortlist` | Which of these hotels — the ones google-maps found near a place — is cheapest for these nights? Filterable by price, rating, stars, free cancellation and amenity |
| `cheapest` | Which check-in day in a window is cheapest for an N-night stay? `--step 7` compares weekends |
| `watch` | Has this hotel dropped under my threshold yet? (one check; cron owns the loop) |
| `resolve` | Every id form for a hotel — ftid, place_id, CID, entity token — offline |
| `doctor` | Why did that fail — environment and transport check |

```sh
cd google-maps/scripts
python3 gmaps.py search --near Canmore --query hotels --full --json > /tmp/canmore.json
cd ../../google-hotels/scripts
python3 hotels.py shortlist --ids-from /tmp/canmore.json --checkin +30 --checkout +32
python3 hotels.py quote 0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce --checkin 2026-12-31 --checkout 2027-01-02
python3 hotels.py cheapest 0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce --checkin +1 --nights 2 --days 21 --step 7
```

**Every seller, both tax bases, on one page.** Google's headline rate is
often not the cheapest — the hotel's own site undercut it in several
captures — and every rate carries the before-tax and after-tax figure side by
side, so *"is that with tax?"* is answered from the page rather than guessed.

<details>
<summary>What it depends on, and what it deliberately does not do</summary>

- **It prices hotels by id; google-maps finds them.** Google's hotel *list*
  is on a path this skill does not use, so there is no "search Banff" here.
  `gmaps.py search --near <place> --query hotels --full --json` produces the
  candidate file (`--full` is what carries the ids), and `shortlist` prices
  it. A bare hotel name is refused with the exact google-maps command to run —
  it never guesses which building you meant. Without google-maps installed it
  still prices any id off a Google Maps link, or a pasted Google Hotels link.
- **`shortlist` is not a market search.** It reports "cheapest of the 6
  checked near Canmore", never "cheapest in Canmore" — a budget motel Maps
  ranks 30th is invisible unless google-maps was asked for more.
- **No "is this a good price?" verdict.** Google publishes one for flights;
  for hotels it exists only on the list page. The skill offers facts instead —
  a cheaper seller, a cheaper check-in day, the rank among those checked.
- **Vacation rentals** have no Maps id, so they are reachable only from a
  pasted Google Hotels link (`quote --token`).
- **Phase 2** (`search` and `trend` over the list page — twenty priced hotels
  per page and Google's typical/low/high band) is designed but not shipped;
  it lands only once a live check proves the list honours requested dates.

</details>

<details>
<summary>Exit codes, what it refuses, and the request budget</summary>

Prices are never cached — the lead rate moved $1,283 → $1,198 → $1,053
between fetches minutes apart. Nothing is written to disk.

`1` the query worked and nothing: no rates listed for these nights (never
"sold out" — the phrase exists nowhere in the data) · `2` refused, and it says
why: a past or inverted stay, more than 30 nights or 330 days out, a party out
of bounds, a bare name, an unknown id, a currency Google didn't price in ·
`3` the check failed, including **a page that priced a different stay than
asked** — Google answers a bad stay with a real price for a different one,
so every response is checked against the page's own echo of the dates and
party before it is reported.

**Only `1` is safe to keep polling on.** A `watch --under` below today's
price is the normal case and exits `1` until the price falls.

Fan-out is capped: one page per hotel (1.6–4.3 MB each, 2.5 s apart);
`shortlist` prices at most 10, `cheapest` at most 21 dates; hard cap 40
requests. A plan over the ceiling is refused before the first request.

Recipes in [`google-hotels/recipes.md`](google-hotels/recipes.md); the
reverse-engineering in [`google-hotels/NOTES.md`](google-hotels/NOTES.md).

</details>

---

### enterprise-rentals

Live rental prices from Enterprise, anywhere it operates. Verified live in
**nine countries on four continents** — Canada, US, UK, Germany, France, Spain,
Ireland, New Zealand and Japan — each priced in the branch's own currency.

One request returns the **whole fleet** for a branch and date pair: real make and
model, seats, luggage, drivetrain, fuel type, transmission, mileage terms and the
trip total, all at once.

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

<details>
<summary>What this skill refuses to guess about</summary>

**`PER DAY` is the trip total ÷ rental days**, not Enterprise's own rate line,
and the footer says so. Some classes are quoted weekly, which read as a daily
price would understate a week several times over; `--json` keeps the raw figure
(`api_rate`, `api_rate_period`) and the derived one (`per_day_effective`) apart.

**Prices are what the branch charges.** `--currency` adds a labelled estimate
(`436.00 CAD (~315.10 USD est.)`) but never replaces the billed figure.

**Age is not cosmetic.** Under-25 renters pay more *and* can't rent every class —
at Halifax, 20 bookable classes at 25 becomes 13 at 21. Pass `--age 21 --age 25`
to price both and see what disappears. The API reports age-restricted classes as
sold out, so for an under-25 renter the two are indistinguishable — the skill
says so rather than sending you hunting for better dates.

**Sold-out classes stay visible in the count**, never silently dropped:
`20 of 59 classes bookable`, with a per-category breakdown, because "every
minivan is gone" is sometimes the answer.

**Commercial terms aren't in this API** — deposit, insurance, fuel policy,
cancellation, additional-driver fees. It says it can't tell you rather than
guessing.

**Branch names are ambiguous.** `Halifax` matches the airport, the train station
and an **Exotic** branch at the same airport with a different fleet at much
higher prices. It refuses to guess and lists the candidates. `locations` also
shows `city` rows — place names like `Halifax, GB` — which aren't branches and
can't be quoted.

**`--country` is a search hint, not a filter** — asking for Australia returns
Sydney, *Nova Scotia*; `--country BO` returns La Paz, *Mexico*. Every listing
carries a `CTRY` column, and a branch outside the country you asked for triggers
a loud warning before it's priced.

**Cross-border one-ways** are refused by Enterprise with the *same* message as a
genuine sell-out. The skill exits with a usage error and says the route probably
isn't permitted, rather than sending you hunting for dates that will never work.
A one-way whose destination country can't be confirmed is treated the same way.

Recipes in [`enterprise-rentals/recipes.md`](enterprise-rentals/recipes.md).

</details>

---

### uber-eats

Menus, prices and deals from **Uber Eats'** own web JSON API — the calls the
site makes when you set a delivery address and open a restaurant. No account,
no key, no browser. Everything is relative to a delivery address, and the
answer says which one.

| Command | What it answers |
| --- | --- |
| `nearby` | What can I get here — rating, ETA, distance, deal badges — filterable by rating, ETA, distance and deal type |
| `find` | Is this restaurant on Uber Eats at my address, and what is its id? |
| `menu` | What's on the menu and what does it cost — sale price *and* original, BOGO markers, sold-out state, today's hours |
| `item` | One dish's option groups and add-on prices, and the "from" price with the required picks |
| `deals` | Every public deal nearby, typed — BOGO, % off, $ off with minimum spend, $0 delivery — and with `--items`, the dishes each applies to |
| `compare` | Where is this dish cheapest across the nearest N menus? |
| `watch` | Has it opened / gone on sale / dropped under $X / come back in stock yet? (one check; cron owns the loop) |
| `locate` | Which address will Uber deliver to, and a token to pin it |
| `doctor` | Why did that fail — is Uber's bot protection blocking us? |

```sh
cd uber-eats/scripts
python3 ubereats.py nearby --at "65 Front St W, Toronto" --max-eta 30 --min-rating 4.5
python3 ubereats.py menu "Himalayan Kitchen" --at "65 Front St W, Toronto" --under 15
python3 ubereats.py deals --at "65 Front St W, Toronto" --type bogo
python3 ubereats.py compare "butter chicken" --at "65 Front St W, Toronto"
```

**The sale price is the price, and it says what it was.** Uber's payload
carries the discounted figure as the price and hides the original inside a
struck-through span of HTML, so a naive reader either misses the saving or
gets it backwards. Every dish reports `price` and `was` separately, and a
BOGO is ranked at full price with "second one free" — never at half.

<details>
<summary>What it depends on, and what it deliberately does not do</summary>

- **`nearby`, `find` and `deals` are not a market search.** They see the
  page of stores Uber's feed returned for that address — about 100–150, and
  both the membership and the order of that set shift between calls minutes
  apart. Every header reads "N of the M stores Uber returned near X — Uber's
  list, not the whole market", and a `find` miss is never "it isn't on Uber
  Eats" — paste the restaurant's Uber Eats link and
  `menu` takes it directly.
- **No keyword or cuisine search.** Uber's search endpoint is behind its bot
  protection and the feed ignores a query, so "Thai near me" is `nearby` and
  pick by name, or `compare "pad thai"` over the nearest menus.
- **No fees, no totals.** Delivery and service fees exist only once there is
  a cart; the anonymous API returns null for both. The skill never estimates
  a total and says fees are added at checkout.
- **Public deals only.** Uber One pricing and account offers are invisible
  logged out, and every deals answer says so.
- **Canada only, verified.** Other Uber Eats countries are attempted via
  `--locale` and labelled unverified.
- **Read-only by construction.** The transport refuses any endpoint outside a
  five-name allowlist before opening a socket; there is no cart, order or
  favourite path to reach.

</details>

<details>
<summary>Exit codes, what it refuses, and the request budget</summary>

Prices, deals, menus and open/closed state are never cached; only a resolved
address is (30 days).

`1` the query worked and nothing matched — filters excluded everything, the
restaurant wasn't among the stores returned, no menu listed the dish; for
`watch`, not yet · `2` refused, and it says why: an address Uber doesn't
serve, an unknown store id, a name matching zero or several stores, a dish
that isn't on the menu, a plan over `--max-requests` · `3` the check failed,
including **Uber's bot protection serving a challenge instead of JSON** —
Cloudflare on the pages, and Uber's own reCAPTCHA defense on the API after
roughly 110 requests from one IP in a day, which then lasts hours — reported
as a block, never as "nothing nearby". Keep runs small; there is no
workaround and none will be added.

**Only `1` is safe to keep polling on.** A `watch --under` below today's
price is the normal case and exits `1` until it drops.

Fan-out is capped: `compare` reads 1 feed + `--stores` menus (default 10,
max 24); `deals --items` one menu per store (default 10 stores); hard cap 25
requests per command, 1 s apart. Each command's own plan plus two spare for
retries is its ceiling and `--max-requests` can only lower it; a plan over
the cap is refused before the first request. A Cloudflare challenge anywhere in a run is exit `3` for the
whole command.

Recipes in [`uber-eats/recipes.md`](uber-eats/recipes.md); the
reverse-engineering in [`uber-eats/NOTES.md`](uber-eats/NOTES.md).

</details>

---

### kayak-browse

> [!WARNING]
> **Needs a KAYAK affiliate API key, and has never run against the live API.**
> Without a key every command exits `4`. Prefer `enterprise-rentals` or
> `google-flights` for a plain rental quote or flight price — both work with no
> key. Reach for this one when you need KAYAK's cross-provider breadth, or hotels.

Cars, hotels and cheapest-flight-dates across hundreds of providers in one
search. This exists because the direct supplier sites are closed: avis.ca and
budget.ca both answer `403` behind DataDome on their pricing endpoint, from
`curl` and a real browser alike.

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
plus the usual class, seat, bag, transmission and fuel constraints. When a filter
empties the result, it reports *which* one did it.

<details>
<summary>Exit codes</summary>

Identical under `--json`. **`1` is the only one that means "keep waiting".**

| Code | Meaning |
| --- | --- |
| `0` | found something, and the search finished |
| `1` | the search finished and nothing matched |
| `2` | usage, lookup or budget error |
| `3` | network or API error |
| `4` | API key missing, rejected or expired — never "nothing available" |
| `5` | the search had not finished — results are partial, and may be empty |

</details>

<details>
<summary>Sandbox prices are fake, and why this is unverified</summary>

**Sandbox prices are mock data.** `cars`, `hotels` and `when` all refuse to print
a price column (and `sweep` refuses to rank at all) against the sandbox host
unless you pass `--sandbox-ok`, and every row is marked `priceIsReal: false`. A
fabricated price presented as a real quote is the worst thing this skill could
do, so the guard is structural rather than a warning in the docs. Sandbox keys
also expire every three months; an expired key exits `4`.

**Not yet verified against the live API.** No key existed when this was built, so
every test runs against fixtures written from KAYAK's published RAML spec. That's
a real limitation, not a formality: the worst bug found during development was a
misspelled request field that silently disabled two critical settings — and the
test written to catch it asserted the same misspelling. Re-recording the fixtures
against a production key is the first job once access exists. Three things stay
open until then: whether sandbox signup is self-serve or needs business approval,
what production rate limits look like, and whether the cars API exposes Canadian
inventory usefully (the sandbox is US-only).

Recipes in [`kayak-browse/recipes.md`](kayak-browse/recipes.md).

</details>

---

## Self-checks

These APIs are public but undocumented, so they can change without warning.
Every skill ships a self-check; a `[network]` failure names the host, so a tenant
being down is easy to tell apart from the skill being broken.

| Skill | Command *(from its `scripts/`)* | Checks |
| --- | --- | --- |
| campsite-search | `python3 test_availability.py` | 24 — 18 offline + 6 live |
| cineplex-showtimes | `python3 test_cineplex.py` | 18 — 14 offline + 4 live |
| enterprise-rentals | `python3 test_availability.py` | 163 — 157 offline + 6 live |
| google-flights | `python3 test_flights.py` | 148 offline + a live group |
| google-maps | `python3 test_gmaps.py` | 97 offline + a live group |
| google-hotels | `python3 test_hotels.py` | 531 offline + a live group |
| uber-eats | `python3 test_ubereats.py` | 574 offline + a live group |
| kayak-browse | `python3 test_kayak.py --offline` | 76 — no network, no key needed |

Add `--offline` to any of them to skip the network group.
`python3 enterprise.py doctor` also reports the environment and transport in use.

## License

[Apache 2.0](LICENSE)
