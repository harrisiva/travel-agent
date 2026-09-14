# Enterprise — endpoint notes

**What this is:** the API reference behind this skill — how the endpoints
behave, and every trap found while building against them. Written during a
read-only investigation beginning 2026-09-08 and kept current since. No
reservation was created at any point; every call described is a lookup or a
quote.

**Who it is for:** anyone maintaining or extending the skill, and Claude when a
call behaves unexpectedly and the reason is not obvious from `SKILL.md`. It is
not needed to *use* the skill — `SKILL.md` is the interface contract and is
always in context; this file is loaded only when the underlying API is the
question.

Everything marked **verified** was executed against production and returned the
described data. Everything marked **unverified** is inference and needs checking
before it goes anywhere near a user. Where an earlier reading turned out to be
wrong, the correction is kept alongside it rather than quietly edited out — the
wrong version is usually the more instructive one.

---

## The two hosts that matter

| Host | Role | Auth | WAF / CDN |
|---|---|---|---|
| `prd.location.enterprise.com/enterprise-sls` | name → location ID, hours, renter age | none | **Imperva** (`x-cdn: Imperva`) |
| `prd-east.webapi.enterprise.ca/enterprise-ewt` | availability + live pricing, branch detail | none | **Imperva**, nginx behind it |
| `prd-east.webapi.enterprise.com/enterprise-ewt` | same, `.com` twin | none | Imperva |
| `www.enterprise.ca` / `.com` | the HTML site | — | **Akamai** (`server: AkamaiGHost`) |

**Corrected:** an earlier draft called the API hosts "nginx direct" and blamed
Akamai for the API blocks. Both wrong. Measured response headers:

```
prd-east.webapi.enterprise.ca -> server: nginx, x-cdn: Imperva,
                                 set-cookie: visid_incap_… / incap_ses_…
prd.location.enterprise.com   -> x-cdn: Imperva
www.enterprise.ca             -> server: AkamaiGHost  (403s plain curl)
```

Two different vendors on two different tiers. The Akamai attribution came from
the *website's* block page and was carried across to the APIs by mistake. It
matters because the block pages differ: Imperva serves an **Incapsula incident
id**, Akamai a `Reference #`. Any code or documentation matching on vendor
wording would be matching the wrong vendor — which is why the tool detects a
block structurally (non-200 plus an HTML body) and never by vendor string.

`prd-west.webapi.enterprise.com` and `prd.webapi.enterprise.com` also answer 200.
**Verified: the `.ca` and `.com` hosts are interchangeable** — both returned
byte-identical results for both a Canadian and a US location. There is no
host-routing logic to write; pick one and keep it.

The website is protected; the APIs behind it are not. There is no API key, no
login, and no token anywhere in the flow — which is what makes this skill-shaped
under the repo's "no keys, no config" rule.

---

## 1. Location search — `enterprise-sls` (verified)

```
GET https://prd.location.enterprise.com/enterprise-sls/search/location/enterprise/web/text/{query}
      ?countryCode=CA&includeExotics=true&brand=ENTERPRISE&dto=true&cor=CA&locale=en_CA
```

Only `Accept: application/json` is needed. Works from plain `curl` with no
cookies, no referer, no TLS tricks.

Response is bucketed, **not** a flat list — each key is a separate array:
`airports`, `branches`, `cities`, `railStations`, `portsOfCall`, `trucks`,
`countries`, `exotics`. A naive `[0]` on the wrong bucket silently picks the
wrong branch.

Per entry: `id` (the integer the quote endpoint wants), `name`, `airport_code`,
`gps`, `address`, `phones`, `hours`, `currency_code`, `station_id`,
`business_types`, and `additional_data.age_options`.

Worth knowing: a query like `Halifax` returns **two** YHZ airports — the regular
branch (`1019286`) and `Halifax Airport Exotic` (`1054600`). Picking blindly gets
you the exotic fleet and wildly different prices.

**Resolving a bare numeric id** needs a different host - the text search
cannot look an id up:

```
GET https://prd-east.webapi.enterprise.ca/enterprise-ewt/location/{id}?type=both
```

The branch sits under `location`, with `address.country_code`, `name`,
`airport_code` and `currency_code`. This matters more than it looks: without
it, a tool that accepts numeric ids has to *guess* the country, and guessing
wrong silently disables cross-border detection - the exact false-negative this
document exists to prevent.

Supporting calls on the same host, both verified:

```
GET .../search/location/enterprise/web/hours/{id}?from=YYYY-MM-DD&cor=CA&locale=en_CA
GET .../search/location/enterprise/web/renterage/{id}?cor=CA&locale=en_CA
```

`brand=NATIONAL` and `brand=ALAMO` are accepted here and return results.

`countryCode`, `cor` and `locale` are fully parameterised — **verified** with
`US`/`en_US` (Denver) and `GB`/`en_GB` (Heathrow). Each entry carries its own
`currency_code`, so the local billing currency comes back with the location and
never has to be guessed.

---

## 2. Availability + pricing — `reservations/initiate` (verified)

This is the whole product. One POST returns the entire fleet with live prices.

```
POST https://prd-east.webapi.enterprise.ca/enterprise-ewt/reservations/initiate
Content-Type: application/json
Origin:  https://www.enterprise.ca
Referer: https://www.enterprise.ca/
brand: ENTERPRISE
locale: en_CA
channel: WEB
```

Body — the location objects come straight from endpoint 1:

```jsonc
{
  "pickup_time": "2026-10-15T10:00",     // no seconds, no timezone
  "return_time": "2026-10-18T10:00",
  "pickup_location":    { /* location object from search */ },
  "pickup_location_id": "1019286",
  "return_location":    { /* same shape; differ for one-way */ },
  "return_location_id": "1019286",
  "renter_age": 25,                      // see age table below
  "country_of_residence_code": "CA",     // renter residency, not the branch
  "view_currency_code": "CAD",           // DISPLAY only - see section 3
  "enable_north_american_prepay_rates": false,
  "applied_vehicle_class_filters": [],
  "check_if_no_vehicles_available": true,
  "check_if_oneway_allowed": true
}
```

Response is ~700 KB. The payload lives at:

```
session.gbo.reservation.car_classes        // the fleet, ~57-59 entries
session.gbo.reservation.car_classes_filters // the facet catalogue
session.gbo.reservation.policies
session.gma.reservation.pickup_location_with_detail
```

### What each `car_class` gives you

| Field | Offers |
|---|---|
| `code` | class code — `SFDR`, `MVAR`, `FJAR` |
| `make_model_or_similar_text` | the actual car — "GMC Terrain", "Jeep Wrangler Unlimited or Ford Bronco" |
| `category` / `sub_category` | Cars / SUVs / Vans / Trucks, then Compact SUV, Minivan, Jeeps … |
| `people_capacity`, `luggage_capacity` | seats, plus `small_`/`large_luggage_capacity` |
| `filters.DRIVE` | 2WD vs "4 Wheel Drive or All Wheel Drive" |
| `filters.FUEL` | Gasoline / Hybrid / Electric |
| `filters.TRANSMISSION` | Automatic |
| `mileage_info.unlimited_mileage` | boolean — the one that matters for road trips |
| `fuel_consumption`, `fuel_efficiency` | L/100km and mpg |
| `charges.PAYLATER.total_price_view` | trip total, with currency |
| `charges.PAYLATER.rates[...]` | rate lines — **NOT reliably daily**, see below |
| `charges.REDEMPTION` | Enterprise Plus points price, alongside cash |
| `status` | `AVAILABLE_AT_RETAIL_RATE` / `SOLD_OUT` / `RESTRICTED_AT_RETAIL_RATE` |
| `images` | templated PNG URLs with `{width}`/`{quality}` placeholders |
| `guaranteed_vehicle` | whether the specific model is guaranteed vs "or similar" |

Sample verified run — Halifax YHZ, 2026-10-15 → 2026-10-18, age 25:

```
59 classes returned, 20 bookable
PPAR  Full Size Pickup      2WD  108.30/day   436.00  Ram 1500
CFDR  Compact SUV           4WD  158.01/day   635.48  Volkswagen Taos
PFDR  Premium & Luxury SUV  4WD  168.82/day   678.72  Chevrolet Suburban (8 seats, 7 bags)
FJAR  Jeeps                 4WD  242.10/day   971.85  Jeep Wrangler Unlimited or Ford Bronco
```

### Renter age materially changes the answer (verified)

Same location, same dates:

| `renter_age` | Bookable | Cheapest |
|---|---|---|
| 25 | 20 | 436.00 CAD |
| 21 | 13 | 516.01 CAD |
| 19 | — | refused, `PRICING_4463` |

