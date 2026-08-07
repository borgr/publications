"""Fails if the CV would list a paper its owner did not write.

Nothing checked this, and it happened. Step 2 added a table row from the Scholar
record "An autonomous debating system"; step 3 resolved that row against DBLP,
accepted a 2022 ISAIM invited talk by Noam Slonim alone -- a similar title, one of
the real paper's twenty co-authors, not the paper -- and step 4 emitted it. The CV
printed it in Journals, next to the correct Nature entry for the same work.

Every check in the pipeline passed while that was true, because every check was
about titles. Similarity said the titles matched; the duplicate detector saw two
different titles; `choose_published` even preferred the wrong entry, being newer
and having a `booktitle`. The one question none of them asked is the cheap one:
does this entry name the author whose CV this is?

Runs against `papers.csv` and `orig.bib`, both committed, so it holds in CI.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from bib_utils import lists_author, parse_bibtex
from table_io import read_table

BIB_PATH = os.path.join(ROOT, "orig.bib")


def test_every_cited_entry_names_the_author():
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
        if not lists_author(entry["content"], config.AUTHOR_NAME):
            offenders.append(f"{key}\n      row: {str(row.get('Name'))[:70]}")

    assert not offenders, (
        f"{len(offenders)} table row(s) point at a BibTeX entry that does not list "
        f"{config.AUTHOR_NAME}. Either the entry's author list is wrong, or the row "
        f"was resolved to somebody else's paper — check the entry in orig.bib "
        f"before trusting the title:\n    " + "\n    ".join(offenders))


def test_the_check_would_actually_catch_one():
    """A guard that cannot fail is not a guard."""
    entry = "  title = {Something},\n  author = {Noam Slonim},\n  year = {2022},\n"
    assert not lists_author(entry, "Leshem Choshen")


def test_an_edited_volume_with_no_author_field_is_accepted():
    """Proceedings the author edited carry `editor` and nothing else."""
    entry = "  title = {Proceedings of Somewhere},\n  editor = {Leshem Choshen and A B},\n"
    assert lists_author(entry, "Leshem Choshen")
