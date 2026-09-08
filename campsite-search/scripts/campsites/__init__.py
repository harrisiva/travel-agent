"""Search Canadian campground availability via the Camis5 reservation API."""

from .camis import CamisClient, SpanTooLongError
from .http import CamisHTTPError
from .model import (
    MAX_SPAN_DAYS,
    AttributeDef,
    Availability,
    BookingCategory,
    Equipment,
    Opening,
    Park,
    SearchResult,
    Site,
    SiteInfo,
)
from .providers import CAMIS_PROVIDERS, OTHER_PROVIDERS, Provider

__all__ = [
    "AttributeDef",
    "Availability",
    "BookingCategory",
    "CAMIS_PROVIDERS",
    "CamisClient",
    "CamisHTTPError",
    "Equipment",
    "MAX_SPAN_DAYS",
    "OTHER_PROVIDERS",
    "Opening",
    "Park",
    "Provider",
    "SearchResult",
    "Site",
    "SiteInfo",
    "SpanTooLongError",
]
