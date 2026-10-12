"""Fails if the CV would print a title a reader cannot follow.

The bibliography style links each title to its DOI, arXiv page or URL. An entry
with none of the three prints as a dead title, which is how a grant office came to
ask for the arXiv link of a preprint the CV listed without one. Step 3 fills these
in by title lookup; this test is what keeps a new paper from slipping through when
no source has indexed it yet.

Runs against `papers.csv` and `orig.bib`, both committed, so it holds in CI.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bib_utils import parse_bibtex
from link_fill import has_link
from table_io import read_table

BIB_PATH = os.path.join(ROOT, "orig.bib")


def test_every_cited_entry_has_a_link():
    with open(BIB_PATH, encoding="utf-8") as fh:
        entries = {e["item_name"]: e for e in parse_bibtex(fh.read())}

    offenders = []
    for _, row in read_table().iterrows():
        key = str(row.get("Bib") or "").strip()
        if not key or key.lower() in ("nan", "none"):
            continue
        entry = entries.get(key)
        if entry is None:
            continue  # A missing entry is WORKLIST.md's business, not this test's.
        if not has_link(entry["content"]):
            offenders.append(f"{key}\n      row: {str(row.get('Name'))[:70]}")

    assert not offenders, (
        f"{len(offenders)} cited entr(ies) in orig.bib have no doi, eprint or url, "
        f"so the CV prints their titles as dead links. Run `python update.py` to "
        f"look them up, or add the identifier by hand:\n    " + "\n    ".join(offenders))


def test_the_check_would_actually_catch_one():
    """A guard that cannot fail is not a guard."""
    assert not has_link("  title={Something},\n  year={2025},\n")
