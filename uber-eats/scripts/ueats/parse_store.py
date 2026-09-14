"""getStoreV1 → Store — Lane B (design 01 §5, every trap in §5.3).

store():
  * dishes from catalogSectionsMap[*][*].payload.standardItemsPayload.catalogItems,
    deduped by item uuid; section = the block title of the first occurrence
    whose title is not "Featured items"; entries_total / duplicates_removed
  * price = Money(item.price) — ALREADY the sale price when discounted
  * was = the amount inside the `text-decoration:line-through` span of
    priceTagline.textFormat; if two amounts but no line-through → was None,
    price_unclear True; parse the tagline's plain `text` only for the display
    check, never as the price
  * deal = parse_deal(first deal-looking text carried by the item: "25% off",
    "Buy 1, get 1 free" …); "Earn $N Uber Cash …" → note, not a deal
  * sold_out from isSoldOut; has_options from hasCustomizations
  * hours: hours[].dayRange → tuple(HoursSpan(startTime, endTime)); minutes
    since midnight
  * rating.ratingValue / reviewCount (string with "+"); etaRange.text;
    pickup ETA from modalityInfo.modalityOptions[diningMode=PICKUP].subtitle
  * distance from distanceBadge.text "5.3 km" (None when no location was sent —
    the caller tells you via `located=False`); within_range from
    isWithinDeliveryRange (None when not located)
  * store.deals = distinct dish deals in first-seen order
  * missing catalogSectionsMap or title → PayloadError; empty map → 0 dishes

Where the deal text actually lives on an item (store_pctoff.json,
store_deal.json): promoInfo.promoBadge.accessibilityText (14 of Albert's 19
"25% off" entries, 4 of Tikka's 6 BOGO entries — 2 bowls × 3 copies) or
itemThumbnailElements[].payload.tagsPayload.tags[].text (the rest). All 6
Tikka entries also carry a typed itemPromotion.buyXGetYItemPromotion; if an
entry ever carries that with no text at all, the text is synthesised
("Buy 1, get 1 free") and raw_type records it.
Duplicate entries of one uuid are merged: the first entry wins, later ones
fill in a deal/was/note it lacked (Tikka's triple-listed bowl carries the
badge on only some copies).
"""
from __future__ import annotations

import html
import re
from decimal import Decimal
from typing import Any

from .http import PayloadError
from .model import Deal, Dish, HoursSpan, Money, Store, fold
from .parse_feed import parse_deal

FEATURED_TITLE = "featured items"

_TAG_RE = re.compile(r"<[^>]+>")
# A span whose own attributes carry line-through, or a <s>/<del>/<strike> element.
_STRUCK_RE = re.compile(
    r"<span\b[^>]*line-through[^>]*>(.*?)</span>|<(?:s|del|strike)\b[^>]*>(.*?)</(?:s|del|strike)>",
    re.I | re.S)
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_DISTANCE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(km|mi|miles?|kilomet(?:er|re)s?)\b", re.I)
_NOTE_RE = re.compile(r"\buber\s+cash\b|\bearn\b", re.I)


def _unwrap(data: Any) -> dict:
    if not isinstance(data, dict):
        raise PayloadError("getStoreV1: body is not a JSON object")
    if "catalogSectionsMap" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    return data


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def money(cents: float | int, currency: str) -> Money:
    return Money(float(cents) if not float(cents).is_integer() else int(cents), currency)


def _cents_from_amount(amount: str) -> float | int:
    cents = Decimal(amount.replace(",", "")) * 100
    return int(cents) if cents == cents.to_integral_value() else float(cents)


def was_price(text_format: str | None, currency: str) -> tuple[Money | None, bool]:
    """(was, price_unclear) from priceTagline.textFormat."""
    if not isinstance(text_format, str) or not text_format.strip():
        return None, False
    for span_inner, elem_inner in _STRUCK_RE.findall(text_format):
        m = _AMOUNT_RE.search(html.unescape(_TAG_RE.sub("", span_inner or elem_inner)))
        if m:
            return Money(_cents_from_amount(m.group(1)), currency), False
    plain = html.unescape(_TAG_RE.sub(" ", text_format))
    amounts = _AMOUNT_RE.findall(plain)
    return None, len(amounts) >= 2


def _promo_texts(item: dict) -> list[tuple[str, bool]]:
    """Every (text, is_promo_badge) the item carries, in display order, stripped.

    Promo-badge texts (promoInfo.promoBadge) are promotional by construction;
    thumbnail tags also carry "#1 most liked"-style labels, so a tag only
    counts as a deal when it matches the grammar."""
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(t: Any, badge: bool) -> None:
        if isinstance(t, str) and t.strip() and t.strip() not in seen:
            seen.add(t.strip())
            out.append((t.strip(), badge))

    promo = item.get("promoInfo")
    if isinstance(promo, dict):
        badge = promo.get("promoBadge")
        if isinstance(badge, dict):
            add(badge.get("accessibilityText"), True)
            content = badge.get("content")
            if isinstance(content, dict):
                rich = content.get("richText")
                if isinstance(rich, dict):
                    add(rich.get("accessibilityText"), True)
                    for el in rich.get("richTextElements") or []:
                        try:
                            add(el["text"]["text"]["text"], True)
                        except (KeyError, TypeError):
                            pass
    for el in item.get("itemThumbnailElements") or []:
        if not isinstance(el, dict):
            continue
        payload = el.get("payload")
        if not isinstance(payload, dict):
            continue
        tags = payload.get("tagsPayload")
        if isinstance(tags, dict):
            for tag in tags.get("tags") or []:
                if isinstance(tag, dict):
                    add(tag.get("text"), False)
    return out


