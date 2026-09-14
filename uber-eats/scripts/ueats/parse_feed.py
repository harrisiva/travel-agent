"""getFeedV1 → StoreRow list — Lane B (design 01 §4).

stores(): walk feedItems: type REGULAR_STORE → .store; REGULAR_CAROUSEL →
  .carousel.stores[]; FEATURED_STORES likewise if it carries stores. Dedupe by
  storeUuid keeping first occurrence and feed order. Per row: title.text,
  rating.text → float (else None), count text from rating.accessibilityText,
  ETA min/max from the ETD badge's accessibilityText "Delivered in 10 to 20
  min", EXCLUSIVE_STORE badge → exclusive, badgeData.fare.deliveryFee → text,
  deals from signposts[].text ∪ mapMarker.secondaryMarkerContent.text (strip,
  dedupe per store, parse_deal), url_id from actionUrl, coordinates from
  mapMarker, distance_km haversine to `origin` (None without one; round 0.1).
  FeedMeta from currencyCode, isInServiceArea, meta.offset/hasMore.
  Missing catalog keys → PayloadError from ueats.http; empty storesMap is fine.

parse_deal(): the grammar in 01 §4.2 — never drops unknown text.

Observed drift the captures forced (feed2.json.gz, g4_pageInfo.json):
  * a scheduled store's ETD badge reads "3:00PM" in both text and
    accessibilityText — no range; fall back to
    tracking.storePayload.etdInfo.dropoffETARange {min,max}, else None/None
  * FEATURED_STORES stores have no mapMarker and often no rating → None
  * page 2 (pageInfo) carries no isInServiceArea → treated as True (Uber only
    says false when it means it); feedItems missing → PayloadError
  * `feed` may be the whole {"status","data"} envelope or just `data`
"""
from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from .http import PayloadError
from .model import Deal, FeedMeta, Location, Money, StoreRow, fold

PAGE_SIZE = 80

_ETA_RE = re.compile(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*min", re.I)
_COUNT_RE = re.compile(r"based on (more than )?([\d,]+)\+?\s*(?:reviews?|ratings?)", re.I)
_URL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

_NUM = r"(\d+(?:\.\d+)?)"
_BOGO_RE = re.compile(r"^buy\s+(\d+),?\s*get\s+(\d+)(\s+free)?$", re.I)
_PERCENT_RE = re.compile(rf"^{_NUM}%\s*off(\s+select\s+items)?(?:\s+\${_NUM}\+)?$", re.I)
_DOLLAR_RE = re.compile(rf"^\${_NUM}\s*off(\s+select\s+items)?(?:\s+\${_NUM}\+)?$", re.I)
_FREE_DELIVERY_RE = re.compile(rf"^\$0(?:\.00)?\s*delivery\s*fee(?:\s+on\s+\${_NUM}\+)?$", re.I)


def _unwrap(feed: Any) -> dict:
    if not isinstance(feed, dict):
        raise PayloadError("getFeedV1: body is not a JSON object")
    if "feedItems" not in feed and isinstance(feed.get("data"), dict):
        feed = feed["data"]
    if not isinstance(feed.get("feedItems"), list):
        raise PayloadError("getFeedV1: data.feedItems missing or not a list")
    return feed


def _iter_raw_stores(items: list):
    """Yield raw store dicts in feed order, from every item type that carries them."""
    for fi in items:
        if not isinstance(fi, dict):
            continue
        kind = fi.get("type")
        if kind == "REGULAR_STORE":
            s = fi.get("store")
            if isinstance(s, dict):
                yield s
        elif kind == "REGULAR_CAROUSEL":
            car = fi.get("carousel")
            if isinstance(car, dict):
                for s in car.get("stores") or []:
                    if isinstance(s, dict):
                        yield s
        elif kind == "FEATURED_STORES":
            payload = fi.get("payload")
            if isinstance(payload, dict):
                for s in payload.get("stores") or []:
                    if isinstance(s, dict):
                        yield s


def _text(obj: Any) -> str | None:
    """`{"text": "…"}` → stripped text; anything else → None."""
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str) and t.strip():
            return t.strip()
    elif isinstance(obj, str) and obj.strip():
        return obj.strip()
    return None


