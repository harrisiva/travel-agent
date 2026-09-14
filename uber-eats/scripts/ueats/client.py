"""Client — Lane C. Composes transport + parsers into the nine questions.

Rules (design 02): the CLI calls transport.plan() once, before the first
request of every command, with the whole plan (location + command + per-store
fan-out); nothing here plans on its own. A name in STORE runs find() first
(D3); one failing store never aborts compare / deals --items (it lands in
failed[]) — except a Cloudflare challenge, which is not a store failure and
would only be repeated by every later request, so it propagates (exit 3).
now() is the only clock (tests pin it).

Nothing here is cached: the feed, menus, prices, deals and open/closed state
are fetched on every call. Only resolved addresses go through LocationCache.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import ids as idmod
from . import location as locmod
from . import parse_feed, parse_item, parse_store
from .http import Blocked, LookupFailure, PayloadError, RequestBudgetError, Transport, UEHTTPError
from .location import LocationCache
from .model import (
    Candidate, Deal, Dish, FeedMeta, HoursSpan, ItemDetail, Location, Money, PlaceCandidate,
    Store, StoreRow, fold,
)


class QueryError(Exception):
    """Usage / lookup problems the user can fix (exit 2)."""


class Ambiguous(QueryError):
    def __init__(self, message: str, candidates: list) -> None:
        super().__init__(message)
        self.candidates = candidates


class OutsideServiceArea(QueryError):
    """getFeedV1 answered isInServiceArea: false for the address (exit 2)."""


def now() -> datetime:
    return datetime.now().astimezone()


FEED_BODY = {
    "cacheKey": "", "feedSessionCount": {"announcementCount": 0, "announcementLabel": ""},
    "userQuery": "", "date": "", "startTime": 0, "endTime": 0, "carouselId": "",
    "sortAndFilters": [], "billboardUuid": "", "feedProvider": "", "promotionUuid": "",
    "targetingStoreTag": "", "venueUUID": "", "selectedSectionUUID": "", "favorites": "",
    "vertical": "", "searchSource": "", "searchType": "", "keyName": "",
    "serializedRequestContext": "", "isUserInitiatedRefresh": False,
}

#: What `locate` costs when the address is neither a token nor cached (G2).
LOCATE_REQUESTS = 2
MAX_PAGES = 5
MAX_CANDIDATES = 5

#: The sentence every "not in the feed" answer must carry (design 02 §3.3).
NOT_IN_FEED = ("this does not mean it isn't on Uber Eats; paste its Uber Eats link "
               "to use it directly")

#: `doctor` needs a location with coordinates without spending the second
#: locate request on it: Union Station, exactly as getDeliveryLocationV1
#: returned it during the probe (evidence/g2_deliveryloc.json).
DOCTOR_QUERY = "Union Station Toronto"
DOCTOR_LOCATION = Location(
    line1="Toronto Union Station Train Station", line2="65 Front St W, Toronto, ON M5J 1E6",
    reference="180933fc-e611-398d-9512-2a86a6ecca45", reference_type="uber_places",
    latitude=43.6452223, longitude=-79.3806428,
    formatted="65 Front St W, Toronto, ON M5J 1E6, CA",
)

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --- small value objects the CLI renders ---------------------------------------

@dataclass(frozen=True)
class CompareRow:
    dish: Dish
    store: StoreRow
    second_free: bool
    effective_each: Money | None     # price / 2 on a BOGO dish; else None
    weak: bool                       # matched on the description only

    def to_dict(self) -> dict:
        return {"price": self.dish.price.to_dict(),
                "was": self.dish.was.to_dict() if self.dish.was else None,
                "deal": self.dish.deal.to_dict() if self.dish.deal else None,
                "second_free": self.second_free,
                "effective_each": self.effective_each.to_dict() if self.effective_each else None,
                "dish": self.dish.title, "dish_uuid": self.dish.uuid, "sold_out": self.dish.sold_out,
                "store": self.store.name, "store_uuid": self.store.uuid,
                "rating": self.store.rating, "eta": eta_text(self.store),
                "distance_km": self.store.distance_km}


@dataclass(frozen=True)
class StoreFailure:
    uuid: str
    name: str
    reason: str
    kind: str = "network"            # "lookup" | "usage" (budget) | "network" — not emitted in JSON

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "name": self.name, "reason": self.reason}


def eta_text(row: StoreRow) -> str | None:
    if row.eta_min is None and row.eta_max is None:
        return None
    if row.eta_min is not None and row.eta_max is not None and row.eta_min != row.eta_max:
        return f"{row.eta_min}–{row.eta_max} min"
    return f"{row.eta_max if row.eta_max is not None else row.eta_min} min"


# --- hours --------------------------------------------------------------------

def _day_index(word: str) -> int | None:
    w = word.strip().lower().rstrip(".")
    for i, name in enumerate(DAY_NAMES):
        if w == name or (len(w) >= 3 and name.startswith(w)):
            return i
    return None


def _range_covers(day_range: str, weekday: int) -> bool:
    text = (day_range or "").strip().lower()
    if text in ("every day", "everyday", "daily", "all days", "all week", ""):
        return text != ""
    parts = [p for p in text.replace("–", "-").replace(" to ", "-").split("-") if p.strip()]
    if len(parts) == 1:
        # "Monday", or "Mon, Wed" style lists
        return any(_day_index(p) == weekday for p in parts[0].replace(",", " ").split())
    if len(parts) == 2:
        a, b = _day_index(parts[0]), _day_index(parts[1])
        if a is None or b is None:
            return False
        if a <= b:
            return a <= weekday <= b
        return weekday >= a or weekday <= b       # wraps past Sunday
    return False


def hours_today(store: Store, at: datetime) -> tuple[HoursSpan, ...] | None:
    """The spans for `at`'s weekday, or None when the store lists no hours for it."""
    for day_range, spans in store.hours.items():
        if _range_covers(day_range, at.weekday()):
            return tuple(spans)
    return None


