#!/usr/bin/env python3
"""Portable entry point for the enterprise CLI.

Runnable by absolute path from any working directory:

    python3 <path-to-skill>/scripts/enterprise.py <command> ...

A thin launcher: it puts its own directory on ``sys.path`` so the sibling
``enterprise/`` package always resolves, then hands off to
``enterprise.cli.main``. Behaviour, output and exit codes are identical to
``python3 -m enterprise`` run from this directory:

    0  found bookable vehicles
    1  query succeeded, nothing bookable  (the only "keep waiting" code)
    2  usage / lookup error, age refusal, refused route
    3  network or API error
"""

from __future__ import annotations

import os
import sys

# realpath, not abspath: stays correct when this file is reached through a
# symlink parked somewhere convenient.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    # Front of the path: the package must win over anything the ambient
    # environment happens to call "enterprise".
    sys.path.insert(0, _HERE)

# A directory package always beats a same-named module on the same path entry,
# so this resolves to enterprise/ rather than to this file.
from enterprise.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
