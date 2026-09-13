# Uber Eats — API research notes

Background for the shipped skill. This file records what the data surface
looks like and how each field's meaning was established, so the
reverse-engineering does not have to be redone when Uber changes something.
It is not the interface contract — that is `SKILL.md`.

Everything below was observed on **2026-09-13** against `www.ubereats.com`
from one residential Toronto IP: 44 HTTP requests in total (11 manual `curl`
calls, then 33 through a small logged helper), no account, no cookies other
than the delivery-address cookie `uev2.loc` and Cloudflare's own `__cf_bm`.
Nothing was added to a cart and no endpoint outside the read allowlist (§9)
was ever called. Capture names (`feed2.json`, `store_pctoff.json`, …) are the
probe's evidence files; the trimmed, gzipped copies under `scripts/fixtures/`
carry the names the test plan gave them. Where something is inferred rather
than observed, it says so.

**Owner decision, recorded:** Uber's Terms of Use prohibit automated access
and its `robots.txt` cannot be read (it is behind the challenge). The owner
chose the unofficial web API anyway on 2026-09-13, because the official
Uber Eats connector answers no price question. The skill stays read-only,
low-volume, and makes no attempt to evade the bot protection; if Uber
objects, this decision is the one to revisit.

## 1. Protection layers — the finding that shapes everything

| Path | Result | Evidence |
|---|---|---|
| `/robots.txt` | `403`, Cloudflare challenge page | manual curl |
| `/ca`, `/ca/city/toronto-on` (HTML) | `403`, header `cf-mitigated: challenge`, body "Just a moment…" | `city.html` |
| `/_p/api/getSearchSuggestionsV1` | **challenged both times tried** (morning and afternoon) — endpoint-level protection, not IP reputation | `getSearchSuggestionsV1.json`, `suggest.json` |
| `/_p/api/mapsSearchV1`, `getDeliveryLocationV1`, `getFeedV1`, `getSearchFeedV1`, `getStoreV1`, `getMenuItemV1` | `200` JSON to a plain `curl` with a browser UA | every other capture |
| `getFeedV1`, `getStoreV1` after ~110 requests/day from one IP | `403` with a JSON `botdefense` / `RECAPTCHA` body — Uber's own layer, below | build smoke runs |

So the website's pages are unreachable without a browser, but the JSON API
the pages call is not challenged for the endpoints the skill needs. This is
the premise of the whole skill and the thing most likely to change.

**Burst test:** 16 consecutive `getStoreV1` calls — 10 spaced 1 s, 6
back-to-back at ~0.7 s each — all `200` with byte-identical bodies
(`requests.log` lines 11–26). No challenge, no throttling signal. One IP, one
afternoon; not a guarantee. The shipped throttle is 1.0 s.

**Uber's own bot defense (found after the design probe, 2026-09-13):**
after roughly **110 requests from one residential IP within a day** (the 44
probe requests plus build smoke runs), `getFeedV1` and `getStoreV1` began
answering HTTP `403` with a JSON body —

```json
{"status": "failure", "metadata": {"botdefense": {"state": "challenge", "provider": "RECAPTCHA"}}}
```

— while `mapsSearchV1` kept returning `200`. This is a second layer, distinct
from Cloudflare: a JSON body, not an HTML page, and per-endpoint. It lasted
hours, not seconds. The transport detects `metadata.botdefense.state ==
"challenge"` and reports exit 3, `kind: "blocked"`, with a message naming
"bot defense (reCAPTCHA)" — never "nothing found". No workaround exists and
none will be added; the skill's defence is volume: 1 s throttle, a 25-request
cap per command, and the guidance in `SKILL.md` to keep runs small and poll
a watch no more than every 15 minutes. The exact threshold and reset time
were not measured (one IP, one day).

**Gate G1 — Python transports:** `requests` and `urllib` both got `200` JSON
on `getStoreV1`, so Cloudflare is not fingerprinting TLS on these endpoints
today. The transport order is `requests` → `curl` → `urllib`, the same chain
as google-hotels.

**Detecting a challenge:** HTTP `403` *and* body containing `Just a moment`
or `_cf_chl_opt`; header `cf-mitigated: challenge`. The body is ~8 KB of HTML
where JSON was expected. It maps to exit 3 with a message naming Cloudflare
— never to "no restaurants". A `403` without the challenge body is still
exit 3 but says 403; a `200` whose body carries the challenge markers is
treated as blocked too (defensive; never observed).

