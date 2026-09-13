#!/usr/bin/env python3
"""Launcher shim: lets `python3 /abs/path/to/ubereats.py …` work from any cwd."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from ueats.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