def _rating(raw: Any) -> tuple[float | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    value: float | None = None
    t = raw.get("text")
    if isinstance(t, str):
        try:
            value = float(t.strip())
        except ValueError:
            value = None
    elif isinstance(t, (int, float)) and not isinstance(t, bool):
        value = float(t)
    count: str | None = None
    acc = raw.get("accessibilityText")
    if isinstance(acc, str):
        m = _COUNT_RE.search(acc)
        if m:
            count = m.group(2).replace(",", "") + ("+" if m.group(1) else "")
    return value, count


def _eta(store: dict) -> tuple[int | None, int | None]:
    for badge in store.get("meta") or []:
        if not isinstance(badge, dict) or badge.get("badgeType") != "ETD":
            continue
        for key in ("accessibilityText", "text"):
            t = badge.get(key)
            if isinstance(t, str):
                m = _ETA_RE.search(t)
                if m:
                    return int(m.group(1)), int(m.group(2))
    # Scheduled stores show a clock time ("3:00PM") — use the tracking range
    # (FEATURED_STORES copies keep it under `trackingCode` instead).
    for key in ("tracking", "trackingCode"):
        try:
            rng = store[key]["storePayload"]["etdInfo"]["dropoffETARange"]
            lo, hi = rng.get("min"), rng.get("max")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) \
                    and not isinstance(lo, bool) and not isinstance(hi, bool):
                return int(lo), int(hi)
        except (KeyError, TypeError, AttributeError):
            pass
    return None, None


def _badges(store: dict) -> tuple[bool, str | None]:
    exclusive = False
    fee: str | None = None
    for badge in store.get("meta") or []:
        if not isinstance(badge, dict):
            continue
        if badge.get("badgeType") == "EXCLUSIVE_STORE":
            exclusive = True
        bd = badge.get("badgeData")
        if isinstance(bd, dict) and isinstance(bd.get("fare"), dict) and fee is None:
            f = bd["fare"].get("deliveryFee")
            if isinstance(f, str) and f.strip():
                fee = f.strip()
    return exclusive, fee


def _raw_promotion_type(store: dict) -> str | None:
    for key in ("tracking", "trackingCode"):
        try:
            t = store[key]["metaInfo"]["additionalTrackingData"]["promotionType"]
        except (KeyError, TypeError):
            continue
        if isinstance(t, str) and t:
            return t
    return None


def _deals(store: dict) -> tuple[Deal, ...]:
    texts: list[str] = []
    for sp in store.get("signposts") or []:
        t = _text(sp)
        if t and fold(t) != "new":
            # "New" is the NEW_STORE marker, not a deal.
            texts.append(t)
    mm = store.get("mapMarker")
    if isinstance(mm, dict):
        t = _text(mm.get("secondaryMarkerContent"))
        if t and fold(t) != "new":
            # The map marker truncates the signpost ("$11 off $40+" → "$11 off"):
            # a proper prefix of a signpost text is the same deal, not a second one.
            ft = fold(t)
            if not any(fold(s) == ft or fold(s).startswith(ft + " ") for s in texts):
                texts.append(t)
    seen: set[str] = set()
    distinct: list[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            distinct.append(t)
    # Uber's typed value describes one promotion; it can only be attached
    # unambiguously when exactly one deal remains on the store.
    raw_type = _raw_promotion_type(store) if len(distinct) == 1 else None
    return tuple(parse_deal(t, raw_type) for t in distinct)


def _url_id(action_url: Any) -> str | None:
    if not isinstance(action_url, str) or not action_url:
        return None
    path = action_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    seg = path.rsplit("/", 1)[-1]
    return seg if _URL_ID_RE.match(seg) else None


def _coords(store: dict) -> tuple[float | None, float | None]:
    mm = store.get("mapMarker")
    if not isinstance(mm, dict):
        return None, None
    lat, lng = mm.get("latitude"), mm.get("longitude")
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng))
    return (float(lat), float(lng)) if ok else (None, None)