def next_opening(spans: tuple[HoursSpan, ...] | None, at: datetime) -> str | None:
    """'16:30' when a span starts later today, else None."""
    if not spans:
        return None
    minutes = at.hour * 60 + at.minute
    later = sorted(s.start_minutes for s in spans if s.start_minutes > minutes)
    if not later:
        return None
    return f"{later[0] // 60:02d}:{later[0] % 60:02d}"


# --- matching -----------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return fold(text).split()


def match_kind(query: str, name: str) -> str | None:
    """'exact' | 'all_tokens' | 'prefix' | None, the way find() scores a store title."""
    q, n = fold(query), fold(name)
    if not q:
        return None
    if q == n:
        return "exact"
    q_tokens, n_tokens = q.split(), n.split()
    if all(t in n_tokens for t in q_tokens):
        return "all_tokens"
    if all(any(nt.startswith(t) for nt in n_tokens) for t in q_tokens):
        return "prefix"
    return None


_RANK = {"exact": 0, "all_tokens": 1, "prefix": 2}


def dish_matches(query: str, dish: Dish) -> str | None:
    """'exact' | 'title' | 'description' | None."""
    q = fold(query)
    if not q:
        return None
    title = fold(dish.title)
    if title == q:
        return "exact"
    q_tokens = q.split()
    if all(t in title.split() for t in q_tokens) or q in title:
        return "title"
    desc = fold(dish.description or "")
    if desc and all(t in desc.split() for t in q_tokens):
        return "description"
    return None


# --- the client -----------------------------------------------------------------

