"""Google Flights search: a read-only client for the public search page.

Public surface:

    from gflights.client import Client, build_query
    from gflights.tfs import Query, Slice

The CLI lives in ``gflights.cli``; ``flights.py`` beside this package is a
launcher that works when invoked by absolute path from any directory.
"""