## 2. Request shape (common to every endpoint)

```
POST https://www.ubereats.com/_p/api/<Endpoint>?localeCode=ca
User-Agent: <desktop Chrome UA>
x-csrf-token: x
content-type: application/json
Cookie: uev2.loc=<url-encoded JSON location, §3.2>
```

`x-csrf-token: x` — the literal letter — is what the site's own frontend
sends for anonymous calls; without it the API answers an error. Every
response is `{"status": "success"|"failure", "data": …}`. **A bad id is
still HTTP 200:** `{"status":"failure","data":{"message":"invalid_store_uuid","code":"404"}}`
(`api.json`, `recheck.json`). Status must be read from the body, not the HTTP
code. `invalid_store_uuid` is the one `failure` that means "your input";
every other `failure` is treated as payload drift (exit 3).

## 3. Location

### 3.1 `mapsSearchV1` — address text → candidates

Body `{"query": "Union Station Toronto"}` → `data[]` (`mapsSearchV1.json`,
`maps_kitchener.json`):

```json
{"id": "180933fc-e611-398d-9512-2a86a6ecca45", "provider": "uber_places",
 "addressLine1": "Toronto Union Station Train Station",
 "addressLine2": "65 Front St W, Toronto, ON M5J 1E6",
 "categories": ["TRAIN_STATION", "…"]}
```

`provider` is `uber_places` or `google_places` (the id then looks like
`ChIJ…`). **No latitude/longitude in this response** — hence G2.

### 3.2 `getDeliveryLocationV1` and the `uev2.loc` cookie (gate G2)

Body `{"placeReferenceType": "<provider>", "placeId": "<id>", "provider":
"<provider>"}` → `data` is exactly the cookie object
(`g2_deliveryloc.json`, 717 bytes):

```json
{"address": {"address1": "Toronto Union Station Train Station",
             "address2": "65 Front St W, Toronto, ON M5J 1E6", "aptOrSuite": "",
             "eaterFormattedAddress": "65 Front St W, Toronto, ON M5J 1E6, CA",
             "subtitle": "65 Front St W, Toronto, ON M5J 1E6",
             "title": "Toronto Union Station Train Station", "uuid": ""},
 "latitude": 43.6452223, "longitude": -79.3806428,
 "reference": "180933fc-e611-398d-9512-2a86a6ecca45",
 "referenceType": "uber_places", "type": "uber_places",
 "addressComponents": ["…"]}
```

The cookie is that object (plus `"source": "manual_auto_complete"`) as JSON
with no spaces, URL-encoded. So `locate` is **two requests**: candidates,
then coordinates for the chosen one.

Why the coordinates matter, from three store fetches of the same restaurant:

| Cookie | `distanceBadge` | ETA | Capture |
|---|---|---|---|
| full, with coordinates | `5.3 km` | 40–60 min | `store.json` |
| reference id only, no coordinates | **2,263 mi**, 170 min | | `g2_reference_only.json` |
| no cookie at all (gate G3) | 91.8 km from some default point; menu and prices complete (98 items) | | `g3_nocookie.json` |

A reference-only cookie is accepted and silently wrong. No cookie is
harmless for the menu — which is why `menu`, `item` and `watch` take `--at`
as optional and null out distance, ETA and `within_range` without it.

### 3.3 Location token and cache

The token `locate` prints is base64url of the cookie JSON (no padding); it
decodes offline, so `--at <token>` costs no request. Resolved addresses are
the only thing cached (`locations.json`, 30 days, keyed by the folded query
text; in-memory when no directory is writable). Nothing else is ever
written: not feeds, not menus, not prices.

## 4. `getFeedV1` — restaurants near the location

Body: 19 keys, all empty strings / zero / false (`FEED_BODY` in
`ueats/client.py`; the site sends the same). Response 2.5–2.8 MB
(`feed.json.gz`, `feed2.json.gz`, `g4_feedSessionCount.json`).