class Client:
    def __init__(self, transport: Transport, cache: LocationCache | None = None) -> None:
        self.transport = transport
        self.cache = cache

    # location -----------------------------------------------------------------
    def locate(self, text: str, pick: int = 1) -> tuple[list[PlaceCandidate], Location]:
        """mapsSearchV1 then getDeliveryLocationV1 on candidate `pick` (1-based)."""
        text = " ".join((text or "").split())
        if not text:
            raise QueryError("an address is needed, e.g. locate \"65 Front St W, Toronto\"")
        candidates = locmod.search(self.transport, text)[:MAX_CANDIDATES]
        if not candidates:
            raise QueryError(f"Uber found no place matching {text!r} — try a street address or a landmark with its city")
        if not 1 <= pick <= len(candidates):
            raise QueryError(f"--pick {pick} is out of range: Uber returned {len(candidates)} candidate(s) for {text!r}")
        loc = locmod.resolve(self.transport, candidates[pick - 1])
        if self.cache is not None and pick == 1:
            try:
                self.cache.put(text, loc)
            except Exception:       # a cache that cannot be written is not an error
                pass
        return candidates, loc

    def location_cost(self, at: str | None) -> int:
        """Requests that resolving --at will cost: 0 for None, a token or a cached address."""
        if at is None or locmod.is_token(at):
            return 0
        if self.cache is not None and self.cache.get(" ".join(at.split())) is not None:
            return 0
        return LOCATE_REQUESTS

    def location_for(self, at: str | None) -> Location | None:
        """--at handling: None → None; a token → decoded, 0 requests; cached text → 0;
        otherwise locate() (2 requests) and cache."""
        if at is None:
            return None
        if locmod.is_token(at):
            return locmod.from_token(at)
        text = " ".join(at.split())
        if not text:
            raise QueryError("--at needs an address or a location token from `locate`")
        if self.cache is not None:
            hit = self.cache.get(text)
            if hit is not None:
                return hit
        _, loc = self.locate(text)
        return loc

    # feed ----------------------------------------------------------------------
    def feed(self, loc: Location, pages: int = 1) -> tuple[list[StoreRow], FeedMeta]:
        """getFeedV1 at `loc`, following pageInfo for up to `pages` pages (G4).
        Raises OutsideServiceArea when Uber does not deliver there."""
        pages = max(1, min(int(pages), MAX_PAGES))
        body = dict(FEED_BODY)
        data = self.transport.call("getFeedV1", body, loc)
        rows, meta = parse_feed.stores(data, loc)
        if not meta.in_service_area:
            raise OutsideServiceArea(f"Uber Eats doesn't deliver to {loc.label}")
        seen = {r.uuid for r in rows}
        offset, has_more = meta.offset, meta.has_more
        for _ in range(pages - 1):
            if not has_more or offset is None:
                break
            page_body = dict(FEED_BODY)
            page_body["pageInfo"] = {"offset": offset, "pageSize": parse_feed.PAGE_SIZE}
            more, page_meta = parse_feed.stores(self.transport.call("getFeedV1", page_body, loc), loc)
            for r in more:
                if r.uuid not in seen:
                    seen.add(r.uuid)
                    rows.append(r)
            offset, has_more = page_meta.offset, page_meta.has_more
        meta = FeedMeta(currency=meta.currency, in_service_area=meta.in_service_area,
                        stores_returned=len(rows), offset=offset, has_more=has_more)
        return rows, meta

    def find(self, name: str, loc: Location, pages: int = 1) -> tuple[list[Candidate], FeedMeta]:
        rows, meta = self.feed(loc, pages)
        return self.match_stores(name, rows), meta

    @staticmethod
    def match_stores(name: str, rows: list[StoreRow]) -> list[Candidate]:
        found = []
        for i, row in enumerate(rows):
            kind = match_kind(name, row.name)
            if kind:
                found.append((_RANK[kind], i, Candidate(row=row, match=kind)))
        found.sort(key=lambda t: (t[0], t[1]))
        return [c for _, _, c in found]

    # store ---------------------------------------------------------------------
    def resolve_store(self, text: str, loc: Location | None) -> tuple[str, str | None]:
        """(uuid, name-used-for-match) — id forms cost 0; a name costs a feed (D3).

        Confident = exactly one exact match, or no exact match and exactly one
        all-tokens match. Anything else is exit 2 with the candidates listed.
        """
        text = (text or "").strip()
        uuid = idmod.store_uuid(text)
        if uuid:
            return uuid, None
        if not text:
            raise QueryError("STORE is empty — give a restaurant name, its Uber Eats link or its UUID")
        if loc is None:
            raise QueryError(f"{text!r} looks like a restaurant name, and a name can only be looked up near an "
                             f"address: add --at \"<address>\", or pass the store's Uber Eats link or UUID")
        candidates, meta = self.find(text, loc)
        exact = [c for c in candidates if c.match == "exact"]
        strong = [c for c in candidates if c.match == "all_tokens"]
        if len(exact) == 1:
            return exact[0].row.uuid, exact[0].row.name
        if not exact and len(strong) == 1:
            return strong[0].row.uuid, strong[0].row.name
        if candidates:
            listing = "; ".join(f"{c.row.name} ({c.row.uuid})" for c in candidates[:8])
            raise Ambiguous(
                f"{text!r} matches {len(candidates)} stores near {loc.label}: {listing} — "
                f"pass the UUID or the store's Uber Eats link", candidates)
        raise QueryError(
            f"{text!r} is not among the {meta.stores_returned} stores Uber's feed returned near "
            f"{loc.label} — {NOT_IN_FEED}")

    def menu(self, uuid: str, loc: Location | None, pickup: bool = False) -> Store:
        body = {"storeUuid": uuid, "diningMode": "PICKUP" if pickup else "DELIVERY",
                "time": {"asap": True}, "cbType": "EATER_ENDORSED"}
        data = self.transport.call("getStoreV1", body, loc)
        return parse_store.store(data, located=loc is not None)

    @staticmethod
    def match_dish(store: Store, query: str) -> Dish:
        """The one dish `query` names on this menu; Ambiguous / QueryError otherwise."""
        exact, by_title = [], []
        for d in store.dishes:
            kind = dish_matches(query, d)
            if kind == "exact":
                exact.append(d)
            elif kind == "title":
                by_title.append(d)
        if len(exact) == 1:
            return exact[0]
        pool = exact or by_title
        if len(pool) == 1:
            return pool[0]
        if pool:
            listing = "; ".join(f"{d.title} {d.price.display()}" + (" (sold out)" if d.sold_out else "")
                                for d in pool[:10])
            raise Ambiguous(f"{query!r} matches {len(pool)} dishes at {store.title}: {listing} — "
                            f"use the exact dish name", pool)
        raise QueryError(f"no dish matching {query!r} on {store.title}'s menu "
                         f"({len(store.dishes)} dishes) — `menu` lists them")

    def item(self, store: Store, dish_query: str, loc: Location | None) -> ItemDetail:
        dish = self.match_dish(store, dish_query)
        return self.item_for(store, dish, loc)

    def item_for(self, store: Store, dish: Dish, loc: Location | None) -> ItemDetail:
        body = {"itemRequestType": "ITEM", "storeUuid": store.uuid,
                "sectionUuid": dish.section_uuid or "", "subsectionUuid": dish.subsection_uuid or "",
                "menuItemUuid": dish.uuid, "cbType": "EATER_ENDORSED", "contextReferences": []}
        data = self.transport.call("getMenuItemV1", body, loc)
        return parse_item.item(data, dish, store.currency)

    # fan-out -------------------------------------------------------------------
    def _menu_or_failure(self, row: StoreRow, loc: Location, pickup: bool) -> tuple[Store | None, StoreFailure | None]:
        try:
            return self.menu(row.uuid, loc, pickup), None
        except Blocked:
            raise                                   # not a store failure; see module docstring
        except RequestBudgetError:
            raise
        except LookupFailure as e:
            return None, StoreFailure(row.uuid, row.name, f"Uber says the store id is unknown: {e}", "lookup")
        except (UEHTTPError, PayloadError) as e:
            return None, StoreFailure(row.uuid, row.name, str(e) or type(e).__name__, "network")

    def compare(self, query: str, rows: list[StoreRow], loc: Location, pickup: bool = False
                ) -> tuple[list[CompareRow], list[CompareRow], list[StoreFailure]]:
        """Read `rows`' menus in order; (ranked rows, weaker description-only rows, failures)."""
        strong: list[CompareRow] = []
        weaker: list[CompareRow] = []
        failed: list[StoreFailure] = []
        pending = list(rows)
        while pending:
            row = pending.pop(0)
            try:
                store, failure = self._menu_or_failure(row, loc, pickup)
            except RequestBudgetError as e:
                failed.append(StoreFailure(row.uuid, row.name, f"request budget exhausted: {e}", "usage"))
                failed.extend(StoreFailure(r.uuid, r.name, "not checked: request budget exhausted", "usage") for r in pending)
                break
            if failure:
                failed.append(failure)
                continue
            for dish in store.dishes:
                kind = dish_matches(query, dish)
                if not kind:
                    continue
                bogo = bool(dish.deal and dish.deal.type == "bogo")
                each = Money(dish.price.cents / 2, dish.price.currency) if bogo else None
                out = CompareRow(dish=dish, store=row, second_free=bogo, effective_each=each,
                                 weak=(kind == "description"))
                (weaker if out.weak else strong).append(out)

        def key(r: CompareRow):
            return (r.dish.sold_out, r.dish.price.cents,
                    r.store.distance_km if r.store.distance_km is not None else float("inf"))
        strong.sort(key=key)
        weaker.sort(key=key)
        return strong, weaker, failed

    def deal_dishes(self, rows: list[StoreRow], loc: Location, pickup: bool = False
                    ) -> tuple[dict[str, list[Dish]], list[StoreFailure]]:
        """For deals --items: uuid → dishes carrying a deal; failures collected."""
        out: dict[str, list[Dish]] = {}
        failed: list[StoreFailure] = []
        pending = list(rows)
        while pending:
            row = pending.pop(0)
            try:
                store, failure = self._menu_or_failure(row, loc, pickup)
            except RequestBudgetError as e:
                failed.append(StoreFailure(row.uuid, row.name, f"request budget exhausted: {e}", "usage"))
                failed.extend(StoreFailure(r.uuid, r.name, "not checked: request budget exhausted", "usage") for r in pending)
                break
            if failure:
                failed.append(failure)
                continue
            out[row.uuid] = [d for d in store.dishes if d.deal is not None]
        return out, failed


# --- deal filters shared by nearby / deals -----------------------------------------

DEAL_TYPE_FLAGS = {"bogo": "bogo", "percent": "percent", "dollar": "dollar",
                   "free-delivery": "free_delivery", "free_delivery": "free_delivery", "other": "other"}


def deal_type(flag: str) -> str:
    try:
        return DEAL_TYPE_FLAGS[flag]
    except KeyError:
        raise QueryError(f"unknown deal type {flag!r}: choose bogo, percent, dollar, free-delivery or other")


def keep_deal(deal: Deal, type_: str | None, min_spend_at_most: float | None) -> bool:
    if type_ and deal.type != type_:
        return False
    if min_spend_at_most is not None and deal.min_spend is not None:
        try:
            if float(deal.min_spend) > min_spend_at_most:
                return False
        except ValueError:
            return False
    return True
