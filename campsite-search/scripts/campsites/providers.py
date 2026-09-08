"""Known Canadian campground reservation systems.

Every host in CAMIS_PROVIDERS runs Camis5 and serves the same unauthenticated
JSON API under /api/. Hosts in OTHER_PROVIDERS run different software and are
not supported by CamisClient — they are listed so the gaps stay visible.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    host: str
    name: str


CAMIS_PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in [
        Provider("pc", "reservation.pc.gc.ca", "Parks Canada (national parks)"),
        Provider("ontario", "reservations.ontarioparks.ca", "Ontario Parks"),
        Provider("grca", "www.grcacamping.ca", "Grand River Conservation Authority"),
        Provider("bc", "camping.bcparks.ca", "BC Parks"),
        Provider("manitoba", "manitoba.goingtocamp.com", "Manitoba Parks"),
        Provider("novascotia", "novascotia.goingtocamp.com", "Nova Scotia Parks"),
        Provider("newfoundland", "www.nlcamping.ca", "Newfoundland & Labrador Parks"),
        Provider("yukon", "yukon.goingtocamp.com", "Yukon Parks"),
        # NOTE: the booking system is on reservations.*, not the www marketing
        # site — testing www.parcsnbparks.ca 404s and looks like a non-Camis
        # vendor, which is how this tenant was missed the first time round.
        Provider("newbrunswick", "reservations.parcsnbparks.ca", "New Brunswick Parks"),
    ]
}

# Verified NOT to be Camis5 — /api/resourceLocation returns 404 or a vendor
# error page. Each would need its own client.
OTHER_PROVIDERS: dict[str, str] = {
    "alberta": "shop.albertaparks.ca — Aspira, behind a Queue-it waiting room",
    "saskatchewan": "parks.saskatchewan.ca — Aspira, behind a Queue-it waiting room",
    "pei": "www.peiprovincialparks.ca — Aspira, behind a Queue-it waiting room",
    "quebec": "www.sepaq.com — Sépaq in-house; serves a CAPTCHA challenge (HTTP 403)",
    "nwt": "www.nwtparks.ca — Drupal site, no reservation API at this host",
}


def resolve(key_or_host: str) -> str:
    """Accept either a provider key ('ontario') or a bare host."""
    if key_or_host in CAMIS_PROVIDERS:
        return CAMIS_PROVIDERS[key_or_host].host
    if key_or_host in OTHER_PROVIDERS:
        raise ValueError(
            f"{key_or_host!r} does not run Camis5: {OTHER_PROVIDERS[key_or_host]}"
        )
    if "." in key_or_host:
        return key_or_host
    raise ValueError(
        f"Unknown provider {key_or_host!r}. "
        f"Known: {', '.join(sorted(CAMIS_PROVIDERS))}"
    )
