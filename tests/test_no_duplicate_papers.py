"""Fails if the committed data would print one paper on the CV twice.

Nothing checked this. `build_bib` warns when two table rows normalize to the same
*title*, and CI fails on that warning -- but two rows for one paper usually do not
share a title. They are the same work entered twice under two spellings, months
apart, and the only thing they reliably have in common is an identifier sitting in
each one's BibTeX entry.

Four papers were being printed twice on the live CV when this test was written:

    10.18653/v1/2023.conll-1.29   DBLP:conf/conll/KaridiCPA23   / karidi2023muler
    10.18653/v1/2022.conll-1.14   DBLP:conf/conll/PatelCA22     / patel2022neurons
    2022.coling-1.401             DBLP:conf/coling/YehudaiCFA22 / yehudai2022reinforcement
    arXiv 2605.29512              wang2026mindgames...evaluating / wang2026mindgames

Each pair had matching authors, venue, pages and DOI, and both halves were cited,
so the CV listed 114 papers for 110 distinct works. It went unnoticed for as long
as it did because every automated check passed: the titles differed, so the table
check was silent, and the identity store had no record for the DBLP-keyed half, so
the identifier check was silent too.

Runs against `papers.csv` and `orig.bib`, both committed, so it works in CI
without the private Overleaf submodule.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import dedupe

from table_io import read_table

BIB_PATH = os.path.join(ROOT, "orig.bib")


def test_no_two_table_rows_are_the_same_paper():
    with open(BIB_PATH, encoding="utf-8") as fh:
        bib_text = fh.read()
    drops, _unresolved, _suspected = dedupe.plan(read_table(), bib_text)

    listed = [f"{loser[1] or '(no key)'} duplicates {winner[1] or '(no key)'}"
              f"\n      {loser[0][:70]}"
              for loser, winner, _why in drops]
    assert not drops, (
        f"{len(drops)} paper(s) are in the table twice, so the CV lists them "
        f"twice. Run `python scripts/dedupe.py --dry-run` to see the merge, then "
        f"without the flag to apply it:\n    " + "\n    ".join(listed))


def test_no_row_is_named_what_another_row_s_entry_is_titled():
    """The weaker signal, which `dedupe` reports but must never act on.

    One row named "An autonomous debating system" and another whose BibTeX entry
    carries that title is either a duplicate or a row resolved to the wrong paper,
    and the two need opposite fixes. Ranking cannot tell them apart -- it picked the
    wrong survivor when this last fired -- so the decision is a person's. What this
    test buys is that the decision gets made rather than deferred forever.
    """
    with open(BIB_PATH, encoding="utf-8") as fh:
        bib_text = fh.read()
    _drops, _unresolved, suspected = dedupe.plan(read_table(), bib_text)

    listed = ["\n      ".join(f"[{k or '(no key)'}] {n[:64]}" for n, k in group)
              for group in suspected]
    assert not suspected, (
        f"{len(suspected)} group(s) of rows share a title through their BibTeX "
        f"entries. Check each in orig.bib: if they are one paper, drop a row; if a "
        f"row was resolved to somebody else's paper, fix the row's Bib key. Run "
        f"`python scripts/dedupe.py --dry-run` for the report:\n    "
        + "\n    ".join(listed))


def test_the_duplicate_detector_would_actually_catch_one():
    """A guard that cannot fail is not a guard.

    Two entries sharing only a DOI -- different keys, different titles, different
    entry types -- which is the shape that slipped past every earlier check.
    """
    bib = (
        "@inproceedings{real2023paper,\n"
        "  title = {A Detailed and Scalable Evaluation},\n"
        "  booktitle = {Proceedings of Somewhere},\n"
        "  year = {2023},\n"
        "  doi = {10.18653/V1/2023.EXAMPLE-1.29},\n"
        "}\n\n"
        "@article{DBLP:journals/x/Real23,\n"
        "  title = {A Rather Differently Remembered Title},\n"
        "  journal = {Some Journal},\n"
        "  year = {2023},\n"
        "  doi = {10.18653/v1/2023.example-1.29},\n"
        "}\n"
    )
    import pandas as pd
    df = pd.DataFrame([
        {"Name": "A Detailed and Scalable Evaluation", "Bib": "real2023paper"},
        {"Name": "A Rather Differently Remembered Title",
         "Bib": "DBLP:journals/x/Real23"},
    ])
    drops, _unresolved, _suspected = dedupe.plan(df, bib)
    assert len(drops) == 1, drops
    # The DOIs differ in case, as DBLP's and the ACL Anthology's do for the same
    # paper; matching them is what `normalize_identifier` is for.
    assert drops[0][1][1] == "real2023paper", "the published version must survive"
