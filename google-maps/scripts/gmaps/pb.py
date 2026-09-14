"""Builder for the ``pb`` parameter of the Maps search RPC.

``pb`` is a positional, ``!``-delimited encoding: ``<index><type><value>``.
Only a handful of its fields are meaningful to us; the rest are fixed tokens
that must be present verbatim, because omitting ``6e2`` or ``20e3`` returns an
empty result list rather than an error (see NOTES.md).
"""

from __future__ import annotations

#: Results per page. Google's own frontend uses 20 and offsets are multiples
#: of it; other values work but page 2 then overlaps page 1.
PAGE_SIZE = 20


def search_pb(lat: float, lng: float, span_m: int = 10000,
              page_size: int = PAGE_SIZE, offset: int = 0) -> str:
    """Build the pb for a place search centred on ``lat``/``lng``.

    ``span_m`` is the viewport span in metres — the closest thing this endpoint
    has to a radius. Note the coordinate order: longitude (``2d``) comes before
    latitude (``3d``).
    """
    return (
        f"!4m12!1m3!1d{span_m}!2d{lng}!3d{lat}"
        "!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1"
        f"!7i{page_size}!8i{offset}"
        "!10b1!12m6!2m3!5m1!6e2!20e3!10b1!16b1"
    )