def item_deal(item: dict) -> tuple[Deal | None, str | None]:
    """(deal, note) carried by one catalog item."""
    raw_type: str | None = None
    bogo_synth: str | None = None
    ip = item.get("itemPromotion")
    if isinstance(ip, dict):
        t = ip.get("type")
        raw_type = t if isinstance(t, str) and t else None
        bxgy = ip.get("buyXGetYItemPromotion")
        if isinstance(bxgy, dict):
            buy, get = bxgy.get("buyQuantity"), bxgy.get("getQuantity")
            if _is_num(buy) and _is_num(get):
                bogo_synth = f"Buy {int(buy)}, get {int(get)} free"
            else:
                bogo_synth = "Buy 1, get 1 free"

    note: str | None = None
    deal: Deal | None = None
    fallback: Deal | None = None
    for text, is_badge in _promo_texts(item):
        if fold(text) == "new":
            continue  # a "New" badge is a marker, not a deal (same rule as the feed)
        if _NOTE_RE.search(text):
            if note is None:
                note = text
            continue
        candidate = parse_deal(text, raw_type)
        if candidate.type != "other":
            if deal is None:
                deal = candidate
        elif is_badge and fallback is None:
            # A promo badge whose wording is outside the grammar: keep verbatim.
            fallback = candidate
    if deal is None:
        deal = fallback
    if deal is None and bogo_synth is not None:
        deal = parse_deal(bogo_synth, raw_type or "buyXGetYItemPromotion")
    if deal is None and raw_type and raw_type != "buyXGetYItemPromotion":
        deal = Deal(text=raw_type, type="other", raw_type=raw_type)
    return deal, note


def _hours(raw: Any) -> dict[str, tuple[HoursSpan, ...]]:
    out: dict[str, tuple[HoursSpan, ...]] = {}
    if not isinstance(raw, list):
        return out
    for day in raw:
        if not isinstance(day, dict) or not isinstance(day.get("dayRange"), str):
            continue
        spans: list[HoursSpan] = []
        for sh in day.get("sectionHours") or []:
            if not isinstance(sh, dict):
                continue
            s, e = sh.get("startTime"), sh.get("endTime")
            if _is_num(s) and _is_num(e):
                spans.append(HoursSpan(int(s), int(e)))
        out[day["dayRange"]] = tuple(spans)
    return out


