#!/usr/bin/env python3
"""Launcher shim: works by absolute path from any working directory.

Puts this file's real directory on sys.path so `ghotels` imports whether the
skill was cloned, symlinked, or unzipped from a .skill bundle.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ghotels.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
