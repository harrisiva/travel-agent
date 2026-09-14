"""The `ts=` query protobuf for the entity page (01 §ts). Pure, no I/O.

Schema (field numbers):
  1: 1
  2: { 1: {1:3} per adult; 1: {1:2, 2:<age>} per child; 2: 0 }
  3: { 2: { 2: { 1:{1:y,2:m,3:d}, 2:{1:y,2:m,3:d} }, 3: 1 } }
  5: { 1: { 7: "<ISO currency>" } }
  6: { 1: 1 }
Encoded with the minimal hand-rolled protobuf writer in `ids.py` (varint,
length-delimited), base64url without padding. Lane A owns this file;
test_hotels.py asserts it reproduces every captured `ts` in
evidence/requests.log byte for byte.

Field order matters for the byte-for-byte guarantee: adults before children
inside field 2, and the `2: 0` trailer after all of them — exactly as the
captures are laid out. `checkin=`/`checkout=` URL parameters are ignored by
Google (01 §ts); this string is the only way dates reach the page.

Nothing here validates the stay — `client.validate_stay` refuses the inputs
Google would silently answer with its default stay, and the echo check
catches whatever slips through. This module only refuses what it cannot
encode at all.
"""
from __future__ import annotations

import base64
from datetime import date

from .ids import pb_bytes, pb_message, pb_uint

#: Party-member type codes inside field 2.1 (01 §ts).
_ADULT, _CHILD = 3, 2


def _date(field: int, d: date) -> bytes:
    return pb_message(field, pb_uint(1, d.year), pb_uint(2, d.month), pb_uint(3, d.day))


def encode_ts(checkin: date, checkout: date, adults: int, child_ages: list[int], currency: str) -> str:
    if isinstance(adults, bool) or not isinstance(adults, int) or adults < 1:
        raise ValueError(f"adults must be a positive integer, got {adults!r}")
    ages = list(child_ages or [])
    if any(isinstance(a, bool) or not isinstance(a, int) or a < 0 for a in ages):
        raise ValueError(f"child ages must be non-negative integers, got {ages!r}")
    if not isinstance(currency, str) or not currency.isascii() or not currency.isalpha():
        raise ValueError(f"currency must be an ISO code, got {currency!r}")

    party = b"".join(pb_message(1, pb_uint(1, _ADULT)) for _ in range(adults))
    party += b"".join(pb_message(1, pb_uint(1, _CHILD), pb_uint(2, age)) for age in ages)
    party += pb_uint(2, 0)

    dates = pb_message(2, pb_message(2, _date(1, checkin), _date(2, checkout)), pb_uint(3, 1))

    raw = (
        pb_uint(1, 1)
        + pb_bytes(2, party)
        + pb_bytes(3, dates)
        + pb_message(5, pb_message(1, pb_bytes(7, currency.encode("ascii"))))
        + pb_message(6, pb_uint(1, 1))
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
