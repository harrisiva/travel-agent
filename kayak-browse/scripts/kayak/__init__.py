"""kayak-browse — a read-only client for KAYAK's affiliate search APIs.

Read-only by construction: there is no endpoint here that holds, books, pays
for or cancels anything. The API this wraps is a *search* API, and the skill
hands the user a booking link rather than a reservation.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .errors import (  # noqa: F401
    AuthError,
    BudgetError,
    KayakError,
    RateLimited,
    SearchTimeout,
    TransportError,
    UsageError,
)

__all__ = [
    "AuthError", "BudgetError", "KayakError", "RateLimited",
    "SearchTimeout", "TransportError", "UsageError", "__version__",
]