Under-25 is both a surcharge and a fleet restriction, so `--age` belongs on every
pricing command with a default of 25. Age 19 returns a structured message and
**no `car_classes` key at all** — see failure modes.

### One-way works (verified)

Pass a different `return_location` / `return_location_id`. `YHZ → YQM` returned
59 classes, 8 bookable, cheapest 447.01 CAD. So `--dropoff` is a real flag.

---

## 3. Geography and currency — global, not Canada-only

This was the biggest wrong assumption in the first pass. The API is worldwide,
and the country/currency knobs must be **open flags in the CLI**, not constants.

Verified quotes, all through the same `.ca` host, in the branch's own currency:

| Location | `cor` / `locale` | Bookable | Cheapest |
|---|---|---|---|
| Halifax `YHZ` | `CA` / `en_CA` | 23 of 59 | 761.08 CAD |
| Denver `DEN` | `US` / `en_US` | 52 of 74 | 521.06 USD |
| London Heathrow `LHR` | `GB` / `en_GB` | 23 of 64 | 168.44 GBP |
| Frankfurt `FRA` | `DE` / `de_DE` | 45 | 199.40 EUR |
| Paris `CDG` | `FR` / `fr_FR` | — | 221.60 EUR |
| Madrid `MAD` | `ES` / `es_ES` | 53 of 67 | 210.72 EUR |
| Dublin `DUB` | `IE` | 9 of 50 | 200.32 EUR |
| Auckland `AKL` | `NZ` | — | 330.60 NZD |
| Tokyo Haneda `HND` | `JP` / `ja_JP` | — | 78,320.00 JPY |

Nine countries on four continents, each priced in its local currency. Coverage
is genuinely worldwide, not North America plus the UK.

### Labels are translated; codes are not

This is the important consequence, and it is a silent-wrongness trap. At a
German branch the API returns `Mietwagen` for Cars, `Kleinbusse` for Vans,
`Vierrad- oder Allradantrieb` for all-wheel drive and `Benzinfahrzeug` for
petrol. Any filter matching those **descriptions** returns nothing outside
English while the matching vehicles sit right there in the response — measured
at Frankfurt: `--class car` matched 0 of 27 cars, `--class van` 0 of 9 vans,
`--drive awd` 0 of 1.

The numeric facet codes (`100` Cars, `200` Trucks, `300` SUVs, `500` Vans;
`59` AWD, `112` 2WD; `102`/`168`/`169`/`170` fuel) are identical in every
locale. Match on codes, display the translated text.

### A coverage gap that reads as a wrong answer

Enterprise's catalogue has **no Australian airport branches**. Searching
`Sydney Airport` with `countryCode=AU` returns `1036511 YQY Sydney Airport,
Reserve Mines, CAD` — Sydney, **Nova Scotia**. That is not an error and
nothing in the response flags it; a caller that takes the first result quotes a
Canadian branch for an Australian trip. Always check the returned country
before quoting, which is why `locations` prints it.

Three separate knobs, which are easy to conflate:

| Field | Meaning | CLI flag |
|---|---|---|
| `countryCode` (search) | which country's branches to search | `--country` |
| `country_of_residence_code` | **renter's** residency — drives eligibility and terms | `--residency` |
| `view_currency_code` | display currency only, see below | `--currency` |

`country_of_residence_code` is not cosmetic but it is *not* a price lever either:
quoting Halifax as a `GB` resident returned the same 436.00 CAD. It affects
eligibility, cross-border and one-way rules. Treat it as a correctness input,
not a discount hunt.

### The currency trap — `view` vs `payment`

`view_currency_code` performs a **display conversion**. It does not change what
the renter is charged. Verified, Halifax quoted with `view_currency_code=USD`:

```
total_price_view    : 315.10 USD   <- converted estimate
total_price_payment : 436.00 CAD   <- what actually gets charged
```

Every charge object carries both, and the same split exists on the per-day
`unit_amount_view` / `unit_amount_payment`. Reporting `_view` as "the price"
would tell someone a Halifax rental costs $315 when their card is billed
CAD 436. The CLI should lead with `total_price_payment` and show `_view` only as
an explicitly-labelled conversion.

---

## 4. Performance and concurrency (verified)

| Measure | Value |
|---|---|
| Response size | ~717 KB per quote |
| Single call, warm | ~2.8 s |
| 6 calls at `max_workers=6` | 3.4 s total, all 200 |

