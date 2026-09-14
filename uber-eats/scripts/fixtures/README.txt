Fixtures for test_ubereats.py — real Uber Eats web-API responses, captured
2026-09-13 from Toronto (delivery address: Toronto Union Station, 65 Front St W)
and copied ONCE from uber-eats-design/evidence/ by the trimming script kept
in the build session's scratch directory. Total ~150 KB. Every JSON file keeps
the {"status", "data"} envelope; .gz files are gzip of the same JSON.

Trim rule (design 04 §1): keep the JSON valid and every field the parsers
read; drop image URLs, analytics/tracking blobs and UI-only subtrees.

feed_union_station.json.gz        getFeedV1, the later of two captures (feed2.json.gz).
                                  Dropped: feedHeader, timeWindowPicker. feedItems cut
                                  from 129 to 40: 30 REGULAR_STORE + 4 REGULAR_CAROUSEL
                                  (≤ 9 stores each) chosen to keep Tim Hortons in both a
                                  carousel AND the list (dedupe pair), Himalayan, Albert's,
                                  Tikka, Blondies, every $0-delivery string, ≥ 5 strings of
                                  every deal grammar family, the one FARE badge, unrated
                                  ("New") stores and a scheduled store ("2:15PM" badge);
                                  plus ANNOUNCEMENT, SECTION_HEADER, 2 DIVIDER, the
                                  FEATURED_STORES item (3 stores, images dropped) and the
                                  CATALOG_ITEMS_CAROUSEL_PAYLOAD item (header only).
                                  Per store kept: storeUuid, title, meta, rating, actionUrl,
                                  favorite, signposts, mapMarker{latitude, longitude,
                                  secondaryMarkerContent, zIndex}, and of `tracking` only
                                  metaInfo.additionalTrackingData.promotionType and
                                  storePayload.etdInfo.dropoffETARange.
                                  Census: 69 store refs (incl. 3 featured), 57 distinct
                                  storeUuids → 12 duplicates. Deal strings: bogo 40,
                                  percent 22, dollar 16, free_delivery 5, other 12.
feed_union_station_earlier.json.gz  getFeedV1 17 minutes earlier (feed.json.gz), same
                                  trim. 69 refs, 63 distinct. Membership and order differ.
feed_page2.json                   getFeedV1 with pageInfo {offset 112, pageSize 80}
                                  (g4_pageInfo.json): first 8 REGULAR_STORE items + meta
                                  {offset 192, hasMore true}; no isInServiceArea key.
store_himalayan_delivery.json.gz  getStoreV1 Himalayan Kitchen & Bar, DELIVERY (store.json):
                                  109 catalog entries, 98 distinct dishes, no rating block,
                                  Samosa Chaat carries "Earn $7 Uber Cash for photo".
store_himalayan_pickup.json.gz    Same store, PICKUP (store_pickup.json): 98 entries, ETA
                                  18–28 Min, prices identical to delivery.
store_alberts_pctoff.json.gz      Albert's Real Jamaican Foods (store_pctoff.json): 94
                                  entries, 71 dishes; 7 dishes 25% off with fractional
                                  cents (1087.5, 1012.5, 1181.25) and a line-through was;
                                  "Save on Select Items" block with promoUUID;
                                  hasStorePromotion true with promotion null; Monday with
                                  no hours; Friday–Saturday ending at 00:30.
store_tikka_bogo.json.gz          Tikka by LTH (store_deal.json): 23 entries, 13 dishes;
                                  two bowls listed three times each, BOGO badge on some
                                  copies and itemPromotion.buyXGetYItemPromotion on all.
store_blondies.json.gz            Blondies Pizza (Bay) (store_pizza.json): 55 entries, 34
                                  dishes; "Every Day" hours.
  Store trim: top-level keys kept when the parser reads them or they are ≤ 400 bytes
  (dropped: timeWindowPicker, exploreMoreStores, metaJson, storeReviews,
  featuredReviews, adaptedDeliveryHoursInfos, heroImageUrls, featuredItemsSections,
  sectionEntitiesMap, subsectionsMap, seoMeta, storeBanners …). Per catalog item
  dropped: imageUrl, itemThumbnailElements, imageOverlayElements,
  itemDescriptionBadge. promoInfo, priceTagline, itemPromotion and
  catalogItemAnalyticsData are whole.
item_blondies_pizza.json          getMenuItemV1, Blondies Custom Pizza 16" Large
                                  (item_pizza.json), whole: 8 option groups.
item_himalayan_chili.json         getMenuItemV1, Honey Chicken Chili (item.json), whole:
                                  one free 0–1 group.
maps_union_station.json           mapsSearchV1 "Union Station Toronto", whole: 5
                                  candidates, uber_places and google_places.
deliveryloc_union_station.json    getDeliveryLocationV1 on the first candidate
                                  (g2_deliveryloc.json), whole: the cookie object with
                                  coordinates.
invalid_store.json                getStoreV1 with a bad id (api.json), whole: HTTP 200,
                                  {"status":"failure","data":{"message":"invalid_store_uuid"}}.
cloudflare_challenge.html         The 403 challenge page (city.html), whole: "Just a
                                  moment…" title and _cf_chl_opt.
search_ignored.json.gz            getSearchFeedV1 userQuery "pizza" (search_v3.json),
                                  trimmed to 3 stores + meta. Kept only to document that
                                  search returns the generic feed and is NOT used by the
                                  skill; a test asserts no code path calls it.

Synthetic fixtures are built inside the test (functions named synthetic_*):
a closed store with hours, a two-amounts-no-strikethrough tagline, a required
paid option group with a sold-out cheapest option and a nested
childCustomizationList, isInServiceArea false, a feed with no deal badges.
botdefense_challenge.json         getFeedV1 after ~110 requests from one IP in a day (live,
                                  2026-09-13, dropped in by the lead), whole: HTTP 403 with
                                  {"status":"failure","metadata":{"botdefense":{"state":
                                  "challenge","provider":"RECAPTCHA",…}}} — Uber's own bot
                                  defense, distinct from the Cloudflare page; mapsSearchV1
                                  kept answering.
