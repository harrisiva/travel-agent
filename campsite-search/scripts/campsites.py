#!/usr/bin/env python3
"""Portable entry point for the campsites CLI.

Runnable by absolute path from any working directory:

    python3 <path-to-skill>/scripts/campsites.py <command> ...

It is a thin launcher: it puts its own directory on ``sys.path`` so the
sibling ``campsites/`` package always resolves, then hands off to
``campsites.cli.main``. Behaviour, output and exit codes are identical to
``python3 -m campsites`` run from this directory:

    0  found what was asked for
    1  query succeeded, nothing available
    2  usage / lookup error (bad park name, ambiguous match, unknown provider)
    3  network or API error
"""

from __future__ import annotations

import os
import sys

# realpath, not abspath: stays correct when this file is reached through a
# symlink parked somewhere convenient (e.g. ~/bin/campsites).
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    # Front of the path: the package must win over anything the ambient
    # environment happens to call "campsites".
    sys.path.insert(0, _HERE)

# A directory package always beats a same-named module on the same path
# entry, so this resolves to campsites/ rather than to this file.
from campsites.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
