"""Client-side filtering, and the count of what each filter threw away.

Almost every question worth asking is answered here rather than by the API.
`SearchResultsParameters` offers only price mode, sort, paging and currency —
there is no "SUV only" or "unlimited mileage only" parameter — so the client
asks for everything and narrows it locally. That is why `client.py` always
requests 500 rows: filtering out of a price-sorted 50 would return a confident
"none available" that is simply wrong.

Two design rules:

* **A predicate is a named pure function of one `Offer`.** No lambdas defined
  in `cli.py`, no filtering inside the client or the formatter. That keeps each
  one a two-line unit test and keeps the registry enumerable, so the test suite
  can parameterise over every filter that exists rather than over the ones
  somebody remembered to list.
* **A missing field excludes the row, and the exclusion is counted.** An offer
  with no `passengers` cannot satisfy `--min-passengers 5`, so it goes; but it
  goes *visibly*. "0 matched" is a dead end, while "0 matched — passengers
  dropped 41, unlimited-mileage dropped 12" tells the user which constraint to
  relax. That counter is the difference between a tool and an oracle.

The one deliberate asymmetry is `--exclude-opaque`. An offer whose agency code
is missing from the response's `agencies` map has an *unknown* type, not an
opaque one, and dropping it would silently punish a lookup failure. Absence of
evidence is not evidence of opacity, so unknown types survive the filter.
"""

from __future__ import annotations

from typing import Callable, Iterable, NamedTuple

from .model import CAVEATED_AGENCY_TYPES, Offer, SLEEPABLE_GROUPS

Predicate = Callable[[Offer], bool]

#: Passenger floor implied by `--sleepable`. A two-seat SUV exists and is not
#: what anybody means by "big enough to sleep in".
SLEEPABLE_MIN_PASSENGERS = 4


class Filter(NamedTuple):
    """A predicate plus the flag that produced it, as the user typed it.

    The label is what appears in `filtered_out`, so it must read back as
    something the user can act on: "--min-passengers 5", not "min_passengers".
    """

    label: str
    predicate: Predicate


class Outcome(NamedTuple):
    kept: tuple[Offer, ...]
    rejected: dict[str, int]
    parsed: int


# --------------------------------------------------------------- predicates
#
# Each factory returns a closure over exactly the values the flag supplied.
# They are registered below so tests can enumerate them.


def car_groups(groups: Iterable[str]) -> Predicate:
    """Any of the named CarTypeGroup values (`suv`, `van`, ...)."""
    wanted = {g.strip().lower() for g in groups if g.strip()}

    def match(offer: Offer) -> bool:
        return bool(wanted.intersection(g.lower() for g in offer.car.groups))

    return match


def min_passengers(count: int) -> Predicate:
    def match(offer: Offer) -> bool:
        return offer.car.passengers is not None and offer.car.passengers >= count

    return match


def min_bags(count: int) -> Predicate:
    def match(offer: Offer) -> bool:
        return offer.car.bags is not None and offer.car.bags >= count

    return match


def min_doors(count: int) -> Predicate:
    """Compares the *minimum* of the door range.

    `doors23` means the agency may hand over a two-door car, so it must not
    satisfy a request for four. See DOORS_RANGES in model.py for why this is
    never done with arithmetic on the code string.
    """

    def match(offer: Offer) -> bool:
        low = offer.car.doors_min
        return low is not None and low >= count

    return match


def transmission(kind: str) -> Predicate:
    wanted = kind.strip().lower()

    def match(offer: Offer) -> bool:
        return offer.car.transmission.lower() == wanted

    return match


def fuels(kinds: Iterable[str]) -> Predicate:
    wanted = {k.strip().lower() for k in kinds if k.strip()}

    def match(offer: Offer) -> bool:
        return offer.car.fuel.lower() in wanted

    return match


def unlimited_mileage() -> Predicate:
    def match(offer: Offer) -> bool:
        return offer.policy.unlimited_mileage is True

    return match


def free_cancellation() -> Predicate:
    def match(offer: Offer) -> bool:
        return offer.free_cancellation

    return match


def cancel_window(hours: float) -> Predicate:
    """Free cancellation still available at least `hours` before pickup."""

    def match(offer: Offer) -> bool:
        if not offer.free_cancellation:
            return False
        limit = offer.policy.cancel_hours
        return limit is not None and limit >= hours

    return match


def no_credit_card() -> Predicate:
    """Only offers that state a card is *not* required.

    `None` means the API did not say, and "did not say" is not "not required" —
    a debit-only traveller turned away at the counter is exactly the harm this
    flag exists to prevent, so unknowns are excluded.
    """

    def match(offer: Offer) -> bool:
        return offer.credit_card_required is False

    return match


def exclude_agency_type(kind: str) -> Predicate:
    """Drop opaque or peer-to-peer offers — but never for an unknown type."""
    unwanted = kind.strip().lower()

    def match(offer: Offer) -> bool:
        known = offer.agency_type.strip().lower()
        return known != unwanted

    return match