def _row(store: dict, origin: Location | None) -> StoreRow:
    uuid = store.get("storeUuid")
    name = _text(store.get("title"))
    if not isinstance(uuid, str) or not uuid or not name:
        raise PayloadError("getFeedV1: a feed store lacks storeUuid or title.text")
    rating, count = _rating(store.get("rating"))
    eta_min, eta_max = _eta(store)
    exclusive, fee = _badges(store)
    lat, lng = _coords(store)
    distance: float | None = None
    if origin is not None and origin.latitude is not None and origin.longitude is not None \
            and lat is not None and lng is not None:
        distance = round(haversine_km(origin.latitude, origin.longitude, lat, lng), 1)
    return StoreRow(
        uuid=uuid, name=name, rating=rating, rating_count_text=count,
        eta_min=eta_min, eta_max=eta_max, distance_km=distance, deals=_deals(store),
        exclusive=exclusive, delivery_fee_text=fee, url_id=_url_id(store.get("actionUrl")),
        latitude=lat, longitude=lng,
    )


def _merge_rows(kept: StoreRow, later: StoreRow) -> StoreRow:
    """First-seen row keeps its position; a later copy fills in what it lacked.

    FEATURED_STORES lists sparse copies (no rating, no mapMarker, "New" for a
    signpost) ahead of the full REGULAR_STORE entry for the same uuid."""
    from dataclasses import replace
    changes: dict[str, Any] = {}
    for f in ("rating", "rating_count_text", "delivery_fee_text", "url_id"):
        if getattr(kept, f) is None and getattr(later, f) is not None:
            changes[f] = getattr(later, f)
    if kept.eta_min is None and kept.eta_max is None and later.eta_min is not None:
        changes["eta_min"], changes["eta_max"] = later.eta_min, later.eta_max
    if kept.latitude is None and kept.longitude is None and later.latitude is not None:
        changes["latitude"], changes["longitude"] = later.latitude, later.longitude
        changes["distance_km"] = later.distance_km
    if not kept.deals and later.deals:
        changes["deals"] = later.deals
    if later.exclusive and not kept.exclusive:
        changes["exclusive"] = True
    return replace(kept, **changes) if changes else kept


def stores(feed: dict, origin: Location | None) -> tuple[list[StoreRow], FeedMeta]:
    data = _unwrap(feed)
    rows: list[StoreRow] = []
    index: dict[str, int] = {}
    for raw in _iter_raw_stores(data["feedItems"]):
        row = _row(raw, origin)
        if row.uuid in index:
            i = index[row.uuid]
            rows[i] = _merge_rows(rows[i], row)
            continue
        index[row.uuid] = len(rows)
        rows.append(row)

    currency = data.get("currencyCode")
    currency = currency if isinstance(currency, str) and currency else None
    in_area = data.get("isInServiceArea")
    in_area = True if in_area is None else bool(in_area)
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    offset = meta.get("offset")
    offset = int(offset) if isinstance(offset, (int, float)) and not isinstance(offset, bool) else None
    has_more = meta.get("hasMore")
    has_more = bool(has_more) if isinstance(has_more, bool) else None
    return rows, FeedMeta(currency=currency, in_service_area=in_area, stores_returned=len(rows),
                          offset=offset, has_more=has_more)


def _money_str(num: str) -> str:
    """Currency units ("70") → "70.00", rounded exactly as Money.amount rounds."""
    cents = Decimal(num) * 100
    return Money(int(cents) if cents == cents.to_integral_value() else float(cents), "").amount


def parse_deal(text: str, raw_type: str | None = None) -> Deal:
    original = (text or "").strip()
    t = " ".join(original.split())
    m = _BOGO_RE.match(t)
    if m:
        return Deal(text=original, type="bogo", raw_type=raw_type)
    m = _PERCENT_RE.match(t)
    if m:
        return Deal(text=original, type="percent", percent=int(Decimal(m.group(1))),
                    min_spend=_money_str(m.group(3)) if m.group(3) else None,
                    select_items=bool(m.group(2)), raw_type=raw_type)
    m = _DOLLAR_RE.match(t)
    if m:
        return Deal(text=original, type="dollar", amount=_money_str(m.group(1)),
                    min_spend=_money_str(m.group(3)) if m.group(3) else None,
                    select_items=bool(m.group(2)), raw_type=raw_type)
    m = _FREE_DELIVERY_RE.match(t)
    if m:
        return Deal(text=original, type="free_delivery",
                    min_spend=_money_str(m.group(1)) if m.group(1) else None, raw_type=raw_type)
    return Deal(text=original, type="other", raw_type=raw_type)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
