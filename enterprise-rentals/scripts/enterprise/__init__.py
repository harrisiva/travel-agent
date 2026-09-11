"""Enterprise rental car pricing - read-only.

Queries the same public JSON APIs the enterprise.ca reservation flow uses. No
API key, no account, no configuration. It prices and compares rentals; it can
never book one.
"""

from __future__ import annotations

__version__ = "1.0.0"