Concurrency is close to free — six parallel quotes cost barely more than one
sequential. A `sweep` should use a small thread pool (6 is proven; do not go
higher without re-probing) rather than a serial loop, but must still refuse up
front when the plan exceeds `--max-requests`.

### Rate limiting: what is measured, and what is not

Measured, at 10-second spacing (8 calls): every one HTTP 200, latency flat at
1.75-2.41 s with no upward drift, zero 429s. Six concurrent: all 200, 2.9-3.6 s.
**No `Retry-After`, no `RateLimit-*`, no `X-RateLimit-*`, no throttle header of
any kind in any response.** So there is no advance warning to read — a client
cannot know it is approaching the limit.

Watch for a false positive here: one paced call returned 200 in 0.50 s with a
317-byte `PRICING_16007` body. That is a real Canadian-Thanksgiving sell-out,
not a throttle — but a fast, tiny 200 is exactly the shape a throttle might
take, so never teach the tool to read it as one.

**Unmeasured:** the exact threshold, the cooldown, and what a block response
actually contains. No 403 from the API host was ever captured, so its status,
headers and body shape are inference. Expect an Imperva page with an incident
id and probably no `Retry-After`, but treat that as unverified.

### There IS a volumetric limit (corrected)

An earlier draft of this file said "no throttling observed". That was wrong —
it only reflected a light workload. Under sustained testing (roughly 100+
pricing calls from one IP across parallel workers), `reservations/initiate`
began rejecting **every** transport, including `curl`, which had been working
all session on an unchanged TLS stack.

Two things follow, and both matter for how the tool reports failure:

1. **A block is not proof of TLS fingerprinting.** The two are indistinguishable
   from the client: both are a 403 with an HTML body. If a transport worked and
   then stopped on an unchanged stack, it is volume, not fingerprint. Any error
   message asserting "this is TLS fingerprinting" is stating a guess as fact.
2. **It clears on its own — confirmed.** The block lifted without any
   intervention after roughly 20-40 minutes, and a full live self-check then
   passed 74/74 on the *primary* transport (`chrome-tls`), with no fallback
   needed. Back off and retry rather than hunting OpenSSL builds.

**Per-host or per-IP? Unknown — do not assume.** All four pricing hosts
(`prd-east`/`prd-west`/`prd` on `.ca` and `.com`) answered 200 when retested,
*including the one that had been blocked*. That only shows the block had
already lifted; it does not show the block was ever scoped to one host. The two
cases are indistinguishable from the evidence gathered.

**Do not rotate hosts to get around a block.** Even though the hosts are
interchangeable for a normal query, using a second one to keep issuing requests
after the first refused you is evading a rate limit, not failing over. The
correct client behaviour is to slow down and retry the same host. The tool
deliberately talks to one pricing host only.

The block was observed on `reservations/initiate` while the location service
and `enterprise-ewt/location/{id}` kept answering. **Do not rely on that.**
`location/{id}` sits on the *same host and the same Imperva instance* as
pricing, so it only survives a block if the rule is path-scoped, which is
unconfirmed. Branch resolution depending on it is a real risk: if the rule is
ever host-scoped, id lookup fails at the same moment pricing does.

`prd.location.enterprise.com` is a genuinely different host, so the text search
is the more robust of the two lookups.

Note the memory shape: 28 quotes x 717 KB is ~20 MB of JSON if held. Extract the
handful of fields per class and discard the parsed body immediately.

### Cross-border one-way is refused — ambiguously (verified)

`YYZ → DEN` returns HTTP 200 with no `car_classes` and this message:

```json
{"code": "PRICING_16007",
 "message": "We're sorry, but this location has no vehicles available during the
             selected dates and/or times...",
 "priority": "ERROR"}
```

The text says *no vehicles available*, but the real cause is almost certainly
that the route is not permitted — same-country one-way (`YHZ → YQM`) works fine
on the same dates. **The API cannot distinguish "sold out" from "route not
allowed" here**, which is precisely the repo's "an API that answers nothing when
it means bad request" trap.

Handling: never flatten `PRICING_16007` into a bare "nothing available". Surface
the code and the message text verbatim, and when the request was a cross-border
one-way, say explicitly that the route may not be permitted rather than implying
the dates were unlucky.

---

## 5. Filtering — do it client-side

`car_classes_filters` returns the facet catalogue the site's own dropdown uses:

| `filter_code` | Values |
|---|---|
| `CLASS` | 100, 200, 300, 400, 500 — **meaning is market-specific**, see below |
| `FUEL` | 168 Hybrid, 169 Gasoline, 170 Electric |
| `DRIVE` | 112 2WD, 59 4WD/AWD |
| `PASSENGERS` | 4, 5, 6, 7, 8, 10 |
| `TRANSMISSION` | 25 Automatic |

### Category codes: stable set, market-specific meaning (corrected)

An earlier draft said `500` meant Vans "identical in every locale". Wrong, and
the error came from sampling only Canada:

| Market | 100 | 200 | 300 | 400 | 500 |
|---|---|---|---|---|---|
| Frankfurt `de_DE` | Mietwagen | — | SUVs | **Kleinbusse** | **Transporter** |
| Madrid `es_ES` | Coches | — | SUVs | **Monovolúmenes** | **Furgonetas** |
| Halifax `en_CA` | Cars | Trucks | SUVs | — | **Vans** |
| Tokyo `ja_JP` | Cars | — | SUVs | — | **Vans** |

In Europe a passenger people-carrier is **400** and 500 is a *cargo* van; in
Canada and Japan there is no 400 and 500 *is* the passenger van. A fixed
`van → 500` returns cargo vans in Germany and misses every minivan.

Three further traps in the same object, all measured:

- `category.weight` **varies between classes sharing a code** (`SUVs` seen with
  weight 200 and 100). Key on `category.code` only, never the whole object.
- `sub_category` can be `{}` — an empty dict, not absent, not null.
- `sub_category.name_en` is **not English** outside English markets (`"Großer
  Transporter"` at FRA). Never use it as an English fallback.
- Category *membership* is market-specific too: at FRA `Cabrio` sits under
  SUVs, and `7-Sitzer SUV` under Kleinbusse.

### Rate lines are not a daily rate (corrected)

An earlier draft called `rates[0].unit_amount_view` the "daily rate". It is not,
for any rental of five nights or more. Same branch and class, only duration
varying:

| Nights | `unit_rate_type` | qty | unit amount | total |
|---|---|---|---|---|
| 2 | DAILY | 2.0 | 62.01 | 124.02 EUR |
| 4 | DAILY | 4.0 | 44.89 | 179.56 EUR |
| 5 | **WEEKLY** | 1.0 | 244.97 | 244.97 EUR |
| 14 | WEEKLY | 2.0 | 295.71 | 591.42 EUR |
| 30 | **WEEKLY ×4 + EXTRA_DAILY ×2** | | 349.44 + 50.00 | 1497.76 EUR |

The DAILY→WEEKLY switch is between 4 and 5 nights and flips for every class in
the response at once. At 30 nights `rates[]` holds **two** lines, so `rates[0]`
is neither the daily rate nor the whole price. Types seen: `DAILY`, `WEEKLY`,
`EXTRA_DAILY`.

**Never reconstruct the total from the rate lines.** `total == Σ(unit × qty)`
holds in EUR/JPY markets but fails on every Canadian class (tax sits outside the
rate lines: 2 × 108.43 = 216.86 against a 291.02 CAD total). Always read
`total_price_payment`.

### `charges.REDEMPTION` is a per-day figure whose "total" is not a total

`total_price_payment` equals `rates[0].unit_amount_payment` in all 130
REDEMPTION objects checked — a daily rate wearing a total's name — and it
duplicates the top-level `redemption_points`. The response never states how many
days a redemption covers (`max_days` and both `*_max_redemption_days` are 0), so
**a trip-level points cost is not derivable and any cents-per-point figure would
be invented**. The REDEMPTION rate line also has no `unit_rate_type_quantity`,
so a helper written for PAYLATER will crash or return zero on it.

### `guaranteed_vehicle` was never true

False on **all 393 classes** across 12 responses spanning DE, ES, JP and CA, six
durations and two ages. A filter on it can only ever return an empty list.
Not sampled: US branches, exotic branches, NATIONAL/ALAMO.

**The request field `applied_vehicle_class_filters` does not filter server-side.**
Verified: it is accepted (as `List[str]`), echoed back in
`session.gma.reservation.initiate_request`, and the response still contains every
class. Passing objects instead of strings returns `400 bad JSON`.

This is good news — one request returns the whole fleet, so filtering is local
and free. Every class carries a pre-computed `filters` block to match against.
The "sleepable SUV" case from `find-rental-car` is one predicate:

```python
c["category"]["name"] == "SUVs"
    and "4 Wheel" in c["filters"]["DRIVE"]["filter_description"]
    and c["people_capacity"] >= 5
    and c["mileage_info"]["unlimited_mileage"]
```

