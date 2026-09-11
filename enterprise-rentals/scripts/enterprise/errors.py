"""Exception types for the Enterprise CLI.

The hierarchy exists so ``cli.main`` can map every failure to an exit code in
one place. Two rules drive the shape:

* Anything that means "this query can never succeed as asked" is a
  ``ValueError`` subclass and exits 2. Age refusals and refused cross-border
  routes live here — reporting them as "nothing available" (exit 1) would make
  a watch loop poll forever for a rental that cannot be booked.
* Anything that means "the request never got a real answer" is a
  ``TransportError`` and exits 3. A 403 HTML block page is a transport
  failure, not an empty result.
"""

from __future__ import annotations


class EnterpriseError(Exception):
    """Base for everything this package raises deliberately."""


# --------------------------------------------------------------------------
# Exit code 2 - the query is malformed, ambiguous, or refused outright.
# --------------------------------------------------------------------------

class UsageError(EnterpriseError, ValueError):
    """Bad input, or a request the API will never satisfy."""


class LocationNotFound(UsageError):
    def __init__(self, query: str) -> None:
        super().__init__(f"no Enterprise location matches {query!r}")
        self.query = query


class AmbiguousLocation(UsageError):
    """More than one branch matched and picking one would be a guess.

    Halifax returns both the regular airport branch and 'Halifax Airport
    Exotic'; silently taking the first gives a different fleet at wildly
    different prices.
    """

    def __init__(self, query: str, candidates: list) -> None:
        lines = "\n".join(
            f"    {c.id:>9}  {c.airport_code or '   '}  {c.name}" for c in candidates
        )
        super().__init__(
            f"{len(candidates)} locations match {query!r} - name one by id:\n{lines}"
        )
        self.query = query
        self.candidates = candidates


class AgeRefused(UsageError):
    """The renter is below the minimum age for this branch (PRICING_4463).

    The response carries a message and *no* ``car_classes`` key at all, which
    is why this is a refusal rather than an empty result.
    """


class RouteRefused(UsageError):
    """A cross-border one-way that came back empty (PRICING_16007).

    The API reports a route it will not permit with the same message it uses
    for a genuine sell-out, so this cannot be distinguished from the outside.
    Raised only when pickup and dropoff countries differ, where a restriction
    is overwhelmingly the likelier cause.
    """


class RequestCeiling(UsageError):
    """The planned fan-out exceeded --max-requests; nothing was sent."""


# --------------------------------------------------------------------------
# Exit code 3 - the network or the API let us down.
# --------------------------------------------------------------------------

class TransportError(EnterpriseError):
    """No usable response, from any transport rung."""


class BotBlocked(TransportError):
    """Every transport was rejected by TLS fingerprinting.

    Distinct from a generic network error because the remedy is completely
    different: the environment's TLS stack is the problem, not connectivity.
    """
