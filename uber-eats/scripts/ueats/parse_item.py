"""getMenuItemV1 → ItemDetail — Lane B (design 01 §6).

customizationsList[] → OptionGroup(uuid, title, minPermitted, maxPermitted,
options[]); option → Option(uuid, title, Money(price), isSoldOut, minPermitted,
maxPermitted, defaultQuantity, groups from childCustomizationList recursively).
from_price via model.from_price; required_groups = count of min>0.
price/was/deal as in parse_store (the item carries priceTagline-like fields
only sometimes — fall back to the Dish passed in).

Observed (item.json, item_pizza.json): `priceTagline`, `itemPromotion` and
`currencyCode` are all null on the item — so `was` and `deal` come from the
Dish unless the item carries them, and the currency is always the store's.
`data` may be the whole {"status","data"} envelope or just `data`.
"""
from __future__ import annotations

from typing import Any

from .http import PayloadError
from .model import Dish, ItemDetail, Money, Option, OptionGroup, from_price
from .parse_store import item_deal, money, was_price


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v: Any, default: int) -> int:
    return int(v) if _is_num(v) else default


def _unwrap(data: Any) -> dict:
    if not isinstance(data, dict):
        raise PayloadError("getMenuItemV1: body is not a JSON object")
    if "customizationsList" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    return data


def item(data: dict, dish: Dish, currency: str) -> ItemDetail:
    data = _unwrap(data)
    if not any(k in data for k in ("uuid", "price", "customizationsList")):
        raise PayloadError("getMenuItemV1: body carries no item (no uuid, price or customizationsList)")
    uuid = data.get("uuid") if isinstance(data.get("uuid"), str) and data.get("uuid") else dish.uuid
    title = data.get("title") if isinstance(data.get("title"), str) and data.get("title").strip() else dish.title
    if not uuid or not title:
        raise PayloadError("getMenuItemV1: item lacks uuid or title")
    title = title.strip()

    raw_price = data.get("price")
    if _is_num(raw_price):
        price = money(raw_price, currency)
    elif raw_price is None:
        price = dish.price
    else:
        raise PayloadError(f"getMenuItemV1: item {title!r} has a non-numeric price")

    tagline = data.get("priceTagline")
    was: Money | None = None
    if isinstance(tagline, dict):
        was, _unclear = was_price(tagline.get("textFormat"), currency)
    if was is None and dish.was is not None and dish.price.cents == price.cents:
        was = dish.was

    deal, _note = item_deal(data)
    if deal is None:
        deal = dish.deal

    sold_out = data.get("isSoldOut")
    sold_out = bool(sold_out) if isinstance(sold_out, bool) else dish.sold_out

    grp = groups(data.get("customizationsList"), currency)
    return ItemDetail(
        uuid=uuid, title=title, price=price, was=was, deal=deal, sold_out=sold_out,
        groups=grp, from_price=from_price(price, grp),
        required_groups=sum(1 for g in grp if g.min_permitted > 0),
    )


def _option(raw: Any, currency: str, depth: int) -> Option:
    if not isinstance(raw, dict):
        raise PayloadError("getMenuItemV1: option is not an object")
    uuid, title, price = raw.get("uuid"), raw.get("title"), raw.get("price")
    if not isinstance(uuid, str) or not uuid or not isinstance(title, str):
        raise PayloadError("getMenuItemV1: option lacks uuid or title")
    if price is None:
        price = 0
    if not _is_num(price):
        raise PayloadError(f"getMenuItemV1: option {title!r} has a non-numeric price")
    return Option(
        uuid=uuid, title=title.strip(), price=money(price, currency),
        sold_out=bool(raw.get("isSoldOut")),
        min_qty=_int(raw.get("minPermitted"), 0), max_qty=_int(raw.get("maxPermitted"), 0),
        default_qty=_int(raw.get("defaultQuantity"), 0),
        groups=_groups(raw.get("childCustomizationList"), currency, depth + 1),
    )


def _groups(raw: Any, currency: str, depth: int) -> tuple[OptionGroup, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PayloadError("getMenuItemV1: customizationsList is not a list")
    if depth > 8:
        raise PayloadError("getMenuItemV1: customizations nested deeper than 8 levels")
    out: list[OptionGroup] = []
    for g in raw:
        if not isinstance(g, dict):
            raise PayloadError("getMenuItemV1: customization group is not an object")
        uuid, title = g.get("uuid"), g.get("title")
        if not isinstance(uuid, str) or not uuid:
            raise PayloadError("getMenuItemV1: customization group lacks uuid")
        title = title.strip() if isinstance(title, str) else ""
        options = g.get("options")
        if options is None:
            options = []
        if not isinstance(options, list):
            raise PayloadError(f"getMenuItemV1: group {title!r} options is not a list")
        out.append(OptionGroup(
            uuid=uuid, title=title,
            min_permitted=_int(g.get("minPermitted"), 0), max_permitted=_int(g.get("maxPermitted"), 0),
            options=tuple(_option(o, currency, depth) for o in options),
        ))
    return tuple(out)


def groups(raw: list | None, currency: str) -> tuple[OptionGroup, ...]:
    return _groups(raw, currency, 0)
