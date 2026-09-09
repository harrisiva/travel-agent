"""Exception hierarchy for the KAYAK affiliate client.

One exception class per exit code. `cli.py` is the only place that turns these
into numbers, so the mapping lives in exactly one table and nothing else in the
package has to think about process exit status.

The distinction that matters most is `AuthError` (exit 4) versus `NoResults`
(exit 1). A sandbox key expires every three months, and an expired key that
reported "no cars available" would make a polling agent wait forever for
inventory that was never queried. Compare the Camis5 rule in this repo's
CLAUDE.md: refuse rather than return a false negative.
"""

from __future__ import annotations


class KayakError(Exception):
    """Base for everything this package raises deliberately."""


class UsageError(KayakError):
    """The command was wrong, or would cost more than the caller allowed.

    Exit 2. Retrying without changing the command cannot help.
    """


class BudgetError(UsageError):
    """A planned fan-out exceeds --max-requests or the hourly API quota.

    A subclass of UsageError because the fix is the same: change the command.
    We refuse up front rather than sleeping until quota frees up — an agent
    blocked for fourteen seconds inside a bash timeout is worse than an error
    that names the numbers.
    """


class TransportError(KayakError):
    """Network, TLS, or a non-2xx the caller cannot fix by changing flags.

    Exit 3. A polling agent must stop on this, not keep waiting: "the network
    is down" is not "the car is not available yet".
    """


class RateLimited(TransportError):
    """HTTP 429. Carries `retry_after` seconds when the server supplied it."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(KayakError):
    """No key, a rejected key, or an expired key. Exit 4.

    Never carries the key itself — see `auth.redact`. The message is shown to
    the user and may be echoed into logs.
    """


class SearchTimeout(KayakError):
    """The wall clock ran out before the search reached the requested phase.

    Exit 5. Partial results are still returned and printed; `complete: false`
    in the JSON envelope says so. Distinct from "nothing available" because
    the honest answer is "we did not finish looking", not "there is nothing".
    """

    def __init__(self, message: str, partial=None, status: str = ""):
        super().__init__(message)
        self.partial = partial
        self.status = status