def _rating(raw: Any) -> tuple[float | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    value = raw.get("ratingValue")
    value = float(value) if _is_num(value) else None
    count = raw.get("reviewCount")
    if _is_num(count):
        count = str(int(count))
    count = count.strip() if isinstance(count, str) and count.strip() else None
    return value, count


def _pickup_eta(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for opt in raw.get("modalityOptions") or []:
        if isinstance(opt, dict) and opt.get("diningMode") == "PICKUP":
            sub = opt.get("subtitle")
            if isinstance(sub, str) and sub.strip():
                return sub.split("•", 1)[0].strip() or None
    return None


def _distance_km(raw: Any) -> float | None:
    text = raw.get("text") if isinstance(raw, dict) else raw
    if not isinstance(text, str):
        return None
    m = _DISTANCE_RE.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2).lower().startswith("mi"):
        value *= 1.609344
    return round(value, 1)


def _text(obj: Any) -> str | None:
    if isinstance(obj, dict):
        obj = obj.get("text")
    return obj.strip() if isinstance(obj, str) and obj.strip() else None


def _blocks(data: dict):
    """Yield (section_title, rank, catalogItems) for every menu block.

    rank 0 = a real menu section, 1 = a promo block (carries promoUUID, e.g.
    "Save on Select Items"), 2 = "Featured items". A dish's section is the
    lowest-ranked block it appears in, first occurrence winning within a rank."""
    csm = data.get("catalogSectionsMap")
    if not isinstance(csm, dict):
        raise PayloadError("getStoreV1: catalogSectionsMap missing or not an object")
    for blocks in csm.values():
        if not isinstance(blocks, list):
            raise PayloadError("getStoreV1: catalogSectionsMap entry is not a list")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            payload = block.get("payload")
            if not isinstance(payload, dict):
                continue
            sp = payload.get("standardItemsPayload")
            if not isinstance(sp, dict):
                continue
            items = sp.get("catalogItems")
            if items is None:
                continue
            if not isinstance(items, list):
                raise PayloadError("getStoreV1: catalogItems is not a list")
            title = _text(sp.get("title"))
            if (title or "").strip().lower() == FEATURED_TITLE:
                rank = 2
            elif sp.get("promoUUID"):
                rank = 1
            else:
                rank = 0
            yield title, rank, items


def _dish(item: Any, section: str | None, currency: str) -> Dish:
    if not isinstance(item, dict):
        raise PayloadError("getStoreV1: catalog item is not an object")
    uuid, title, price = item.get("uuid"), item.get("title"), item.get("price")
    if not isinstance(uuid, str) or not uuid or not isinstance(title, str) or not title.strip():
        raise PayloadError("getStoreV1: catalog item lacks uuid or title")
    if not _is_num(price):
        raise PayloadError(f"getStoreV1: item {title!r} has no numeric price")
    tagline = item.get("priceTagline")
    text_format = tagline.get("textFormat") if isinstance(tagline, dict) else None
    was, unclear = was_price(text_format, currency)
    deal, note = item_deal(item)
    desc = item.get("itemDescription")
    desc = desc.strip() if isinstance(desc, str) and desc.strip() else None
    return Dish(
        uuid=uuid, title=title.strip(), description=desc, section=section,
        price=money(price, currency), was=was, deal=deal,
        sold_out=bool(item.get("isSoldOut")), has_options=bool(item.get("hasCustomizations")),
        section_uuid=item.get("sectionUuid") if isinstance(item.get("sectionUuid"), str) else None,
        subsection_uuid=item.get("subsectionUuid") if isinstance(item.get("subsectionUuid"), str) else None,
        note=note, price_unclear=unclear,
    )


def _merge(first: Dish, later: Dish, section: str | None, better_section: bool) -> Dish:
    """First occurrence wins; a later copy fills in what it lacked."""
    from dataclasses import replace
    changes: dict[str, Any] = {}
    if better_section and section is not None:
        changes["section"] = section
    if first.deal is None and later.deal is not None:
        changes["deal"] = later.deal
    if first.note is None and later.note is not None:
        changes["note"] = later.note
    if first.was is None and later.was is not None and later.price.cents == first.price.cents:
        changes["was"] = later.was
        changes["price_unclear"] = False
    return replace(first, **changes) if changes else first


def store(data: dict, located: bool = True) -> Store:
    data = _unwrap(data)
    uuid, title = data.get("uuid"), data.get("title")
    if not isinstance(uuid, str) or not uuid:
        raise PayloadError("getStoreV1: uuid missing")
    if not isinstance(title, str) or not title.strip():
        raise PayloadError("getStoreV1: title missing")
    currency = data.get("currencyCode")
    currency = currency if isinstance(currency, str) and currency else "CAD"

    order: list[str] = []
    dishes: dict[str, Dish] = {}
    section_rank: dict[str, int] = {}
    entries = 0
    for sec_title, rank, items in _blocks(data):
        section = sec_title  # a featured-only dish keeps "Featured items"; a better block replaces it
        for raw in items:
            entries += 1
            d = _dish(raw, section, currency)
            if d.uuid in dishes:
                better = rank < section_rank[d.uuid]
                dishes[d.uuid] = _merge(dishes[d.uuid], d, section, better)
                if better:
                    section_rank[d.uuid] = rank
            else:
                dishes[d.uuid] = d
                order.append(d.uuid)
                section_rank[d.uuid] = rank
    dish_list = tuple(dishes[u] for u in order)

    deals: list[Deal] = []
    seen_deals: set[str] = set()
    for d in dish_list:
        if d.deal is not None and d.deal.text not in seen_deals:
            seen_deals.add(d.deal.text)
            deals.append(d.deal)

    rating, count = _rating(data.get("rating"))
    loc = data.get("location") if isinstance(data.get("location"), dict) else {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    closed = data.get("closedMessage")
    closed = closed.strip() if isinstance(closed, str) and closed.strip() else None
    cuisines = tuple(c.strip() for c in (data.get("cuisineList") or []) if isinstance(c, str) and c.strip()) \
        if isinstance(data.get("cuisineList"), list) else ()
    phone = data.get("phoneNumber")
    within = data.get("isWithinDeliveryRange")
    slug = data.get("slug")

    return Store(
        uuid=uuid, title=title.strip(), slug=slug if isinstance(slug, str) and slug else None,
        address=_text(loc.get("address")),
        is_open=bool(data.get("isOpen")), is_orderable=bool(data.get("isOrderable")),
        closed_message=closed, hours=_hours(data.get("hours")),
        rating=rating, rating_count_text=count,
        eta_text=_text(data.get("etaRange")) if located else None,
        pickup_eta_text=_pickup_eta(data.get("modalityInfo")) if located else None,
        distance_km=_distance_km(data.get("distanceBadge")) if located else None,
        within_range=(bool(within) if isinstance(within, bool) else None) if located else None,
        cuisines=cuisines, phone=phone if isinstance(phone, str) and phone.strip() else None,
        currency=currency,
        latitude=float(lat) if _is_num(lat) else None, longitude=float(lng) if _is_num(lng) else None,
        has_store_promotion=bool(data.get("hasStorePromotion")),
        deals=tuple(deals), dishes=dish_list,
        entries_total=entries, duplicates_removed=entries - len(dish_list),
    )