---

## 6. The TLS trap — read before writing any client

`prd-east.webapi.enterprise.ca` fingerprints the TLS handshake. Verified results,
**identical headers and body in all three**:

| Client | Result |
|---|---|
| `curl` (LibreSSL 3.3.6, both h2 and `--http1.1`) | 200, full payload |
| `requests` with default cipher order | **403** — HTML block page, 6/6 deterministic |
| Playwright / real Chrome | CORS error — blocked preflight returns no `Access-Control-Allow-Origin` |

It is not headless detection and not HTTP/2 — `curl --http1.1` passes. It is
Python's default cipher **ordering**. Overriding it fixes the problem and keeps
the repo on `requests` with no new dependency (9/9 successes, including fresh
handshakes per call):

```python
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

CHROME = ("TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
          "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
          "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
          "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
          "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:"
          "AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA")

class ChromeTLS(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = create_urllib3_context(ciphers=CHROME)
        return super().init_poolmanager(*a, **kw)
```

Verified against curl 8.7.1 / LibreSSL 3.3.6 and Python's OpenSSL 1.1.1s on
macOS. **Unverified** on other OpenSSL builds — a container may negotiate
differently, so keep `curl` as a fallback rather than the primary path, matching
the layering `campsites` already uses.

---

## 7. Failure modes that produce a *wrong* answer, not an error

Per the repo's "refuse rather than return a false negative" rule, these are the
ones the self-check has to cover:

1. **403 is HTML, not an exception.** Default `requests` returns a 200-shaped
   failure path: status 403 with an HTML block page. Code that only catches
   `RequestException` reports "no cars available". Assert the body parses as JSON
   *and* `car_classes` is non-empty.
2. **`SOLD_OUT` classes are still in the list**, just with no `charges`. Counting
   `len(car_classes)` says 59 available when 20 are bookable. Filter on `status`
   and on the presence of a price.
3. **Charge key is `PAYLATER`, not `PAY_LATER`.** A wrong key yields `None`
   prices that look like "price unavailable" rather than a bug.
4. **Two YHZ entries** — regular and Exotic. Wrong pick, wrong fleet, wrong price.
5. **`DRIVE` is absent on some classes** (premium/luxury) rather than `2WD`. A
   `!= "4 Wheel Drive"` test wrongly includes them; test for presence first.
6. The `DRIVE` facet list contains a literal `{"code": "null", "description":
   "null"}` entry — string `"null"`, not JSON null. Skip it when building filters.
7. **`total_price_view` is a converted estimate, not the charge.** Reading it as
   the price understates a Halifax rental as $315 USD when CAD 436 is billed.
   Lead with `total_price_payment`.
8. **Age refusal returns no `car_classes` key**, with a `PRICING_4463` message.
   A `.get("car_classes", [])` reads that as "nothing available" and a watcher
   would poll a rental that can never be booked. Must map to exit code 2, not 1.

---

## 8. Not yet cracked

- **National / Alamo pricing.** The location API accepts `brand=NATIONAL` and
  `brand=ALAMO` and returns real branches, so `locations --brand` works. But
  their pricing hosts 404 on every path guessed (`prd-east.webapi.nationalcar.ca/national-ewt`,
  `.../nationalcar-ewt`, `prd-east.webapi.alamo.ca/alamo-ewt`, plus `.com`
  variants). Same devtools method against their own reserve pages would find the
  real host. **Unverified.**
- **Non-Latin locales and RTL.** Only `en_*` locales exercised.
- **Corporate rates.** The widget has a `cid` (Corporate Account Number) field;
  its effect on the payload is untested.
- **Rate limiting.** See the correction in section 4 - there *is* a volumetric
  limit, hit at roughly 100+ pricing calls from one IP. The exact threshold and
  cooldown were not measured. Whatever the skill does, it needs the `--max-requests` ceiling the
  repo mandates before any multi-date sweep.

---

## 9. Discovery method, for the next site

The endpoints are not in the HTML and not in the webpack chunk map — the site's
JS is split across ~126 hashed chunks and the reserve page's own bundles
intermittently 403 to `curl`. What worked: drive the real reservation flow in
Playwright and record every request. The endpoint, its headers, and its exact
body shape all fall out of one captured POST.

Note the ordering — the browser was needed only to *discover* the API. Replaying
it needs no browser at all, which is what makes this shippable as a CLI.
