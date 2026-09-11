"""Build the ``tfs`` query parameter: hand-rolled protobuf, base64url encoded.

Google Flights encodes the entire query — route, dates, trip type, passengers,
cabin, stop limit — into one opaque URL parameter. There is no documented
schema, so every field below was established empirically by mutating one field
at a time and observing the result set change (the evidence for each is in
NOTES.md). Fields whose meaning is still unknown are emitted verbatim as
``_TAIL``, because omitting them returns an empty payload.

This module is deliberately the *only* place that knows about protobuf. It has
no network calls and no dependencies, so it can be unit-tested offline — which
matters, because this is the part most likely to rot when Google changes the
encoding, and a wrong ``tfs`` produces an empty result set rather than an
error. ``test_flights.py`` asserts that a known query round-trips to a known
byte string for exactly that reason.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date

#: Root field 2 is a constant: the value is 2 on every query that works, and
#: 1, 3 and 4 each return an empty payload. Not a cabin selector, despite
#: looking like one — cabin is field 9.
_CONSTANT_2 = 2

#: Cabin class. Verified on YYZ->LHR, all else held equal: 704 / 2214 / 3771 /
#: 9243 CAD for the four values.
CABINS = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}

#: Trip types, carried in field 19. Verified: flipping 1 -> 2 on an otherwise
#: identical round trip drops the fare from 231 to 98, the genuine one-way
#: price for that route.
ROUND_TRIP, ONE_WAY, MULTI_CITY = 1, 2, 3

#: Passenger type codes, emitted once per traveller in repeated field 8.
#: Verified: emitting field 8 twice took the same trip from 231 to 461.
ADULT, CHILD, INFANT_IN_SEAT, INFANT_ON_LAP = 1, 2, 3, 4

#: Fields 14 and 16, whose meaning has not been established. They are constant
#: across every query observed and removing either empties the response, so
#: they are reproduced byte-for-byte rather than modelled. Field 19 used to be
#: lumped in here, which is how trip type came to be set by accident; it is now
#: emitted explicitly below.
_TAIL = bytes.fromhex("7001" "82010b08ffffffffffffffffff01")

#: Root field 1 is a constant in every working query.
_MAGIC = 28


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def varint_field(field: int, value: int) -> bytes:
    """A protobuf varint field (wire type 0)."""
    return _tag(field, 0) + _varint(value)


def bytes_field(field: int, payload: bytes) -> bytes:
    """A protobuf length-delimited field (wire type 2)."""
    return _tag(field, 2) + _varint(len(payload)) + payload


def string_field(field: int, value: str) -> bytes:
    return bytes_field(field, value.encode("utf-8"))


def _airport(code: str) -> bytes:
    """An endpoint. Field 1 is a kind discriminator; 1 means "airport code"."""
    return varint_field(1, 1) + string_field(2, code)


@dataclass(frozen=True)
class Slice:
    """One directional leg of the query: "YYZ to YHZ on 2026-09-25".

    A round trip is two slices, a multi-city trip is N. ``max_stops`` is
    per-slice, which is how Google models it — not a global setting.
    """

    origin: str
    destination: str
    depart: date
    max_stops: int | None = None

    def encode(self) -> bytes:
        body = string_field(2, self.depart.isoformat())
        if self.max_stops is not None:
            body += varint_field(5, self.max_stops)
        body += bytes_field(13, _airport(self.origin))
        body += bytes_field(14, _airport(self.destination))
        return bytes_field(3, body)


@dataclass(frozen=True)
class Query:
    """A complete Google Flights query, encodable to a ``tfs`` string."""

    slices: tuple[Slice, ...]
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0
    cabin: str = "economy"
    trip_type: int = ROUND_TRIP

    @property
    def passengers(self) -> int:
        return (
            self.adults + self.children + self.infants_in_seat + self.infants_on_lap
        )

    def _passenger_fields(self) -> bytes:
        """Field 8 repeated once per traveller — not a count.

        Two adults is the field emitted twice, not the value 2. Sending the
        value 2 asks for one *child* instead, and Google answers with a
        different fare and no error whatsoever.
        """
        counts = (
            (ADULT, self.adults),
            (CHILD, self.children),
            (INFANT_IN_SEAT, self.infants_in_seat),
            (INFANT_ON_LAP, self.infants_on_lap),
        )
        return b"".join(
            varint_field(8, code) * count for code, count in counts if count
        )

    def encode(self) -> bytes:
        """Field order matches the browser's exactly: 1, 2, slices, 8, 9, 14,
        16, 19. Protobuf does not require it, but matching byte-for-byte is
        what lets the self-check assert against a real captured URL."""
        body = varint_field(1, _MAGIC) + varint_field(2, _CONSTANT_2)
        body += b"".join(s.encode() for s in self.slices)
        body += self._passenger_fields()
        body += varint_field(9, CABINS[self.cabin])
        body += _TAIL
        return body + varint_field(19, self.trip_type)

    def tfs(self) -> str:
        """The value for the ``tfs`` URL parameter."""
        return base64.urlsafe_b64encode(self.encode()).decode().rstrip("=")