| Field | Meaning |
|---|---|
| `data.currencyCode` | `"CAD"` |
| `data.isInServiceArea` | `true`; **`false` is exit 2** ("Uber Eats doesn't deliver to <address>") — never "no stores" |
| `data.diningModes` | `DELIVERY`, `PICKUP` |
| `data.feedItems[]` | 129 items: 93 `REGULAR_STORE`, 15 `REGULAR_CAROUSEL`, 17 `DIVIDER`, 1 each of `ANNOUNCEMENT`, `FEATURED_STORES`, `CATALOG_ITEMS_CAROUSEL_PAYLOAD`, `SECTION_HEADER` |
| `data.storesMap` | **empty** (`{}`) — do not rely on it |
| `data.sortAndFilters[]` | server-side filter definitions, §4.4 |
| `data.meta` | `{"offset": 112, "hasMore": true}` — pagination, §4.6 |

### 4.1 A store in the feed

`feedItems[].store` (type `REGULAR_STORE`) and `feedItems[].carousel.stores[]`
(type `REGULAR_CAROUSEL`) share one shape:

| Path | Example | Note |
|---|---|---|
| `storeUuid` | `80a1f654-f869-40b2-80bc-f15ba251c4ad` | the id every other call takes |
| `title.text` | `"Tim Hortons"` | no branch address here; `getStoreV1.title` has it |
| `rating.text` | `"4.6"`; `"New"` for a new store | count only inside `rating.accessibilityText` ("…based on more than 700…") |
| `meta[]` with `badgeType: "ETD"` | `text: "10 min"`, `accessibilityText: "Delivered in 10 to 20 min"` | the range is only in the accessibility text |
| `meta[]` with `badgeType: "EXCLUSIVE_STORE"` | "Only on Uber Eats" | |
| `meta[].badgeData.fare.deliveryFee` | seen **once** in 129 items | fees are essentially absent from the feed |
| `signposts[].text` | `"20% off select items"` | **the deal badge** |
| `mapMarker.latitude` / `longitude` | `43.6461, -79.3827` | distance is haversine from the address to these |
| `mapMarker.secondaryMarkerContent.text` | often the deal string again | duplicate of the signpost — dedupe per store |
| `actionUrl` | `/store/tim-hortons-55-york-street/gKH2VPhpQLKAvPFbolHErQ?diningMode=DELIVERY` | last path segment = **base64url of the 16 UUID bytes, no padding** — gate G6, verified offline on **404 of 404** feed stores |

**Not in the feed:** cuisine, price level, hours, full address, delivery fee
(bar the one). Cuisine lives only in `getStoreV1.cuisineList`, so `nearby`
has no `--cuisine`.

The same store appears in several places (a carousel *and* the main list).
**Dedupe by `storeUuid`.** The first capture held 319 distinct ids across all
item types; the second 297.

### 4.2 Deals in the feed

Where the strings live (counts in `feed2.json`):

```
154  feedItems[].carousel.stores[].signposts[].text
128  feedItems[].carousel.stores[].mapMarker.secondaryMarkerContent.text
 22  feedItems[].store.mapMarker.secondaryMarkerContent.text
 20  feedItems[].store.signposts[].text
```

23 of 93 `REGULAR_STORE` entries carry one. Texts seen (first snapshot, with
counts): `Buy 1, get 1` (167, plus 21 with a **leading space** — strip), `$5
off` (18), `20% off select items` (13), `$5 off $20+` (9), `20% off` (8), `$0
Delivery Fee` (8), `$5 off $50+` (7), `$3 off $15+` (6), `$3 off` (6), `10%
off` (6), `$0 Delivery Fee on $15+` (6), `25% off select items`, `20% off
$70+`, `$10 off`.

Typed values exist elsewhere in the payload (`promotionType` / `offersTag`):
`MULTI_SKU_FLAT` 7, `DISCOUNTED_ITEM` 6, `BOGO` 4, `FLAT` 3, `PERCENT` 2 —
far fewer than the text badges. The parser therefore reads the **text** (it
is what the user sees) and keeps the typed value as `raw_type` when present.

### 4.3 Deal grammar (every form observed)

```
Buy 1, get 1[ free]                      → bogo
<N>% off[ select items][ $<M>+]          → percent, percent=N, min_spend=M, select_items
$<N> off[ select items][ $<M>+]          → dollar, amount=N, min_spend=M, select_items
$0 Delivery Fee[ on $<M>+]               → free_delivery, min_spend=M
```

Anything else → `type: "other"`, text kept verbatim, **never dropped** — a
new badge wording must show up as an unknown deal, not vanish.

### 4.4 `sortAndFilters` (server-side; gate G5 open)