def agencies(names: Iterable[str]) -> Predicate:
    wanted = {n.strip().lower() for n in names if n.strip()}

    def match(offer: Offer) -> bool:
        return (offer.agency_name.lower() in wanted
                or offer.agency_code.lower() in wanted)

    return match


def max_price(amount: float) -> Predicate:
    """Trip total at or under `amount`.

    Compares the *total*, not the raw figure, so a per-day price cannot slip
    under a total budget. An offer whose total is unknowable is excluded: an
    unknown price cannot be shown to be within budget.
    """

    def match(offer: Offer) -> bool:
        total = offer.price.total()
        return total is not None and total <= amount

    return match


def sleepable() -> Predicate:
    """SUV or van with room for four — the car-camping question."""

    def match(offer: Offer) -> bool:
        return bool(SLEEPABLE_GROUPS.intersection(offer.car.groups)) and (
            offer.car.passengers is not None
            and offer.car.passengers >= SLEEPABLE_MIN_PASSENGERS
        )

    return match


#: Every filter the CLI can build, keyed by the flag that builds it. The value
#: is (factory, takes_argument). Tests parameterise over this, so a filter
#: added here is covered without anyone remembering to add a test for it.
REGISTRY: dict[str, tuple[Callable[..., Predicate], bool]] = {
    "--type": (car_groups, True),
    "--min-passengers": (min_passengers, True),
    "--min-bags": (min_bags, True),
    "--min-doors": (min_doors, True),
    "--transmission": (transmission, True),
    "--fuel": (fuels, True),
    "--unlimited-mileage": (unlimited_mileage, False),
    "--free-cancellation": (free_cancellation, False),
    "--cancel-window": (cancel_window, True),
    "--no-credit-card": (no_credit_card, False),
    "--exclude-opaque": (lambda: exclude_agency_type("opaque"), False),
    "--exclude-p2p": (lambda: exclude_agency_type("p2p"), False),
    "--agency": (agencies, True),
    "--max-price": (max_price, True),
    "--sleepable": (sleepable, False),
}


def build(args) -> list[Filter]:
    """Turn parsed arguments into labelled filters, in flag order.

    Labels carry the value so the rejection report is actionable:
    "--min-passengers 7 dropped 41" rather than "min passengers dropped 41".
    """
    built: list[Filter] = []

    def add(label: str, predicate: Predicate) -> None:
        built.append(Filter(label, predicate))

    if getattr(args, "type", None):
        add(f"--type {','.join(args.type)}", car_groups(args.type))
    if getattr(args, "min_passengers", None):
        add(f"--min-passengers {args.min_passengers}",
            min_passengers(args.min_passengers))
    if getattr(args, "min_bags", None):
        add(f"--min-bags {args.min_bags}", min_bags(args.min_bags))
    if getattr(args, "min_doors", None):
        add(f"--min-doors {args.min_doors}", min_doors(args.min_doors))
    if getattr(args, "transmission", None):
        add(f"--transmission {args.transmission}", transmission(args.transmission))
    if getattr(args, "fuel", None):
        add(f"--fuel {','.join(args.fuel)}", fuels(args.fuel))
    if getattr(args, "unlimited_mileage", False):
        add("--unlimited-mileage", unlimited_mileage())
    if getattr(args, "free_cancellation", False):
        add("--free-cancellation", free_cancellation())
    if getattr(args, "cancel_window", None):
        add(f"--cancel-window {args.cancel_window}",
            cancel_window(args.cancel_window))
    if getattr(args, "no_credit_card", False):
        add("--no-credit-card", no_credit_card())
    if getattr(args, "exclude_opaque", False):
        add("--exclude-opaque", exclude_agency_type("opaque"))
    if getattr(args, "exclude_p2p", False):
        add("--exclude-p2p", exclude_agency_type("p2p"))
    if getattr(args, "agency", None):
        add(f"--agency {','.join(args.agency)}", agencies(args.agency))
    if getattr(args, "max_price", None):
        add(f"--max-price {args.max_price}", max_price(args.max_price))
    if getattr(args, "sleepable", False):
        add("--sleepable", sleepable())
    return built


def apply(offers: Iterable[Offer], filters: list[Filter]) -> Outcome:
    """Narrow `offers`, counting what each filter removed.

    An offer is attributed to the *first* filter that rejects it, so the counts
    partition the input exactly: `kept + sum(rejected) == parsed`. That
    invariant is asserted in the test suite, because a filter that drops rows
    without reporting them turns a helpful empty result back into a dead end.
    """
    kept: list[Offer] = []
    rejected: dict[str, int] = {}
    parsed = 0

    for offer in offers:
        parsed += 1
        for flag, predicate in filters:
            if not predicate(offer):
                rejected[flag] = rejected.get(flag, 0) + 1
                break
        else:
            kept.append(offer)

    return Outcome(tuple(kept), rejected, parsed)
