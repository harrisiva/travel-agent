# Google Hotels — API research notes

Background for the shipped skill. This file records what the data surface
looks like and how each field's meaning was established, so the
reverse-engineering does not have to be redone when Google changes something.
It is not the interface contract — that is `SKILL.md`.

Same architecture as `google-flights/NOTES.md`: server-rendered
`AF_initDataCallback({key:'ds:N', data:[...]})` blocks, parsed with a regex
plus `json.loads`, no key, no cookies beyond `CONSENT=YES+cb`, no browser.

Everything below was verified with live requests on **2026-09-12** from a
Canadian address with `hl=en&gl=CA`: 44 HTTP requests in total, all ≥ 2.5 s
apart with a Chrome User-Agent. No captcha, no 429, no consent wall was ever
served. Every claim cites a capture and a JSON path into it. Capture names
(`p8_fairmont_ts.ds1.json`, `fixture_samesun_cid.html`, …) are the probe's
`evidence/` files; the full pages among them are what `scripts/fixtures/`
holds, trimmed and gzipped, under the same names. `requests.log` numbers
(`#8`) are the request sequence in that log; `echo-summary.json` is the
per-request digest (query echo, lead price, OTA rows) for captures too large
to keep whole.

## The finding that shapes everything: the list page is a redirect into a disallowed path