| `type` | label | options |
|---|---|---|
| `dealsFilter` | Offers | Offers |
| `bookingFeeFilter` | Delivery fee | $2, $4, $6, $6+ |
| `deliveryTime` | Delivery time | Under 30 min |
| `topEatsFilter` | From Uber Eats | Best overall |
| `ratingFilter` | Rating | 3+, 3.5+, 4+, 4.5+, 5 |
| `sort` | Sort | Recommended, Rating, Earliest arrival |

Each has a `uuid` and `options[]`. Whether sending them back changes the
result was **not tested**; every filter in the CLI is client-side, which is
correct either way. The delivery-fee filter is the only fee signal the
anonymous API offers at feed level, and it is a band, not a number.

### 4.5 Stability (gate G7 partly open)

Two snapshots 17 minutes apart (`feed.json.gz`, `feed2.json.gz`), same
address:

- Stores with a deal: 143 → 131; stores present in both: 106.
- Among stores in both, deals unchanged: **130 of 131** (one `$10 off` became
  `20% off $70+`).
- Order of the first 20 stores: **different**.

Deals are stable on a minutes scale; **the feed's membership and order are
not.** A store missing from one response is not proof it is missing from
Uber Eats — this is why `find`'s miss message says so, why `exhaustive` is
always false, and why `compare` orders by computed distance rather than feed
order. How deals move across a whole day (lunch / dinner / late) was not
measured.

### 4.6 Pagination (gate G4)

`data.meta = {"offset": 112, "hasMore": true}`. Re-sending the feed body with
`"pageInfo": {"offset": 112, "pageSize": 80}` returns 80 further
`REGULAR_STORE` items and `meta.offset 192, hasMore true`
(`g4_pageInfo.json`, 275 KB — a continuation page carries no carousels). A
bare top-level `offset` key is ignored. Hence `--pages N` (default 1, max 5)
on `nearby`, `find` and `deals`, one request per page, planned up front.

### 4.7 Search does not work (gate G8)

`getSearchFeedV1` with `userQuery: "pizza"` (two body variants: `search.json`,
`search_v3.json`) and `getFeedV1` with `userQuery` (`search2.json`,
`search_v4.json`) all return the **same generic list** (Walmart, Rorschach
Brewing, Michaels, Ashario Pets…) — zero pizza places in the first 10 each
time. `getSearchSuggestionsV1`, which the site calls first to turn a query
into a search id, is Cloudflare-protected. v1 therefore matches **names within
the feed**, and finds dishes by reading menus (`compare`). The self-check
asserts no code path calls a search endpoint.

## 5. `getStoreV1` — one restaurant

Body `{"storeUuid": "<uuid>", "diningMode": "DELIVERY"|"PICKUP", "time":
{"asap": true}, "cbType": "EATER_ENDORSED"}`. Response 240–520 KB.
Captures: `store.json` (Himalayan Kitchen & Bar, delivery),
`store_pickup.json` (same, pickup), `store_pctoff.json` (Albert's Real
Jamaican Foods), `store_deal.json` (Tikka by LTH), `store_pizza.json`
(Blondies Pizza).

### 5.1 Store fields

| Field | Example | Note |
|---|---|---|
| `uuid`, `title`, `slug` | `"Albert's Real Jamaican Foods (St. Claire Ave W)"` | title includes the branch |
| `isOpen`, `isOrderable`, `closedMessage` | `true`, `true`, `""` | a closed store still returns its full menu |
| `hours[]` | `{"dayRange": "Sunday", "sectionHours": [{"startTime": 660, "endTime": 1410}]}` | **minutes since midnight** (660 = 11:00, 1410 = 23:30); `dayRange` can be a span like "Monday - Friday" |
| `rating` | `{"ratingValue": 4.6, "reviewCount": "6000+"}` | count is a **string** with `+` |
| `etaRange.text` | `"40–60 Min"` | en dash |
| `distanceBadge.text` | `"5.3 km"` | relative to the cookie location |
| `location` | `address, streetAddress, city, region, postalCode, country, latitude, longitude` | |
| `phoneNumber` | `"+14166589445"` | |
| `cuisineList` | `["Caribbean", "Seafood", "Latin American", "Exclusive to Eats", "Jamaican"]` | contains non-cuisine tags |
| `modalityInfo.modalityOptions[]` | Delivery `"40 min"`; Pickup `"18 min • 4.6 km"` | pickup ETA comes from here |
| `isWithinDeliveryRange` | `true` | `false` → say so; pickup may still work |
| `priceBucket` | `""` | usually empty; not used |
| `fareInfo.serviceFeeCents` | **`null`** | fees not available anonymously (§8) |
| `fareBadge` | `null` | |
| `hasStorePromotion` / `promotion` | `true` / `null` | the flag is true but the object stays null; the deal lives on the items (§5.3) |
| `currencyCode` | `"CAD"` | |
| `featuredReviews` | present | not analysed; out of scope |

