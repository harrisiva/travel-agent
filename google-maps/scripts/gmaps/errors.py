"""Exception hierarchy and the exit codes it maps to.

The codes are the skill's contract with a caller that polls, so the mapping
lives in one place rather than being spelled out at each raise site:

    0  found what was asked for
    1  query succeeded, nothing matched
    2  usage / lookup error
    3  network or API error

The distinction that matters is 1 vs everything else. Only 1 means "keep
waiting". A crash that escapes as exit 1 tells a watch loop to wait forever for
a result that will never come, and tells a user "nothing is open" when the truth
is "the parser broke" — so `cli.main` catches *everything* and maps unknown
exceptions to 3, never to 1.
"""

from __future__ import annotations

FOUND = 0
EMPTY = 1
USAGE = 2
NETWORK = 3
INTERRUPTED = 130


class GmapsError(Exception):
    """Base for every error this package raises deliberately."""

    exit_code = NETWORK


class UsageError(GmapsError):
    """The query cannot be answered as asked: bad flag, unresolvable place."""

    exit_code = USAGE


class NetworkError(GmapsError):
    """No usable bytes came back. Never reported as 'nothing found'."""

    exit_code = NETWORK


class ParseError(GmapsError):
    """Bytes arrived but did not have the shape we know how to read.

    Its own class because it means Google changed something, which is a
    different remedy from a flaky network.
    """

    exit_code = NETWORK
