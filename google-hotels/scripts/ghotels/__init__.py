"""google-hotels — price a named hotel for real nights from Google Hotels' own page.

Public surface: `ghotels.cli.main`. Layering (04 §1): raw payload arrays are
read once, in `parse.py`; `client.py` and `cli.py` never index a list.
"""