### 5.2 The menu

`catalogSectionsMap: {<menuUuid>: [block, …]}`. A map entry can be an empty
list. Each block:

```
{ "catalogSectionUUID", "type": "HORIZONTAL_GRID"|"VERTICAL_GRID",
  "payload": { "type", "standardItemsPayload": {
      "title": {"text": "Dinner"}, "sectionUUID", "catalogItems": [item, …],
      "promoUUID"        (only on a deal section),
      "paginationEnabled", … } } }
```

Section titles seen: `"Featured items"` (HORIZONTAL_GRID), `"Save on Select
Items"` (carries `promoUUID`), then the real sections (`"Dinner"`, …).

An item:

| Field | Example | Note |
|---|---|---|
| `uuid` | | **the same dish appears in several sections** |
| `title`, `itemDescription` | | |
| `price` | `1919`, `1087.5` | **cents; can be fractional**; on a discounted dish it is the **sale** price |
| `priceTagline.text` | `"$19.19"` | display only |
| `priceTagline.textFormat` | `<span><span style="color:#05944F">$10.88 </span><span style="text-decoration:line-through;…">$14.50</span></span>` | **the original price exists only here**, inside HTML, in the `line-through` span |
| `isSoldOut`, `isAvailable`, `itemAvailabilityState` | | |
| `hasCustomizations` | | → `getMenuItemV1` for options |
| `sectionUuid`, `subsectionUuid` | | needed for `getMenuItemV1` |
| `promoInfo` | `"Earn $7 Uber Cash for photo"` | a review reward, not a price |
| `purchaseInfo` | | not analysed |

### 5.3 Traps in the menu (each has a fixture and a test)

1. **Duplicates.** Himalayan Kitchen: 109 catalog entries, **98 unique
   uuids**. Tikka by LTH lists "Butter Chicken Hefty Bowl" three times.
   Dedupe by `uuid`; a dish's section is the first block that is neither
   `"Featured items"` nor a promo block (one carrying `promoUUID`, such as
   "Save on Select Items") — so Albert's deal dishes read "Lunch Specials"
   and Tikka's "Hefty Tikka Bowls". `duplicates_removed` is reported.
2. **`price` is already discounted.** Albert's: `price: 1087.5` with tagline
   `$10.88` struck against `$14.50` (25% off). A CLI that reads `price` as
   the list price understates the saving; one that reads the tagline's
   *first* dollar amount as the original gets it backwards. `was` comes only
   from the `line-through` span; two amounts with no `line-through` → `was`
   null and `price_unclear: true`.
3. **Fractional cents.** `1087.5`. Display rounds **half-even** — Uber's
   own rule, so `1012.5` shows as `$10.12` and `1087.5` as `$10.88`, matching
   the tagline; JSON keeps the raw number in `cents` and the rounded string
   in `amount`.
4. **Deal markers live on the item**, as text: `"25% off"` (19 of 94 entries
   at Albert's before dedupe), `"Buy 1, get 1 free"` (6 of 23 entries at
   Tikka: 2 dishes × 3 copies each). The
   store-level `promotion` is null even when `hasStorePromotion` is true.
5. **Promo-only items.** Himalayan's "Samosa Chaat" carried `promoInfo`
   "Earn $7 Uber Cash for photo". Reported as `note`, never as a deal or a
   `was` price.

### 5.4 Pickup vs delivery

Himalayan Kitchen, `PICKUP` vs `DELIVERY`: 98 unique items, **0 price
differences**; ETA pickup 18–28 min vs delivery 52–73 min. One store — the
CLI keeps `--pickup` as a mode switch and the docs do not claim prices are
always equal.

## 6. `getMenuItemV1` — one dish's options

Body:

