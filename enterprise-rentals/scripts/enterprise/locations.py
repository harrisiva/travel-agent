"""Branch lookup against the location service.

A separate host from pricing, with a much simpler personality: plain
unauthenticated GETs, no TLS fingerprinting, no headers beyond Accept. Kept in
its own module so the pricing host's transport quirks never leak into a code
path that does not need them.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import cache
from .errors import AmbiguousLocation, EnterpriseError, LocationNotFound
from .http import USER_AGENT, Transport
from .model import Location

BASE = "https://prd.location.enterprise.com/enterprise-sls/search/location/enterprise/web"

#: Resolving a bare numeric id needs the pricing host's location endpoint - the
#: text search cannot look an id up. Getting this right matters more than it
#: looks: a fabricated location with a guessed country makes a US branch appear
#: Canadian, which silently disables cross-border detection and the Exotic
#: warning.
DETAIL = "https://prd-east.webapi.enterprise.ca/enterprise-ewt/location"

#: Response buckets, in the order a human would want them ranked. `exotics` is
#: last on purpose: those branches carry a different fleet at very different
#: prices, and picking one by accident is a documented trap.
BUCKETS = (
    ("airports", "airport"),
    ("branches", "branch"),
    ("railStations", "rail"),
    ("portsOfCall", "port"),
    ("cities", "city"),
    ("trucks", "truck"),
    ("exotics", "exotic"),
)


def _is_error_body(raw: Any) -> bool:
    """True for the 200-with-an-error-message shape both hosts use.

    An unknown branch id comes back as HTTP 200 carrying
    `CROS_LOCATION_INVALID_LOCATIONID`. Cached, that error would answer for the
    id for a week; matched on wording it would break in another locale, so the
    test is on the message *priority*.
    """
    messages = raw.get("messages") if isinstance(raw, dict) else None
    return any(
        isinstance(m, dict) and str(m.get("priority") or "").upper() == "ERROR"
        for m in messages or []
    )


def _is_branch_detail(raw: Any) -> bool:
    """True only for a `location/{id}` body that really carries a branch.

    This is the validator that stops a foreign payload under a `locations` key
    from being read as "the service does not know this id".
    """
    node = raw.get("location") if isinstance(raw, dict) else None
    return isinstance(node, dict) and bool(node.get("id"))


def _is_search_result(raw: Any) -> bool:
    """True for a text-search body with at least one recognised bucket."""
    if not isinstance(raw, dict) or _is_error_body(raw):
        return False
    return any(bucket in raw for bucket, _ in BUCKETS)


def _is_answer(raw: Any) -> bool:
    """Weakest useful check: something was returned, and it is not an error."""
    return raw is not None and not _is_error_body(raw)


class LocationClient:
    def __init__(
        self,
        transport: Transport,
        *,
        brand: str = "ENTERPRISE",
        country: str = "CA",
        locale: str = "en_CA",
        use_cache: bool = True,
    ) -> None:
        self.transport = transport
        self.brand = brand
        self.country = country
        self.locale = locale
        self.use_cache = use_cache

    def _get(self, path: str, params: dict) -> Any:
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        return self.transport.get_json(url, {"Accept": "application/json"})

    def search(self, query: str) -> list[Location]:
        """All matching locations, best bucket first."""
        key = f"{query}|{self.country}|{self.brand}|{self.locale}"

        def fetch() -> Any:
            return self._get(
                f"text/{urllib.parse.quote(query)}",
                {
                    "countryCode": self.country,
                    "includeExotics": "true",
                    "brand": self.brand,
                    "dto": "true",
                    "cor": self.country,
                    "locale": self.locale,
                },
            )

        raw = cache.get_or_fetch(
            "locations", key, fetch,
            enabled=self.use_cache, validate=_is_search_result,
        )
        # A branch can appear in more than one bucket - exotic airports show up
        # under both `airports` and `exotics` - so dedupe by id, keeping the
        # first (best-ranked) bucket it was seen in.
        found: list[Location] = []
        seen: set[str] = set()
        for bucket, kind in BUCKETS:
            for entry in raw.get(bucket) or []:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                location = Location.parse(entry, kind)
                if location.id in seen:
                    continue
                seen.add(location.id)
                found.append(location)
        return found

    def resolve(self, query: str) -> Location:
        """Exactly one bookable branch, or raise.

        An id is passed straight through, which is how a caller escapes an
        ambiguity. Otherwise: one match wins; several is an error listing the
        candidates, because guessing between Halifax Airport and Halifax
        Airport Exotic silently changes both fleet and price.
        """
        if query.isdigit():
            for candidate in self.search(query):
                if candidate.id == query:
                    return candidate
            return self.by_id(query)

        matches = [c for c in self.search(query) if c.bookable]
        if not matches:
            raise LocationNotFound(query)
        if len(matches) == 1:
            return matches[0]

        # An exact airport-code hit beats everything, and a single
        # non-exotic airport is the obvious intent.
        needle = query.strip().upper()
        exact = [c for c in matches if (c.airport_code or "").upper() == needle]
        if len(exact) == 1:
            return exact[0]
        mainstream = [c for c in (exact or matches) if not c.is_exotic]
        if len(mainstream) == 1:
            return mainstream[0]
        raise AmbiguousLocation(query, (exact or matches)[:10])

    def by_id(self, location_id: str) -> Location:
        """Full detail for a numeric branch id.

        Never guesses the country. If the lookup fails the country is left
        None, so `QuoteRequest.is_cross_border` reports "unknown" rather than
        asserting a wrong answer - see `Location.country_known`.

        "This id does not exist" is a claim only the *network* is allowed to
        make. Cache content never justifies it: a real branch was once
        reported as non-existent for a week because an unrelated payload sat
        under its cache key, so the flag below records whether the service was
        actually asked.
        """
        asked_service = False

        def fetch() -> Any:
            nonlocal asked_service
            asked_service = True
            url = f"{DETAIL}/{location_id}?type=both"
            return self.transport.get_json(url, self._detail_headers())

        reachable = True
        try:
            raw = cache.get_or_fetch(
                "locations", f"id:{location_id}", fetch,
                enabled=self.use_cache, validate=_is_branch_detail,
            )
        except EnterpriseError:
            # Could not ask. That is different from being told the id is bad.
            raw, reachable = None, False

        node = (raw or {}).get("location") if isinstance(raw, dict) else None
        if isinstance(node, dict) and node.get("id"):
            return Location.parse(
                node, str(node.get("location_type") or "branch").lower()
            )

        if reachable and not asked_service:
            # Belt and braces: the cache returned something unusable that the
            # validator should have rejected. Ask the service once, uncached,
            # rather than convicting the id on cache content.
            try:
                raw = fetch()
            except EnterpriseError:
                raw, reachable = None, False
            node = raw.get("location") if isinstance(raw, dict) else None
            if isinstance(node, dict) and node.get("id"):
                return Location.parse(
                    node, str(node.get("location_type") or "branch").lower()
                )

        if reachable:
            # The service answered and does not know this id. Inventing a
            # branch here would produce a confident quote for somewhere that
            # does not exist.
            raise LocationNotFound(location_id)

        # Service unreachable: carry the id with an explicitly unknown country
        # rather than guessing one, so cross-border checks stay conservative.
        return Location(
            id=location_id, name=f"Location {location_id}", kind="branch",
            location_type="BRANCH", airport_code=None, city=None,
            country=None, currency=None, latitude=None, longitude=None,
        )

    def country_mismatch(
        self, location: Location, expected: str | None = None
    ) -> str | None:
        """The branch's country code when it is *not* the one asked for.

        Enterprise has no Australian airport branches, so searching
        `Sydney Airport` with `countryCode=AU` returns Sydney, Nova Scotia -
        a confident single match, in the wrong hemisphere, priced in CAD. The
        search host treats `countryCode` as a hint, so the only defence is to
        check the country that actually came back.

        Returns None when the branch matches, and also when either side is
        unknown - an unknown country is a "cannot tell", not a mismatch, and
        the caller can see it as `location.country is None`.
        """
        expected = (expected if expected is not None else self.country) or ""
        actual = location.country or ""
        if not expected or not actual:
            return None
        return actual.upper() if actual.upper() != expected.upper() else None

    def _detail_headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.enterprise.ca",
            "Referer": "https://www.enterprise.ca/",
            "brand": self.brand,
            "locale": self.locale,
            "channel": "WEB",
            "User-Agent": USER_AGENT,
        }

    def hours(self, location_id: str, from_date: str) -> Any:
        def fetch() -> Any:
            return self._get(
                f"hours/{location_id}",
                {"from": from_date, "cor": self.country, "locale": self.locale},
            )

        return cache.get_or_fetch(
            "hours", f"{location_id}|{from_date}", fetch,
            enabled=self.use_cache, validate=_is_answer,
        )

    def renter_ages(self, location_id: str) -> Any:
        def fetch() -> Any:
            return self._get(
                f"renterage/{location_id}",
                {"cor": self.country, "locale": self.locale},
            )

        return cache.get_or_fetch(
            "renterage", location_id, fetch,
            enabled=self.use_cache, validate=_is_answer,
        )
