"""Client-side vehicle filters.

All filtering happens here, in Python, over an already-fetched list. The API's
`applied_vehicle_class_filters` field is accepted and echoed back but is inert
server-side - every quote returns the full fleet regardless - so filtering is
local and costs nothing.

Filters are declared once in `SPECS` and wired into argparse by
`add_filter_flags`. Adding a filter means adding one tuple entry; there is no
if-chain to extend and no command to remember to update.

Note the deliberate split in flag semantics, which SKILL.md documents:

* `--seats` / `--bags` / `--max-price` are **thresholds** (at least this many
  seats, at most this much money).
* `--class` / `--fuel` / `--drive` are **substring matches** on the API's own
  descriptions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from . import model
from .model import Vehicle

Predicate = Callable[[Vehicle], bool]


def _amount_arg(text: str) -> "Decimal":
    """Money argument. Named so argparse's error reads sensibly."""
    try:
        return model.parse_amount(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


_amount_arg.__name__ = "amount"


def _text_match(needle: str, haystack: str | None) -> bool:
    """Case-insensitive substring, False when the API omitted the value.

    Absence is not a negative answer: `DRIVE` is missing entirely on several
    premium classes, so a naive `!= "4 Wheel Drive"` would wrongly include
    them.
    """
    return haystack is not None and needle.strip().lower() in haystack.lower()


# Filters match on the API's facet CODES, never on its descriptions. The
# descriptions are translated - at a German branch `Cars` comes back as
# `Mietwagen`, `Vans` as `Kleinbusse` and all-wheel drive as `Vierrad- oder
# Allradantrieb` - so a substring filter on the text silently matched nothing
# outside English while the vehicles were sitting right there. Codes are the
# same in every locale.

#: What a user may type -> the class code(s) it means.
#:
#: The code set is stable worldwide but its MEANING is market-specific, which a
#: Canada-only sample hides completely:
#:
#:   Frankfurt  100 Mietwagen   300 SUVs  400 Kleinbusse     500 Transporter
#:   Madrid     100 Coches      300 SUVs  400 Monovolumenes  500 Furgonetas
#:   Halifax    100 Cars        300 SUVs  (no 400)           500 Vans
#:   Tokyo      100 Cars        300 SUVs  (no 400)           500 Vans
#:
#: So in Europe a passenger people-carrier is 400 and 500 is a cargo van, while
#: in Canada and Japan there is no 400 at all and 500 IS the passenger van. A
#: fixed "van -> 500" therefore returns cargo vans in Germany and misses every
#: minivan. "van" matches both codes and lets the SEAT/BAG columns settle it;
#: "minibus" and "cargo" are there for anyone who needs to be precise.
_CLASS_ALIASES: dict[str, frozenset[str]] = {
    "car": frozenset({model.CLASS_CARS}),
    "cars": frozenset({model.CLASS_CARS}),
    "truck": frozenset({model.CLASS_TRUCKS}),
    "trucks": frozenset({model.CLASS_TRUCKS}),
    "pickup": frozenset({model.CLASS_TRUCKS}),
    "suv": frozenset({model.CLASS_SUVS}),
    "suvs": frozenset({model.CLASS_SUVS}),
    "van": frozenset({model.CLASS_MINIBUS, model.CLASS_VANS}),
    "vans": frozenset({model.CLASS_MINIBUS, model.CLASS_VANS}),
    "minivan": frozenset({model.CLASS_MINIBUS, model.CLASS_VANS}),
    "minibus": frozenset({model.CLASS_MINIBUS}),
    "people carrier": frozenset({model.CLASS_MINIBUS}),
    "cargo": frozenset({model.CLASS_VANS}),
    "transporter": frozenset({model.CLASS_VANS}),
}

_DRIVE_ALIASES: dict[str, str] = {
    "awd": model.DRIVE_AWD, "4wd": model.DRIVE_AWD, "4x4": model.DRIVE_AWD,
    "all-wheel": model.DRIVE_AWD, "all wheel": model.DRIVE_AWD,
    "four-wheel": model.DRIVE_AWD,
    "2wd": model.DRIVE_2WD, "fwd": model.DRIVE_2WD, "rwd": model.DRIVE_2WD,
    "two-wheel": model.DRIVE_2WD, "two wheel": model.DRIVE_2WD,
}

_FUEL_ALIASES: dict[str, str] = {
    "petrol": model.FUEL_PETROL, "gas": model.FUEL_PETROL,
    "gasoline": model.FUEL_PETROL,
    "diesel": model.FUEL_DIESEL,
    "hybrid": model.FUEL_HYBRID,
    "electric": model.FUEL_ELECTRIC, "ev": model.FUEL_ELECTRIC,
}


def _by_code(
    aliases: dict[str, str], value: str, code_of: Callable[[Vehicle], str | None],
    text_of: Callable[[Vehicle], str | None],
) -> Predicate:
    """Match a code when the word is one we know, else fall back to text.

    The fallback keeps unusual spellings working (and any sub-category the
    catalogue does not code), but the code path is what makes the common
    filters locale-proof.
    """
    wanted = aliases.get(value.strip().lower())
    if wanted is None:
        return lambda v: _text_match(value, text_of(v))
    return lambda v: code_of(v) == wanted


def _category(value: str) -> Predicate:
    wanted = _CLASS_ALIASES.get(value.strip().lower())
    if wanted is None:
        # Not a class word - try the sub-category text, which is how someone
        # asks for a "Jeep" or a "Kompakt-SUV".
        return lambda v: _text_match(value, v.sub_category) or _text_match(
            value, v.category
        )
    return lambda v: v.category_code in wanted


def _drive(value: str) -> Predicate:
    return _by_code(_DRIVE_ALIASES, value, lambda v: v.drive_code, lambda v: v.drive)


def _fuel(value: str) -> Predicate:
    return _by_code(_FUEL_ALIASES, value, lambda v: v.fuel_code, lambda v: v.fuel)


#: Half of Europe's fleet is manual, and the two cheapest cars at Frankfurt are
#: stick shifts - so a North American client needs to be able to exclude them.
_TRANSMISSION_ALIASES: dict[str, str] = {
    "auto": "25", "automatic": "25",
    "manual": "26", "stick": "26", "stickshift": "26", "stick shift": "26",
}


def _transmission(value: str) -> Predicate:
    return _by_code(
        _TRANSMISSION_ALIASES, value,
        lambda v: v.transmission_code, lambda v: v.transmission,
    )


def _sleepable(_: Any) -> Predicate:
    """SUV or van, all-wheel drive, 5+ seats, unlimited mileage.

    The composite behind the road-trip question - a vehicle you can fold flat
    and sleep in, that will not bill you per kilometre for doing it. All four
    tests are code- or number-based, so this works at a German branch too.
    """

    def predicate(v: Vehicle) -> bool:
        return (
            v.is_big_enough_to_sleep_in
            and v.is_awd
            and v.seats >= 5
            and v.unlimited_mileage
        )

    return predicate


@dataclass(frozen=True)
class FilterSpec:
    flag: str
    build: Callable[[Any], Predicate]
    help: str
    type: Any = None
    action: str | None = None
    metavar: str | None = None

    @property
    def dest(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")


SPECS: tuple[FilterSpec, ...] = (
    FilterSpec(
        "--class", _category, "vehicle class or sub-category (suv, van, car, truck)",
        type=str, metavar="NAME",
    ),
    FilterSpec(
        "--drive", _drive, "drive type: awd/4wd or 2wd", type=str, metavar="TYPE",
    ),
    FilterSpec(
        "--fuel", _fuel,
        "fuel type: hybrid, electric, petrol/gas, diesel", type=str, metavar="TYPE",
    ),
    FilterSpec(
        "--transmission", _transmission,
        "transmission: automatic or manual", type=str, metavar="TYPE",
    ),
    FilterSpec(
        "--seats", lambda n: (lambda c: c.seats >= n),
        "at least this many seats", type=int, metavar="N",
    ),
    FilterSpec(
        "--bags", lambda n: (lambda c: c.bags >= n),
        "at least this much luggage capacity", type=int, metavar="N",
    ),
    FilterSpec(
        "--max-price", lambda x: (lambda c: c.total is not None and c.total.sort_key <= x),
        "trip total at or below this, in the branch's BILLING currency",
        type=_amount_arg, metavar="AMOUNT",
    ),
    FilterSpec(
        "--unlimited-mileage", lambda _: (lambda c: c.unlimited_mileage),
        "only classes with unlimited mileage", action="store_true",
    ),
    FilterSpec(
        "--sleepable", _sleepable,
        "AWD SUV/van, 5+ seats, unlimited mileage - a road-trip vehicle you "
        "can sleep in", action="store_true",
    ),
)


def add_filter_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("vehicle filters")
    for spec in SPECS:
        kwargs: dict[str, Any] = {"help": spec.help, "default": None}
        if spec.action:
            kwargs["action"] = spec.action
        else:
            kwargs["type"] = spec.type
            if spec.metavar:
                kwargs["metavar"] = spec.metavar
        group.add_argument(spec.flag, **kwargs)


def build(args: argparse.Namespace) -> list[Predicate]:
    """Predicates for the flags actually supplied, in declaration order."""
    predicates: list[Predicate] = []
    for spec in SPECS:
        value = getattr(args, spec.dest, None)
        if value is None or value is False:
            continue
        predicates.append(spec.build(value))
    return predicates


def apply(
    vehicles: Iterable[Vehicle], predicates: Sequence[Predicate]
) -> list[Vehicle]:
    return [v for v in vehicles if all(p(v) for p in predicates)]


def describe(args: argparse.Namespace, **extra: Any) -> str:
    """Human-readable echo of the active filters, for table footers.

    `extra` carries constraints that are not in `SPECS` because they belong to
    one command - `watch --below`, notably. Without it a threshold silently
    excluded classes and the footer named no filter at all, so a reader could
    not tell a sold-out branch from a threshold set too low. On a watch, that
    is the whole decision.
    """
    parts: list[str] = []
    for name, value in extra.items():
        if value is not None and value is not False:
            parts.append(f"{name}={value}")
    for spec in SPECS:
        value = getattr(args, spec.dest, None)
        if value is None or value is False:
            continue
        parts.append(spec.flag.lstrip("-") if value is True else f"{spec.flag.lstrip('-')}={value}")
    return ", ".join(parts)