```json
{"itemRequestType": "ITEM", "storeUuid": "…", "sectionUuid": "…",
 "subsectionUuid": "…", "menuItemUuid": "…",
 "cbType": "EATER_ENDORSED", "contextReferences": []}
```

`data.customizationsList[]` group: `uuid, title, minPermitted, maxPermitted,
minPermittedUnique, maxPermittedUnique, options[], groupId, displayState,
parentCustomizationOptionUuids`. Option: `uuid, title, price (cents),
defaultQuantity, minPermitted, maxPermitted, isSoldOut, quantityLabel,
subtitle, childCustomizationList[]` (nested groups — none seen live, walked
recursively, covered by a synthetic fixture).

Also on the item: `price`, `isSoldOut`, `suspendUntil`, `itemPromotion`,
`itemPromotionV2`, `membershipUpsellPromotion` (the Uber One upsell — never
reported as a price).

Blondies Custom Pizza — 16" Large, `price: 2300` (`item_pizza.json`):

| Group | min–max | Options |
|---|---|---|
| Choose Base Sauce | 1–1 | Tomato, Alfredo — free |
| Add Extra Meat Toppings | 0–7 | Bacon +5.75, Calabrese Salami +5.75, Fennel Salami +4.60, Italian Sausage +4.60, … |
| Add Extra Cheese Toppings | 0–6 | +4.60 each |
| Add Extra Vegetable Toppings | 0–12 | +4.03 each |
| Add Finishing Touches | 0–8 | free |
| Add Dipping Sauces | 0–4 | +2.88 each |
| Add Salads | 0–2 | +14.95 each |
| Add Drinks | 0–8 | +2.88 each |

Himalayan's Honey Chicken Chili (`item.json`): one group, "Choice of spice"
0–1, all free.

**"From" price** = item price + for every group with `minPermitted > 0`, the
cheapest `minPermitted` in-stock options (recursing into a chosen option's
own required groups). Pizza: 23.00 (sauce is free). A required group with a
*paid* cheapest option was not observed — the rule is inferred and covered
by a synthetic fixture. There is deliberately no "max price": unbounded
quantities make it meaningless.

## 7. Store ids (gate G6)

Three forms, all converted offline: a UUID; a store URL
`https://www.ubereats.com/ca/store/<slug>/<id>`; or the `<id>` segment alone,
which is the 16 UUID bytes as unpadded base64url (22 characters). Checked on
every store in the feed: 404 of 404 round-trip. Example pair: Tim Hortons
`80a1f654-f869-40b2-80bc-f15ba251c4ad` ↔ `gKH2VPhpQLKAvPFbolHErQ`. Anything
else in a `STORE` slot is treated as a name and resolved through the feed.

## 8. What is not available anonymously

| Thing | Evidence | Consequence |
|---|---|---|
| Delivery fee for an order | `fareInfo.serviceFeeCents: null`, `fareBadge: null`; a feed fee badge on 1 of 129 items | quote the `$0 Delivery Fee` deal text and the delivery-fee **filter band** only; never a total |
| Service fee | store page disclaimer: "Restaurant: varies per order but always between $2.50 and $6.50" | may be quoted, labelled as Uber's published range |
| Uber One / account promos | not visible logged out; `membershipUpsellPromotion` is an upsell, not a price | "public deals only" on every deals answer |
| Keyword / cuisine search | §4.7 | name matching within the feed; `compare` reads menus |
| Reviews | `featuredReviews` present on the store, not analysed | out of scope |
| Other countries | only `localeCode=ca` probed (gate G9 open) | `--locale` passed through, labelled unverified |

## 9. Endpoints — the read allowlist

The transport refuses, before opening a socket, any endpoint not in:

```
mapsSearchV1  getDeliveryLocationV1  getFeedV1  getStoreV1  getMenuItemV1
```