| Path | robots.txt | What it actually returns |
|---|---|---|
| `/travel/hotels/<Place>?checkin=&checkout=` | allowed | **HTTP 302 → `/travel/search?q=<Place>&qs=OAA&…`** (`requests.log` #3) |
| `/travel/hotels?q=<Place>` | allowed | HTTP 302 → `/travel/search?…` (#4) |
| `/travel/hotels` (root, no query) | allowed | HTTP 302 → `/travel/search?qs=OAA&…` (#24) |
| `/travel/search` | **Disallow** | the hotel list — the only place the list exists |
| `/travel/hotels/entity/<token>` | allowed (only `/travel/hotels/*/stories` is disallowed) | **HTTP 200, one hotel's full record with dated, per-OTA prices** (#5–#32) |
| `/travel/hotels/entity/<token>/prices` | allowed | same page, same payload shape (#9) |
| `/travel/clk/hi?…`, `/travel/lodging/clk?…`, `/aclk?…` | `/travel/clk` and `/travel/lodging/clk` **Disallow**; `/aclk` is an ads redirect | OTA click-out redirects; never fetch |
| `/travel/entity`, `/hotels/rpc`, `/hotelfinder/rpc` | **Disallow** | not probed |

The two list captures (`banff_oct.ds0.json`, `banff_ny.ds0.json`) exist
because the probe helper followed the 302 on its first two calls before this
was noticed; the redirect was never followed again. **Every dated-price claim
below comes from the allowed entity path.** The shipped client treats any 3xx
as exit 3 and never follows it.

So phase 1 is not "search Banff, get 20 priced hotels". It is: resolve hotel
names to Google Maps feature ids in google-maps (`--full` emits `ftid` and
`place_id`), then fetch `/travel/hotels/entity/<token>?ts=<protobuf>` per
hotel. The list-only data — the "typical price" widget, result counts, the
sponsored block, pagination — is documented at the end for the phase-2
`search`/`trend` extension (`ghotels/listing.py`, the only module allowed to
touch `/travel/search`).

## Links

| What | URL |
|---|---|
| Entity page | `https://www.google.com/travel/hotels/entity/<token>?hl=en&gl=CA&ts=<protobuf>` |
| Worked example: Fairmont Banff Springs, 2026-12-31 → 2027-01-02, 2 adults, CAD | `https://www.google.com/travel/hotels/entity/ChQIzu3T55K1-kwaCS9tLzA1MnR2chAB?hl=en&gl=CA&ts=CAESCgoCCAMKAggDEAAaGBIWEhIKBwjqDxAMGB8SBwjrDxABGAIYASoHCgU6A0NBRDICCAE` |
| Same hotel via a token built from its Maps CID alone | `https://www.google.com/travel/hotels/entity/CgkIzu3T55K1-kwQAQ?hl=en&gl=CA&ts=…` (#10) |
| Vacation rental example (Ascent Amour, Canmore) | `https://www.google.com/travel/hotels/entity/ChkQp5T1uba_qKpQGg0vZy8xMXo5cmh6MG03EAI?hl=en&gl=CA&ts=…` (#23) |
| robots.txt | `https://www.google.com/robots.txt` → `google-robots.txt` |

## How to parse

```python
re.compile(r"AF_initDataCallback\((\{key:\s*'(ds:\d+)'.*?)\);</script>", re.S)
# then re.search(r"data:(.*?), sideChannel", block, re.S) -> json.loads
```

Identical to `gflights/parse.py::blocks`. The same regex found every block on
every page. On the **entity** page `ds:0` is the literal `[]` and the record is
in `ds:1`; on the (disallowed) list page `ds:0` is the 235 KB results block.

| Page | Key | Size | Contents |
|---|---|---|---|
| entity | `ds:0` | 2 B | `[]` |
| entity | `ds:1` | 60 KB (rental) – 900 KB (Fairmont, 27 OTAs) | `[record, null, [[widget ids]]]`; the hotel is `ds:1[0]` |
| entity | `ds:2` | 2150 B | static currency catalogue (72 currencies + 7 "popular") — the healthy-page marker; `CURRENCIES` is generated from `p8_fairmont_ts.ds2.json` |
| list | `ds:0` | ~237 KB | widget tree: hotel cards, ads, filters, price trend, pagination |
| list | `ds:1` | 2150 B | the same currency catalogue, byte-identical |

Entity pages are 1.65 MB (unknown entity) to 4.3 MB; that weight, not the
request count, is why `shortlist` caps at ten and `cheapest` at 21 dates.

## Tokens: what `<token>` is, and how to make one without the list page

`token` is base64url protobuf. Decoded for all 20 list cards
(`banff_oct.ds0.json`, each card's `e[20]`):

```
{1: {1: <uint64 hotel id>, 3: "/g/… or /m/… KG id"}, 2: 1}   # hotel
{1: {2: <uint64 rental id>, 3: "/g/… KG id"},           2: 2}   # vacation rental
```

Field 2 is the kind (1 hotel, 2 vacation rental). For hotels, **field 1.1 is
the Google Maps CID** — the second half of the `ftid`:

- Fairmont `e[9]` = `0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce`; `0x99e9a92cf4f6ce`
  = 43322584249726670 = token field 1.1 exactly.
- Samesun `e[9]` = `…:0x573e93dcf38f6714`; a token built from only that CID
  (`CgoIlM69nM_7pJ9XEAE`) returned the Samesun Banff page with dated prices
  (#29, `p29_samesun_ny.ds1.json`).
- Fairmont from CID alone (`CgkIzu3T55K1-kwQAQ`): #10, full page, prices.
- The entity page's own `e[26][4]` is the Maps `place_id`
  (`ChIJv7gvLjvKcFMRzvb0LKnpmQA`), which decodes to
  `{1: {1: fixed64 0x5370ca3b2e2fb8bf, 2: fixed64 0x99e9a92cf4f6ce}}` — the two
  ftid halves, little-endian. So `place_id`, `ftid` and the CID are all
  interconvertible offline (`ghotels/ids.py`), and google-maps `--full` emits
  the first two.

What does **not** work: a token with only the KG id (`{1:{3:"/m/052tvr"},2:1}`,
#7) or a bogus CID (`{1:{1:12345},2:1}`, #18, #35) — see *The unknown-entity
signature* below. Vacation rentals carry no CID (their `e[9]` is null); their
token needs the rental id in field 1.2, which only the list page and a pasted
link supply. A Maps-resolved pipeline therefore reaches **hotels only**;
rentals need `quote --token`.

## `ts`: the query protobuf (dates, occupancy, currency)

`checkin=`/`checkout=` URL parameters are **ignored on every path** — the
redirect preserves them (#3) and the entity page (#6) still priced the default
stay. Tested as bare URL params on the entity path (#33, `echo-summary.json`
→ `p33_urlparams`): `adults=1&children=1&rooms=2&curr=USD&checkin=…&
checkout=…` → echo `[[2026,10,13],[2026,10,14],1]`, occupancy `null`,
currency `CAD`. **All six are ignored.** `hl` and `gl` are honoured.

The default stay is *today + 31 days, one night, two adults* (`e[6][1][4]` =
`[[2026,10,13],[2026,10,14],1,null,0]` on 2026-09-12; the sponsored-ad block
spells the 31 out at `ad[19][3]`).

Dates go in `ts`. Built by hand and confirmed on the first try (#8); every
field was then varied one at a time and read back from the query echo
`ds:1[0][6][1]`:

```
1: 1                                   constant
2: { 1: {1: 3}  (repeated, one per adult)
     1: {1: 2, 2: <age>}  (one per child)
     2: 0 }
3: { 2: { 2: { 1: {1:y, 2:m, 3:d}     check-in
               2: {1:y, 2:m, 3:d} }   check-out
          3: 1 } }
5: { 1: { 7: "CAD" } }                 currency, ISO code
6: { 1: 1 }                            constant
```

| Change | Echo `ds:1[0][6][1]` | Effect on Fairmont price | Capture |
|---|---|---|---|
| dates 2026-12-31 → 2027-01-02 | `[4]` = `[[2026,12,31],[2027,1,2],2,null,0]` | $1,023 → $1,283 / night (New Year) | #8 `p8_fairmont_ts.ds1.json` |
| one adult (`{1:3}` once) | `[13]` = `[1, null, 0]` | unchanged | #11 `p11_1adult.ds1.json` |
| two adults + child age 5 | `[13]` = `[2, [[5]], 0]` | $7,139 / night, only 2 OTAs can fit the party | #12 `p12_2ad_child5.ds1.json` |
| two adults + children 5, 10 | `[13]` = `[2, [[5],[10]], 0]` | $7,214 / night, 3 OTAs | #32 (echo-summary) |
| currency USD | `[3]` = `"USD"`, strings `US$926` | FX only | #13 `p13_usd.ds1.json` |
| `gl=US`, currency CAD | same rows, same floats; strings become `CA$1,198` instead of `$1,198` | none | #31 (echo-summary) |
| `hl=fr` | labels localised (`'hôtel cinq\xa0étoiles'`, `'1\xa0198\xa0$'`); floats and layout identical | none | #30 (echo-summary) |

Consequences: read the floats, never the strings — under `gl=CA` a Canadian
dollar renders as a bare `$`, so a string-based currency check is wrong in the
home market. Pin `hl=en`. No rooms field was found in `ts` (every test was one
room); `qs=OAA` on the redirect decodes to `{7: 0}` and is not needed.

The encoder (`ghotels/ts.py`) is asserted byte-for-byte against the URLs in
`requests.log`.

### The echo is the oracle — and it catches four silent fallbacks

Each of these returned **HTTP 200 with a real price** — for the default stay,
not the one asked for. The only tell is that the echo dates differ from the
request:

| Request | Echo dates | Lead price returned | Capture |
|---|---|---|---|
| past: 2026-09-01 → 09-03 | `[2026,10,13]→[2026,10,14]` | $1,053 | #14 |
| checkout = checkin (2026-12-31 → 12-31) | default | $1,053 | #15 |
| 31 nights (2026-12-01 → 2027-01-01) | default | $1,053 | #25 |
| 35 nights | default | $1,023 | #16 |
| +347 days, +365 days (2027-09-10) | default | $1,023 | #34, #22 |

And these were honoured: 14 nights (#20), **30 nights** (#19, one OTA left),
+110, +180 (#21), +270 (#28), +300 (#27), **+330 d** (#26). So the night cap
is 30 (matches the UI), and the horizon is 330 ≤ h < 347 days; `MAX_OFFSET_DAYS
= 330`, the same figure google-flights measured. The exact day was not
binary-searched.

The client validates all of these up front *and* compares `ds:1[0][6][1][4]`
(dates) and `[13]` (occupancy) against what it sent, exactly as
`gflights/client.py::_check_echo` does. Without that, "how much is New Year at
the Fairmont" is answered with a mid-October rate and nothing looks wrong.
Because the client always sends `ts`, a `null` occupancy echo is a mismatch;
the "null means default 2 adults" reading applies only to pages fetched
without `ts` (#33), which the skill never does.

### The genuine empty result

+270 days (2027-06-09 → 06-11, #28, `p28_plus270d.ds1.json`; full page
`fixture_plus270.html`, #38): echo dates **match the request**, `p[1]` (lead
price) is `null`, `p[21]` is null, `p[44]` is `null`. That is "no rates loaded
for these dates" — exit 1 territory — and it is distinguishable from the
fallback above only because the echo is checked first. But see *the union
rule*: on that same page `p[2]` still carried three priced offers.

## The hotel record (`ds:1[0]` on the entity page; `card[0]` on the list page)

Both pages use the same 27/46-slot positional record; the list card is a
truncated copy (slots ≥ 27 absent, `e[19]` null for hotels, amenity names
absent). `e` below is that record. Confirmed on Fairmont (5-star hotel,
`p8_fairmont_ts.ds1.json`), Samesun (3-star hostel, `p29_samesun_ny.ds1.json`),
Ascent Amour (vacation rental, `p23_vr_ny.ds1.json`, `p5_entity.ds1.json`) and
all 20 list cards.

| Path | Field | Example (Fairmont, #8) |
|---|---|---|
| `e[1]` | name | `"Fairmont Banff Springs"` |
| `e[2][0]` | `[lat, lng]` | `[51.164332, -115.56183]` |
| `e[2][1][0][0][0]` | street address (entity page only) | `"405 Spray Ave, Banff, AB T1L 1J4"` |
| `e[2][2]` | `[phone display, tel: URI]` (entity only) | `["(403) 762-2211", "tel:+14037622211"]` |
| `e[2][17]` | `[check-in time, check-out time]` | `["4:00 PM", "12:00 PM"]` (U+202F before AM/PM) |
| `e[2][21]` | containing regions `[[kg id, name, ftid], …]` | Alberta, Improvement District No. 9, Banff |
| `e[2][29][2]` | official website | fairmont.com URL |
| `e[2][31][0]` | Maps categories `[[gcid, is_primary], …]` | `gcid:resort_hotel` = 1 |
| `e[2][34]` | viewport `[[lat,lng],[lat,lng]]` | |
| `e[2][36]` | country code | `"CA"` |
| `e[3]` | `[star-class label, int]`; **null for vacation rentals** | `["5-star hotel", 5]`; Samesun `["3-star hotel", 3]` |
| `e[5][1][k][1][0]` | photo URLs | `lh3.googleusercontent.com/…` |
| `e[6]` | **prices** — see next section | |
| `e[7][0]` | `[rating, review count]` | `[4.7, 17556]` |
| `e[7][1][0]` | star histogram `[[stars, percent, count], …]` | `[[5,83,14329],[4,12,2240],…]` |
| `e[7][3]` | highlighted review snippets (entity) | |
| `e[7][4]` | per-source review summaries `[source, logo, [rating, scale], count, [reviews…]]` (entity) | `["all.accor.com", …, [4.6, 5], 5956, …]` |
| `e[7][9]` | review topics | |
| `e[9]` | **Maps ftid** `0x…:0x…`; null for rentals | `0x5370ca3b2e2fb8bf:0x99e9a92cf4f6ce` |
| `e[10]` | amenities — see *Amenities* | |
| `e[11]` | description: `[one-liner, [paragraphs…]]` on entity | `"Upscale lodging in a castle-like building…"` |
| `e[12]` | thumbnail `[url, h, w]` | |
| `e[14]` | **kind: 1 = hotel, 2 = vacation rental** (agrees with token field 2 and `e[26][0]` on all 20 cards) | `1` |
| `e[19][1]` | rentals only: `[null, sleeps, bedrooms, bathrooms, beds]` | Ascent Amour `[null, 4, 1, 1, 2]` |
| `e[19][8]` | rentals only: listing sites `[name, logo, ?, partner id, deep link, …]` | Expedia.ca, Vrbo.com, Canmore Rental Management |
| `e[20]` | entity token | |
| `e[25]` | Google's own id (string); equals token field 1.2 for rentals, differs from the CID for hotels | |
| `e[26]` | `[kind, …, place_id]` on entity | `[1, null, null, null, "ChIJv7gvLjvKcFMRzvb0LKnpmQA"]` |
| `e[38]`, `e[45]` | photo categories; gallery with attribution | |

Not found anywhere: **distance to centre / neighbourhood label**. Distance is
computable from `e[2][0]` and the gmaps file's `from` centre, not supplied.

## The price block `e[6]`

```
e[6][0]  null
e[6][1]  query echo:  [null, null, null, currency, [checkin, checkout, nights, null, 0], …, [13] = [adults, [[age],…] | null, 0]]
e[6][2]  prices — call it p
```

| Path | Meaning | Fairmont 2 nights (#8) |
|---|---|---|
| `p[1]` | **lead nightly rate** `[ex-tax str, incl-tax str, ex-tax float, null, ex-tax rounded]` — `[3]` is null in every capture | `['$1,283', '$1,452', 1282.75, null, 1283]` |
| `p[8]` | the dates this price is for — same as the echo | `[[2026,12,31],[2027,1,2],2,null,0]` |
| `p[9]` | **stay total** at the lead rate `[ex-tax, incl-tax]` strings | `['$2,566', '$2,905']` |
| `p[44]` | stay total breakdown **`[base, taxes, fees, total]`** floats; `total` = `p[9][1]` | `[2435.625, 339.25, 129.875, 2904.75]` |
| `p[15]` | currency actually priced in (may be null on a page with no headline; then `[6][1][3]` decides) | `"CAD"` |
| `p[32]` | days from today to check-in | `110` |
| `p[2]` | offers **with room lists**: `o[0][0]` seller, `o[7]` = `[[room name, …], …]`, `o[9]` room count, `o[12]` prices as below | 4 sellers; Fairmont direct lists 11 room types (`"DELUXE King - 350sf…"`) |
| `p[12]` | "featured" rows, same row shape | 2 |
| `p[21]` | OTA rows (see below) | 11 rows here; up to 27 on the default date |
| `p[22]` | 1–3 headline rows (official site + others) | `[Fairmont direct 1292.56, dealbase 1282.75]` |
| `p[10]`, `p[17]`, `p[25]`, `p[26]` | nightly / stay-total strings of *other* rows — which rows, not established | |
| `p[14]` | currency catalogue (72 + 7), not price history | |
| `p[4]` | `/travel/clk/hi?…` click-out URLs (disallowed path, never fetch) | |

Per-night rate in `p[1]` is `p[44][0] / nights` only on rooms priced evenly;
the skill reports `p[44]` for the stay and the row's own nightly, never
deriving one from the other.

### An OTA row (`p[21][i]`; same shape in `p[2]`, `p[12]`, `p[22]`)

| Path | Meaning | dealbase row (#8) |
|---|---|---|
| `o[0][0]` | seller name | `"dealbase.com"` |
| `o[0][1]` | partner id (stable: Expedia.ca 89, Booking.com 184, Priceline 220, Hotels.com 1162912808, Trip.com 84, the Fairmont's own site 76) | `1930230291` |
| `o[0][2]` | click-out URL (`/aclk`, `/travel/lodging/clk`) — never fetch | |
| `o[0][3][0]` | logo | |
| `o[12][4]` | **nightly** `[ex str, incl str, ex float, incl float, ex int, incl int]` | `['$1,283','$1,452',1282.75,1452.375,1283,1452]` |
| `o[12][5]` | **stay total**, same layout | `['$2,566','$2,905',2565.5,2904.75,2566,2905]` |
| `o[12][12][0]` | `[2, null, 1]` on every row seen; Priceline/Samesun had `[4, null, 0]` — meaning unknown | |
| `o[12][12][1]` | **free-cancellation deadline** `[1, "Nov 1", "4:00 PM", "11/1"]`, or `[0]` / `null` when none | Fairmont direct: until Nov 1 4 PM; dealbase: none |
| `o[12][13]` | `[0, 0]` or `[0, 1]` — unknown flag | |

**Which row is the lead price?** In all 24 dated and undated entity captures
the `p[1][2]` float appears verbatim as some row's `o[12][4][2]`
(`echo-summary.json`, `ota_rows` vs `lead`). But the rule choosing it is not
the minimum: on the default date the Fairmont's own site listed 1022.56 while
`p[1]` said 1053 (#9, #14, #15, #25). It is usually, not always, `p[22][1]`.
The skill reports the lead *and* the true minimum with its seller, and names
the lead's seller by float match — never by position. The hotel's own site
appears under the hotel's name (`"Fairmont Banff Springs"`, partner id 76),
not as "Official site" (string absent from every capture) — hence `own_site`
is name equality with `e[1]`.

### Sellers are the union of four slots

`fixture_plus270.html` (#38): `p[1]`, `p[21]`, `p[22]`, `p[44]` are all
`null`, and `p[2]` holds **three** priced offers (Vio.com 1587851245, Trip.com
84, goseek.com 695484593 — each `CA$1,254` ex / `CA$1,410` incl per night),
`p[12]` two of them. Reading `p[21]` alone reports "no rates" while three
sellers list the stay.

And `p[2]` is not a subset of `p[21]` even when both exist: on
`fixture_samesun_cid.html` (#37), `p[2]` has Trip.com (84) which is absent
from the six `p[21]` rows. So **"all sellers" = `p[2]` ∪ `p[12]` ∪ `p[21]` ∪
`p[22]`, de-duplicated by partner id `o[0][1]`**, keeping the row with the
most fields; any of the four may be `null`. Row shape is identical across the
four; only the strings differ (`p[2]`/`p[12]` print `CA$1,254`, `p[21]`/`p[22]`
print `$1,254` — same floats).

### One-basis rows: slot `[2]` is the all-in figure

Rows with `o[12][4] = ['$1,453', null, 1452.8091, null, 1453]` (Amimir,
BusinessHotels on Fairmont; Bluepillow on Samesun) have the incl-tax slots
`[1]` and `[3]` null and `[5]` absent, with `[2]` populated. `[2]` is the
**all-in** price, not ex-tax: Amimir's 1452.81 and BusinessHotels' 1452.30 sit
on the neighbours' incl-tax figure (1452.4–1453.7), far above their ex-tax
(1282–1317); and Bluepillow, which is the lead on Samesun, gives `p[44]` =
`[435.82, 0, 57.60, 493.42]` = 2 × 246.71 — base plus fees, no tax line, i.e.
the 246.71 is the total the guest pays per night. The stay slot of such a row
is single too (`['$2,906', null, 2905.62, null, 2906]`). Reading `[2]` as
ex-tax would under-quote by 13 %; this is why `basis: "single"` exists and why
`comparable()` puts single rows on the incl basis with a ≥ 1.00 tie rule
(`SKILL.md`).

## Amenities

Hotel amenities arrive as ids; names exist only in the rendered DOM
(`<span class="LtjZ2d">Indoor pool</span>`), not in any `ds:` block. They live
in three places under `e[10][6]`:

- **`e[10][6][1]` — the four highlight chips.** `[[has, id, qualifier?], …]`
  renders as the first four `LtjZ2d` spans, with the qualifier as a sibling
  `span` whose class list contains `AdLXZd` (`class="AdLXZd tdMWuf"`).
  Fairmont: `[[1,19],[1,26],[1,10],[1,28,1]]` → Pool, Spa, Hot tub, Wi-Fi
  *free*. Samesun: `[[1,54,1],[1,28,1],[1,15,2],[1,23]]` → Breakfast *free*,
  Wi-Fi *free*, Parking *extra charge*, Restaurant. Chips are identical on the
  2-adult and child-5 Fairmont pages (occupancy does not change them). A
  rental (`p23_vr_ny`) has `e[10][6][1] = null` and no spans at all.
- **`e[10][6][0]`, `[2]`, `[3]` — grouped lists** `[[group id, [[has, id,
  qualifier?, …], …]], …]`, with `[2]`/`[3]` nesting groups one level deeper
  than `[0]` — a top-level-only walk finds 58 ids on the Fairmont where the
  recursive walk finds 94 (Samesun 48). The DOM's `div.IYmE3e` groups (`h4`
  heading + `li.IXICF` items) are a *permutation* of the non-empty groups of
  `[0]` plus the groups of `[3]` — Fairmont 15 + 5 = 20 groups / 72 items on
  both sides, Samesun 12 / 34 — and `[2]` renders separately as the Health &
  safety section (`ul.exOJL` → `li.ZQnR8e`, DOM order = JSON order). Groups
  were matched by signature (item count, which items are negated — `has = 0`
  ↔ "No …"/"Not …" — and which carry a qualifier), then required to be
  consistent across the three pages. Result: **0 conflicts**, 15 group
  headings proven, 4 unresolved.
- **`has = 0` means the hotel explicitly lacks it**: Samesun's id 19 renders
  as "No pools", 27 as "Not accessible". A bare id list would present that
  as a pool. The negated label is Google's, not derivable from the name.
- **Qualifiers are on the base id**: `1` free, `2` extra charge, `3` 24 hour
  (Samesun `[1,33,3]`, front desk). "Free Wi-Fi" is `28 + 1`; there is no
  separate free-wifi id in entity space (list cards use pre-qualified ids —
  29 Free Wi-Fi, 6 Free breakfast, 165 Breakfast ($), 16/17 parking — a
  different id space).

Machine-readable table with page lists and qualifiers:
`amenity-entity-table.json` — **the authoritative count is whatever that JSON
holds**; `AMENITY_NAMES` / `AMENITY_GROUPS` are generated from it and the
self-check asserts the shipped constants equal it. It is **provisional:
derived from two distinct properties** (Fairmont appears on three fixtures
and counts once). **The shipped table names 80 ids** — the grouped lists in
`e[10][6][0]` and `[3]`, 5 of them by elimination (marked *inferred*). The 25
Health & safety ids (57–85, in `e[10][6][2]`, groups 15–19) were matched in
the DOM but are **not in the JSON and not in the code**: they render as
unnamed. So do 7, 124, 169, 172, 175 and 184 (the three unresolved 2-item
groups). On the Fairmont that is 94 amenities, 28 unnamed; unnamed ids ship
with `name: null` and are counted in `amenities_unnamed`.

### Entity-space amenity table (ids as they appear in `e[10][6]`; the shipped 80)

F = `fixture_fairmont_2ad_ny.html`, P = `fixture_plus270.html` (the same
hotel), S = `fixture_samesun_cid.html`.

| id | name (as rendered) | group heading | pages | qualifier seen |
|---|---|---|---|---|
| 4 | Bar | Food & drink | FPS |  |
| 8 | Fitness center | Wellness | FPS | 1=free |
| 9 | Golf | Activities | FP |  |
| 10 | Hot tub | Pools | FPS |  |
| 11 | Kid-friendly | Children | FP |  |
| 14 | Kitchen in some rooms | Rooms | S |  |
| 15 | Parking | Parking & transportation | FPS | 2=extra charge |
| 18 | Pet-friendly | Pets | FPS | 2=extra charge |
| 19 | Pool | Pools | FPS |  |
| 20 | Indoor pool | Pools | FP |  |
| 22 | Outdoor pool | Pools | FP |  |
| 23 | Restaurant | Food & drink | FPS |  |
| 24 | Room service | Food & drink | FP |  |
| 25 | Smoke-free property | Policies & payments | FPS |  |
| 26 | Spa | Wellness | FPS |  |
| 27 | accessible | Accessibility | S |  |
| 28 | Wi-Fi | Internet | FPS | 1=free |
| 31 | Full-service laundry | Services | FPS |  |
| 33 | Front desk | Services | FPS | 3=24 hour |
| 34 | Sauna | Wellness | FP |  |
| 35 | Massage | Wellness | FP |  |
| 37 | Credit cards | Policies & payments | S |  |
| 38 | Concierge | Services | FP |  |
| 39 | Car rental onsite | Parking & transportation | FP |  |
| 40 | Convenience store | Services | FP |  |
| 41 | Bicycle rental | Activities | FP | 1=free |
| 44 | Tennis | Activities | FP |  |
| 47 | Horseback riding | Activities | FP |  |
| 51 | Baggage storage | Services | FPS |  |
| 54 | Breakfast | Food & drink | FPS | 1=free, 2=extra charge |
| 56 | Breakfast buffet | Food & drink | S |  |
| 89 | Bathtub in some rooms | Bathrooms | FPS |  |
| 90 | Boutique shopping | Activities | FP |  |
| 93 | Cash | Policies & payments | S |  |
| 94 | Cats allowed | Pets | FP |  |
| 96 | Coffee maker | Rooms | FP |  |
| 98 | Debit cards | Policies & payments | S |  |
| 100 | Dogs allowed | Pets | FP |  |
| 102 | EV charger | Parking & transportation | FP |  |
| 103 | Elevator | Services | FP |  |
| 104 | Elliptical machine | Wellness | FP |  |
| 105 | English | Languages spoken | FPS |  |
| 107 | Free weights | Wellness | FP |  |
| 109 | German | Languages spoken | S |  |
| 110 | Gift shop | Services | FP |  |
| 112 | Housekeeping | Services | FPS |  |
| 116 | Activities for kids | Children | FP |  |
| 117 | Kids' club | Children | FP |  |
| 122 | Local shuttle | Parking & transportation | FP |  |
| 129 | NFC mobile payments | Policies & payments | S |  |
| 130 | Accessible elevator | Accessibility | FP |  |
| 131 | Accessible parking | Accessibility | FP |  |
| 135 | Private bathroom | Bathrooms | FP |  |
| 136 | Private bathroom in some rooms | Bathrooms | S |  |
| 137 | Private car service | Parking & transportation | S | 2=extra charge |
| 142 | Hair salon | Wellness | FP |  |
| 143 | Self parking | Parking & transportation | FPS | 2=extra charge |
| 144 | Shower | Bathrooms | FP |  |
| 145 | Shower in some rooms | Bathrooms | S |  |
| 146 | Social hour | Services | S |  |
| 148 | Table service | Food & drink | FPS |  |
| 151 | Treadmill | Wellness | FP |  |
| 153 | Valet parking | Parking & transportation | FP | 2=extra charge |
| 154 | Vending machines | Food & drink | S |  |
| 156 | Wading pool | Pools | FP |  |
| 157 | Wake up calls | Services | FP |  |
| 161 | Weight machines | Wellness | FP |  |
| 162 | Wi-Fi in public areas | Internet | FPS |  |
| 163 | air conditioning | Rooms | S |  |
| 164 | Air conditioning in some rooms | Rooms | FP |  |
| 190 | Donates excess food *(inferred)* | Waste reduction | FP |  |
| 192 | Food waste reduction program *(inferred)* | Waste reduction | FP |  |
| 193 | single-use plastic straws *(inferred)* | Waste reduction | FP |  |
| 203 | Safely disposes of electronics, batteries, and lightbulbs *(inferred)* | Waste reduction | FP |  |
| 207 | Safely handles hazardous substances *(inferred)* | Waste reduction | FP |  |
| 210 | Locally sourced food and beverages | Sustainable sourcing | FP |  |
| 213 | Vegetarian meals | Sustainable sourcing | FP |  |
| 214 | Organic cage-free eggs | Sustainable sourcing | FP |  |
| 215 | Organic food and beverages | Sustainable sourcing | FP |  |
| 254 | Green Key Eco Rating | Eco certifications | FP |  |

Group ids → headings:

| group id | heading |
|---|---|
| 2 | Accessibility |
| 3 | Activities |
| 5 | Children |
| 6 | Food & drink |
| 7 | Internet |
| 8 | Parking & transportation |
| 9 | Pets |
| 10 | Policies & payments |
| 11 | Pools |
| 12 | Rooms |
| 13 | Services |
| 14 | Wellness |
| 20 | Bathrooms |
| 21 | Languages spoken |
| 25 | Sustainable sourcing |
| 26 | Eco certifications |
| 24 | Waste reduction *(inferred by elimination)* |
| 15–19 | Health & safety sub-lists (`li.ZQnR8e` under the Health & safety section, DOM order = JSON order) — **not shipped**; see below |
| 4, 22, 23 | one of Business & events / Water conservation / Energy efficiency — **unresolved** (all 2-item groups, identical signatures) |

#### Health & safety ids — matched in the DOM, NOT shipped

`e[10][6][2]` (groups 15–19) renders as the Health & safety section; the DOM
labels below were aligned by position (DOM order = JSON order) on the
Fairmont (F) and Samesun (S). They are **not in `amenity-entity-table.json`
and not in the shipped constants**, so the CLI carries these ids with
`name: null`. Kept here so a maintainer can add them once the alignment is
confirmed on a third property.

| id | DOM label | pages |
|---|---|---|
| 57 | Enhanced cleaning of common areas | FS |
| 58 | Enhanced cleaning of guest rooms | FS |
| 60 | Commercial-grade disinfectant used to clean the property | FS |
| 62 | Employees trained in COVID-19 cleaning procedures | FS |
| 63 | Employees trained in thorough hand-washing | FS |
| 64 | Employees wear masks, face shields, and/or gloves | F |
| 65 | Additional safety measures during food prep and serving | FS |
| 66 | Additional sanitation in dining areas | FS |
| 67 | Individually-packaged meals | F |
| 68 | Disposable flatware | S |
| 69 | Single-use menus | S |
| 70 | High-touch items, such as magazines, removed from common areas | FS |
| 71 | High-touch items, such as decorative pillows, removed from guest rooms | FS |
| 73 | Plastic key cards are disinfected or discarded | F |
| 74 | Buffer maintained between room bookings | FS |
| 76 | No-contact check-in and check-out | S |
| 77 | Hand-sanitizer and/or sanitizing wipes in common areas | FS |
| 78 | In-room hygiene kits with masks, hand sanitizer, and/or antibacterial wipes | F |
| 79 | Masks and/or gloves available for guests | F |
| 80 | Masks required on the property | F |
| 81 | Physical distancing required | F |
| 82 | Safety dividers at front desk and other locations | F |
| 83 | Guest occupancy limited within shared facilities | F |
| 84 | Private spaces designated in spa and wellness areas | F |
| 85 | Common areas arranged to maintain physical distancing | F |

**Gate ids** (each on ≥ 2 pages, i.e. two distinct hotels — the minimum; the
self-check re-checks these against each fixture's DOM rather than trusting the
constant):

| Concept | Entity id | Pages | Note |
|---|---|---|---|
| Free Wi-Fi | **28 + qualifier 1** | F, P, S (chip and Internet group on all three) | no separate "Free Wi-Fi" id in entity space |
| Parking | **15** (+ qualifier 2 = extra charge) and **143 Self parking** (+2) | F, P, S | no free-parking page was captured, so "15 + qualifier 1" is unobserved |
| Breakfast | **54** (+1 free on S, +2 extra charge on F) | F, P, S | |
| Pool | **19** | F, P (has = 1); S (has = 0, "No pools") | also 20 Indoor pool, 22 Outdoor pool (F, P); 10 Hot tub (F, P; S negated) |

Any id whose rendered label changes fails the self-check, not the user.

## Vacation rentals

Present in the list (6 of 20 Banff cards) and reachable on the entity path
with dates (#23, `p23_vr_ny.ds1.json`; full page `fixture_rental_ascent_ny.html`,
#41): `e[14] == 2`, `e[3]` null, `e[9]` null, `e[19][1]` sleeps / bedrooms /
bathrooms / beds, `e[10][1]` = `["Amenities", [[name, has(1/0), icon, …], …]]`
(named — "Air conditioning", "Balcony", "Crib"), `e[10][3]` = `["Essential
info", [["Entire apartment", …], ["Sleeps 4", …], …]]`, sellers Vrbo.com /
Expedia.ca / the property manager, `p[2]` room offers empty, `p[21]` 3 rows,
`p[44]` = `[716.09, 102.09, 212, 1030.19]` — the **fees** slot (cleaning) is a
fifth of the total, which is why a rental's incl-tax stay figure must be the
one quoted. No Airbnb rows appeared in any capture.

## Failure modes and detection

| Situation | What Google sends | Detect by |
|---|---|---|
| unknown entity (bad CID, KG-only token) | 200, empty `<title>`, `ds:0=[]`, `ds:2` present, and a `ds:1` block `data:[5],errorHasStatus: true` with no `sideChannel` | `ds:2` present ∧ `ds:1` carries `errorHasStatus` / parses to a single int → usage error (exit 2) |
| dates/occupancy Google will not price | 200, full page **for the default stay** | echo `[6][1][4]`/`[13]` ≠ request → payload error (exit 3), never "no rates" |
| currency not honoured | 200, page priced in the market default | `[6][1][3]` and `p[15]` ≠ request → exit 2 naming the currency (separate from the echo check) |
| no rates for valid dates | 200, echo matches, seller union empty | exit 1 |
| search-path fetch | 302 to a disallowed path | never follow redirects; any 3xx is exit 3 with the Location named |
| block / captcha / consent | **never observed** in 44 requests at ≥ 2.5 s | absence of `ds:2` (present on every real page, including the unknown-entity one) is the positive signal; `recaptcha` appears in every healthy page, so marker-matching is wrong here too |

### The unknown-entity signature, exactly

`echo-summary.json` listed `ds:1` for #7/#18 because it tested the substring
`AF_initDataCallback({key: 'ds:1'`, while the block parser (which needs
`data:… , sideChannel`) skipped it. Both were right about what they saw. The
block is:

```
AF_initDataCallback({key: 'ds:1',  data:[5],errorHasStatus: true,});     # #18, #35: bogus CID
AF_initDataCallback({key: 'ds:1',  data:[3],errorHasStatus: true,});     # #7: KG-id-only token
```

No `hash`, no `sideChannel`, `data` is a one-integer status (`5` and `3`
observed; meanings unknown), and `errorHasStatus: true` — a key that appears
nowhere on a healthy page (0 hits in every fixture). `<title>` is empty,
`ds:0` is `[]`, `ds:2` is the normal 2150-byte block, and there are no
`LtjZ2d` amenity spans (`unknown_entity_full.html`, 1,662,380 bytes).

**Signature:** `ds:2` present **and** the `ds:1` block carries
`errorHasStatus: true` (or, equivalently, parses to a list whose only element
is an int rather than a record). Do not define it as "`ds:1` absent" — the
block exists; a parser that keys on `key: 'ds:1'` alone will find it and then
choke on `[5]`. Exit 2.

## Query surface summary (allowed path)

| Want | How | Verified |
|---|---|---|
| dates | `ts` field 3 | yes, 6 date ranges |
| nights | 1–30 | 31+ silently defaults |
| horizon | ≤ 330 days | 347 and 365 silently default |
| adults | `ts` 2.1 repeated `{1:3}` | 1 and 2 |
| children | `ts` 2.1 `{1:2, 2:age}` | ages 5; 5+10 |
| currency | `ts` 5.1.7 | CAD, USD |
| language | `hl` | en, fr |
| point of sale | `gl` | changes only string formatting |
| rooms | — | no field found; one room always |
| sort, price cap, rating, stars, free cancellation, hotel type | list-page filters only (`ds:0` widget 38 lists the chips with KG ids) | **not available** on the entity path; filter client-side over per-hotel fetches |

## Full-page fixtures

| Fixture | Echo | Lead | `p[44]` | Rows `p[2]/p[12]/p[21]/p[22]` |
|---|---|---|---|---|
| `fixture_fairmont_2ad_ny.html` (#36, 3.65 MB) | `[[2026,12,31],[2027,1,2],2]`, `[2,null,0]` | `1282.75` (dealbase) | `[2435.63, 339.25, 129.88, 2904.75]` | 4 / 2 / 11 / 2 |
| `fixture_samesun_cid.html` (#37, 2.87 MB) | same | `246.71` (Bluepillow, single) | `[435.82, 0, 57.60, 493.42]` | 4 / 2 / 6 / 2 |
| `fixture_plus270.html` (#38, 3.44 MB) | `[[2027,6,9],[2027,6,11],2]`, `[2,null,0]` | `null` | `null` | **3 / 2 / null / null** |
| `fixture_rental_ascent_ny.html` (#41, 2.08 MB, kind 2) | `[[2026,12,31],[2027,1,2],2]`, `[2,null,0]` | `464.05` (Expedia.ca) | `[716.09, 102.09, 212, 1030.19]` | 0 / 0 / 3 / 3 — no chips, no `LtjZ2d` spans |
| `fixture_fairmont_2ad_child5.html` (#42, 3.44 MB) | same dates, `[2,[[5]],0]` | `7139` | `[14148, 1887.5, 130, 16165.5]` | 2 / 2 / 2 / 2 — chips identical to the 2-adult page |
| `unknown_entity_full.html` (#35, 1.66 MB) | — | — | — | `ds:1` = `data:[5],errorHasStatus: true` |

Worked selection cases from these (the tie rule): on the Fairmont,
BusinessHotels.com's single 1452.30 is seven cents under dealbase's 1452.375
incl → dealbase is cheapest (and the headline), BusinessHotels flagged "≈
same". On the Samesun, Bluepillow's single 246.71 undercuts the best two-basis
row (Super.com 803.63) by far → Bluepillow is cheapest, printed as a single
figure treated as all-in — excluding single rows would wrongly report
Super.com.

## Volatility

- **Never cache:** anything under `e[6]` — the lead moved 1,283 → 1,198 →
  1,053 between fetches minutes apart, and the OTA row count varied 10–27.
- **Stable for days:** name → CID/ftid/place_id/token, `e[2]`, `e[3]`,
  `e[9]`, the currency catalogue (`ds:2`), amenity ids. The shipped skill
  nevertheless caches nothing at all (ids convert offline; a wrong cached
  building is worse than the request saved), so there is no cache file to go
  stale.

## The Maps HTML paths are JavaScript shells

`robots.txt` (`google-robots.txt`): `Disallow: /search` (line 3) but `Allow:
/search?*tbm=map` (line 109) in the same `User-agent: *` group — the endpoint
google-maps uses is explicitly allowed by the longer match. `/maps/search/`
(line 97) and `/maps/place/` (line 107) are allowed too, so both were probed
without following redirects (#39, #40): HTTP 200, ~208 KB, title "Google
Maps", but `window.APP_INITIALIZATION_STATE` merely echoes the query with
**no result list, no ftid, no CID, no place_id** anywhere in the page.
Neither page carries `AF_initDataCallback`. So no allowed `/maps/…` HTML path
yields a place without JavaScript; name resolution stays in google-maps.

## The list page (disallowed in phase 1; reference for the phase-2 extension)

Documented from the two accidental captures, both priced for the default
stay. `ds:0[0]` is a widget tree `[[9, [subwidgets…], …, {pagination}], [38,
{filters}], [50, {map}]]`.

- **Cards:** `ds:0[0][0][0][1][i]` with `[0] == 34`; the record is
  `[1]['397419284'][0]` (the key was constant across both captures). 20 cards
  per page, 6 of them rentals in a Banff search. Card `e[6][2][21]` is
  **empty for hotels** — the seller of a card's lead price is not on the list
  page at all.
- **Pagination:** `ds:0[0][0][0][3]` = `{'469845717': ['CBI=', '', 1378, 1,
  20]}` → next-page token, ?, 1378 total results, page 1, page size 20.
- **Sponsored ads** (`[0] == 71`, `[1]['300000000'][2]`): 9 records shaped
  `[name, "/aclk?…", "CA$224", null, reviews, rating, "Booking.com", logo, "",
  [amenity ids], star?, 1, …, token, 0, avs, [lat,lng], null, ["CA$224",
  "CA$254", "CA$254"], ["2026-10-13","2026-10-14", 1, 31, 2, …], [partner id,
  0, null, base, tax, fee, 3], [thumbs]]`. An OTA name at `[6]` and a price
  that belongs to the ad's own date — skip ads structurally.
- **Filter metadata** (widget 38): total count, a 23-bucket price histogram
  with no bucket edges, chain list, suggested chips (`"Under $175"`, `"4+
  rating"`, `"4- or 5-star"`, `"Pool"`) with KG ids.
- **Card amenities** `e[10][8]` = `[[1, id], …]` in a **list-card id space**
  (pre-qualified: 6 Free breakfast, 165 Breakfast ($), 29 Free Wi-Fi, 16 Free
  parking, 17 Parking ($), plus 1 Airport shuttle, 2 Air conditioning, 4 Bar,
  8 Fitness center, 10 Hot tub, 11 Kid-friendly, 12 Kitchen in rooms, 14
  Kitchen in some rooms, 18 Pet-friendly, 19 Pool, 20 Indoor pool, 21 Pools,
  23 Restaurant, 25 Smoke-free property, 26 Spa, 27 Accessible, 31
  Full-service laundry, 54 Breakfast), aligned from the DOM accessibility
  string (`span.lXJaOd`, first eight per hotel) across 13 hotels. Not the
  entity id space.
- **The "typical price" widget** (sub-widget 56; also widget 38's tail):
  `[null, [[[[3]],[[3]],[[3]],[[2]],[[3]],[[4]],[[5]],[[5]],[[5]],[[4]],[[3]],[[3]]]],
  [["CA$187", 0.32, 2, "CA$140", "CA$369"]]]` — 12 one-element buckets valued
  2–5 (ordinal price level, captioned "See hotel price trends"; the pattern
  fits Banff's April trough and July–September peak, so Jan…Dec is
  *consistent, not proven*), then `[typical nightly, 0.32, 2, low, high]`. It
  moved 187 → 194 between two fetches minutes apart, so it is live. **No
  verdict word, no advice** ("low/typical/high", "usually" — searched, absent)
  — so the extension is `trend` (a band), not `price-check`. **There is no
  per-hotel equivalent** on the entity page. Whether it moves with the
  *requested* dates is unknown — `trend` must exit 1 until a live check
  proves it.
- The list page echoes only dates and currency (`ds:0[1][0][2][1]`,
  `ds:0[1][0][3][0][6]`).

## UNKNOWN / not established

- Any list-page behaviour under real dates or filters (never fetched with
  `ts`).
- The selection rule for the lead price row (`p[1]`); whether `p[22]` is
  "official + lead" or just "top two".
- Meaning of `p[10]`, `p[17]`, `p[25]`, `p[26]`, `p[18]=[0,2]`, `p[24]`,
  `p[27]=[1,1]`, `p[37]`, `p[40]`, `o[12][12][0]`, `o[12][13]`; the
  unknown-entity status ints 3 and 5.
- Trend widget: slot-to-month mapping, the `0.32` and `2`, date sensitivity.
- Exact booking horizon between 330 and 347 days.
- Whether `ts` has a rooms field (all tests were one room).
- The six unnamed amenity ids and the three unresolved 2-item groups; a
  free-parking page (`15 + qualifier 1`) was never captured.
- Whether the same page served to an EU egress address shows a consent wall
  (all requests left from Canada with `CONSENT=YES+cb`); the shape of a real
  block page.
- Same-day check-in (`+0`): accepted by the validators, never captured — the
  echo check is what protects it.
- Reviews sub-path (`/travel/hotels/entity/<tok>/reviews`) — not fetched.
- Whether a rental id (token field 1.2) can be obtained anywhere but the list
  page or a pasted link.
- Any sign of rate limiting past 44 requests.

## Evidence index

| File | What |
|---|---|
| `google-robots.txt` | robots.txt as fetched 2026-09-12 |
| `requests.log` | every request: number, time, status, bytes, label, final URL |
| `echo-summary.json` | per-request digest: title, blocks, echo dates/occupancy/currency, lead, `p[44]`, OTA rows |
| `banff_oct.ds0.json`, `banff_ny.ds0.json` | the two list pages (redirect followed by mistake; both priced the default date) |
| `p5_entity.ds1.json` | Ascent Amour rental, no `ts` |
| `p8_fairmont_ts.ds1.json` | Fairmont, 2 adults, 2026-12-31 → 2027-01-02 — the worked example |
| `p8_fairmont_ts.ds2.json` | the 2150-byte currency block (`CURRENCIES` source) |
| `p11_1adult.ds1.json`, `p12_2ad_child5.ds1.json`, `p13_usd.ds1.json` | occupancy and currency variations |
| `p23_vr_ny.ds1.json` | rental with dates |
| `p28_plus270d.ds1.json` | genuine empty headline (echo matches, `p[21]` null) |
| `p29_samesun_ny.ds1.json` | third hotel, via a CID-only token |
| `p7_fairmont_kgonly.head.html`, `p18_bogus_cid.head.html` | first 3 KB of the unknown-entity pages |
| `unknown_entity_full.html` | full unknown-entity page (#35) |
| `fixture_fairmont_2ad_ny.html`, `fixture_samesun_cid.html`, `fixture_plus270.html`, `fixture_rental_ascent_ny.html`, `fixture_fairmont_2ad_child5.html` | full pages (all `ds:` blocks, `<head>`, rendered amenity spans) — #36, #37, #38, #41, #42 |
| `amenity-entity-table.json` | the entity-space amenity table, machine-readable, with page lists and qualifiers |
