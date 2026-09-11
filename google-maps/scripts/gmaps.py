#!/usr/bin/env python3
"""Portable entry point for the gmaps CLI.

Runnable by absolute path from any working directory:

    python3 <path-to-skill>/scripts/gmaps.py <command> ...

A thin launcher: it puts its own directory on ``sys.path`` so the sibling
``gmaps/`` package resolves, then hands off to ``gmaps.cli.main``. Behaviour,
output and exit codes match ``python3 -m gmaps`` run from this directory:

    0  found what was asked for
    1  query succeeded, nothing matched
    2  usage / lookup error
    3  network or API error
"""

from __future__ import annotations

import os
import sys

# realpath, not abspath: stays correct when reached through a symlink.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# A directory package beats a same-named module on the same path entry, so
# this resolves to gmaps/ rather than to this file.
from gmaps.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