Endpoints the web app has that the skill must **never** call (names from the
app's own bundle, not probed): anything containing `Cart`, `Order`,
`Checkout`, `Payment`, `Favorite`, `Profile`, promo apply/redeem, and
`setDeliveryLocation` (the address is sent as a cookie instead, so nothing
is written server-side). `getSearchFeedV1` and `getSearchSuggestionsV1` are
excluded too — one is useless, the other challenged.

## 10. Gates, as answered

| Gate | Question | Answer |
|---|---|---|
| G1 | Do Python `requests` / `urllib` pass Cloudflare on `/_p/api/`? | **Yes** — both `200` JSON; transport order requests → curl → urllib |
| G2 | Where do coordinates come from? | `getDeliveryLocationV1` returns the full cookie object; `locate` = 2 requests; a coordinate-less cookie is silently wrong (2,263 mi) |
| G3 | Is the cookie needed for a menu? | No — full menu and prices without it; distance/ETA/range from a default point, so nulled without `--at` |
| G4 | Does the feed paginate? | `pageInfo: {offset, pageSize: 80}` → next 80 stores; `--pages N` |
| G5 | Do server-side `sortAndFilters` apply when sent back? | **open** — filters stay client-side |
| G6 | URL id ↔ UUID? | base64url of the 16 bytes, 404/404 |
| G7 | How much do deals move across a day? | **open** — only a 17-minute pair measured (§4.5) |
| G8 | Any unprotected keyword search? | No (§4.7) |
| G9 | Does a US address behave the same? | **open** — Canada-only claim |

## 11. Volatility

- **Never cache:** feed membership and order, deal badges, menus, prices,
  `isOpen`, sold-out state, ETAs. All of these moved or can move within
  minutes.
- **Stable for days:** an address's resolution (id + coordinates) — the one
  thing cached, for 30 days; store UUIDs and URL ids; `hours[]`.

## 12. UNKNOWN / not established

- Whether a datacenter egress IP (claude.ai's sandbox) is challenged on
  `/_p/api/` — every request left from a residential IP.
- The exact reCAPTCHA threshold (seen at roughly 110 requests in a day) and
  how long the block lasts (hours; not timed), or whether it is per IP only.
- G5, G7, G9 above.
- Whether `getSearchFeedV1` works with a search id from the (challenged)
  suggestions endpoint — untestable without defeating the challenge, which
  the skill will not do.
- The meaning of `purchaseInfo`, `itemPromotion`, `itemPromotionV2`,
  `itemAvailabilityState` beyond sold-out; `priceBucket` was always empty.
- A required option group whose cheapest option costs money (rule inferred).
- Nested `childCustomizationList` on a live item (synthetic fixture only).
- How grocery / convenience stores in the feed (Walmart, Michaels) behave
  under `menu` — they appear in `nearby`; `menu` should work but was not run
  on one.
- Whether `hours[].dayRange` ever carries a span with a different separator
  than the observed forms.

## 13. Evidence index (the design probe's `evidence/` directory)

| File | What |
|---|---|
| `requests.log` | every helper request: time, endpoint, status, bytes, seconds, capture name, body |
| `ue.py` | the probe helper: one `curl` POST with UA, csrf header and cookie, challenge detection, logging |
| `city.html` | the Cloudflare challenge page (`/ca/city/toronto-on`) |
| `getSearchSuggestionsV1.json`, `suggest.json` | the challenged suggestions endpoint, morning and afternoon |
| `mapsSearchV1.json`, `maps_kitchener.json` | `mapsSearchV1` candidates (uber_places and google_places) |
| `g2_deliveryloc.json` | `getDeliveryLocationV1` — the full cookie object with coordinates |
| `g2_reference_only.json`, `g3_nocookie.json` | store fetched with a coordinate-less cookie / no cookie |
| `feed.json.gz`, `feed2.json.gz` | two full feeds 17 minutes apart |
| `g4_feedSessionCount.json`, `g4_pageInfo.json` | feed page 1 and page 2 (`pageInfo` offset 112) |
| `search.json`, `search2.json`, `search_v3.json`, `search_v4.json` | the four ignored search attempts |
| `store.json`, `store_pickup.json` | Himalayan Kitchen, delivery and pickup |
| `store_pctoff.json` | Albert's — 25% off, fractional cents, strikethrough |
| `store_deal.json` | Tikka by LTH — BOGO items, triple-listed dish |
| `store_pizza.json` | Blondies Pizza — options |
| `item_pizza.json`, `item.json` | `getMenuItemV1` — eight groups; one free group |
| `api.json`, `recheck.json` | `status: failure` / `invalid_store_uuid` inside a 200 |
| `burst.json` | last body of the 16-request burst |

Trimmed copies of the ones the self-check needs live in `scripts/fixtures/`
under the test plan's names (`feed_union_station.json.gz`,
`store_alberts_pctoff.json.gz`, `item_blondies_pizza.json`, …).
