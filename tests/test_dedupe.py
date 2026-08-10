"""scripts/dedupe.py: the only tool in the repo that deletes rows from papers.csv.

test_no_duplicate_papers.py runs `plan` against the committed table and fails if
it finds anything. That covers the detection and none of the removal -- so the
half that decides *which* row goes, rebinds citations onto the survivor and
rewrites the table had no test at all, on the one script whose mistakes are not
recoverable from the output.

The case that mattered is two rows carrying the identical title. Detection worked
on titles that merely normalized alike ("Same Paper" / "same  paper"), which is
what every earlier test used; a group of the *same* string twice collapsed to one
member and was reported as nothing to fix, while build_bib ranked that row's
entry against itself, declared it the winner and suppressed it -- printing the
arXiv version of a paper whose ACL entry was sitting in the table.

Nothing here reads or writes the real papers.csv, orig.bib or identity store.
"""

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import dedupe

from bib_utils import parse_bibtex
from citations_io import write_citation_rows
from identity import IdentityStore

_COLUMNS = ["Name", "Bib", "Venue", "Authors", "year", "Paper"]


def table(*rows):
    base = {"Bib": "", "Venue": "", "Authors": "", "year": 2023, "Paper": 1}
    return pd.DataFrame([{**base, **r} for r in rows], columns=_COLUMNS)


def entry(key, kind="inproceedings", title="A Paper", year=2023, **fields):
    lines = [f"@{kind}{{{key},", f"  title = {{{title}}},", f"  year = {{{year}}},"]
    lines += [f"  {name} = {{{value}}}," for name, value in fields.items()]
    return "\n".join(lines) + "\n}\n\n"


PUBLISHED = entry("acl2023foo", title="Foo, Revisited", booktitle="Proc. ACL")
PREPRINT = entry("arx2023foo", kind="misc", title="Foo (preprint)",
                 eprint="2301.00001", archivePrefix="arXiv")

# A DOI in the shape `normalize_identifier` recognizes; anything shorter is not
# one, and two entries carrying it are not detected as one paper.
DOI = "10.18653/v1/2023.example-1.29"


def entries(bib_text):
    """The parsed entries by key. `is_preprint` and `_entry_year` read the entry's
    source text, not its fields, so an entry has to be parsed to be ranked."""
    return {e["item_name"]: e for e in parse_bibtex(bib_text)}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """dedupe pointed at an empty world: no citations, no identity store, no table.

    `.written` holds whatever was handed to write_table, so a test can assert on
    the table that would be saved without one existing; `.bound` holds what the
    identity store would have gained; `.run(df, bib)` supplies the table and the
    bibliography and returns main()'s exit code.

    `save` is stubbed as well as `load`: a store handed out by a stubbed `load`
    still saves to the default path, which is the repository's own identity.json,
    and a full `main()` run replaced 1139 lines of harvested crosswalk with the two
    keys of a test fixture.
    """
    class Sandbox:
        written = None

        def __init__(self):
            self.bound = {}

        def run(self, df, bib_text, *argv):
            with open(dedupe.BIB_PATH, "w") as f:
                f.write(bib_text)
            monkeypatch.setattr(dedupe, "read_table", lambda *a, **k: df)
            return dedupe.main(list(argv))

    box = Sandbox()
    monkeypatch.setattr(dedupe, "CITATIONS_CSV", str(tmp_path / "citations.csv"))
    monkeypatch.setattr(dedupe, "BIB_PATH", str(tmp_path / "orig.bib"))
    monkeypatch.setattr(dedupe, "write_table",
                        lambda df, path=None: setattr(box, "written", df))
    monkeypatch.setattr(IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    monkeypatch.setattr(IdentityStore, "save",
                        lambda self, path=None: box.bound.update(self.records))
    return box


# ── two rows, one title ──────────────────────────────────────────────────────
#
# The plainest duplicate there is, and the one every earlier test spelled two
# different ways so as never to produce it.

def test_two_rows_with_the_identical_title_are_one_paper():
    df = table({"Name": "Foo", "Bib": "acl2023foo"},
               {"Name": "Foo", "Bib": "arx2023foo"})
    drops, _unresolved, _suspected = dedupe.plan(df, PUBLISHED + PREPRINT)
    assert len(drops) == 1, drops
    (loser, winner, _why) = drops[0]
    assert winner[1] == "acl2023foo", "the published version must survive"
    assert loser[1] == "arx2023foo"


def test_dropping_one_of_two_identically_titled_rows_keeps_the_other(sandbox):
    """The removal is by row, not by title.

    `df["Name"].isin(dropped)` deletes every row with the dropped row's title,
    and here that is both of them -- the merge deleting the paper it was merging,
    silently, on the only tool that can delete a paper.
    """
    df = table({"Name": "Foo", "Bib": "acl2023foo"},
               {"Name": "Foo", "Bib": "arx2023foo"})
    assert sandbox.run(df, PUBLISHED + PREPRINT) == 0
    kept = sandbox.written
    assert list(kept["Bib"]) == ["acl2023foo"], kept.to_dict("records")


def test_a_row_sharing_a_key_with_another_is_not_its_own_duplicate():
    """Two rows pointing at one entry are one entry, so there is nothing to rank.

    Ranking it against itself makes it its own loser. build_bib's
    `_report_duplicates` raises this as `duplicate-bib-cell`; it needs a table
    fix, not a removal decided by comparing an entry to a copy of itself.
    """
    df = table({"Name": "Foo", "Bib": "acl2023foo"},
               {"Name": "Foo", "Bib": "acl2023foo"})
    drops, _unresolved, _suspected = dedupe.plan(df, PUBLISHED)
    assert [(loser[1], winner[1]) for loser, winner, _ in drops] == []


def test_two_differently_titled_rows_on_one_entry_drop_one_of_them(sandbox):
    """Same paper by construction -- one BibTeX entry -- so one row goes.

    Which one is a coin toss, and the report has to name a *different* row from
    the one it keeps. Both candidates otherwise hold the same entry object, and
    the identity lookup that recovers the row from the winning entry then returns
    the first of them for the winner and again for the loser: "keep [k] Foo /
    remove [k] Foo", having removed the row it said it was keeping.
    """
    df = table({"Name": "Foo, Revisited", "Bib": "acl2023foo"},
               {"Name": "Foo, revisited!", "Bib": "acl2023foo"})
    drops, _unresolved, _suspected = dedupe.plan(df, PUBLISHED)
    assert len(drops) == 1, drops
    loser, winner, _why = drops[0]
    assert loser[0] != winner[0], f"named the same row twice: {loser} / {winner}"
    assert sandbox.run(df, PUBLISHED) == 0
    assert list(sandbox.written["Name"]) == [winner[0]]


# ── what the plan reports rather than applies ────────────────────────────────

def test_a_group_with_only_one_entry_is_reported_unresolved(sandbox, capsys):
    """Nothing to compare, so nothing to choose between: the rows are named, not
    ranked. Removing on the strength of one side's metadata is how the wrong row
    goes."""
    df = table({"Name": "Foo, Revisited", "Bib": "acl2023foo"},
               {"Name": "foo revisited", "Bib": ""})
    drops, unresolved, _suspected = dedupe.plan(df, PUBLISHED)
    assert drops == []
    assert len(unresolved) == 1
    assert {(key, has_entry) for _n, key, has_entry in unresolved[0]} == {
        ("acl2023foo", True), ("", False)}
    assert sandbox.run(df, PUBLISHED) == 0
    assert sandbox.written is None
    out = capsys.readouterr().out
    assert "cannot be ranked" in out and "no entry" in out


def test_an_identifier_recorded_in_the_store_groups_two_rows(sandbox, monkeypatch):
    """The store's own crosswalk, which is the detector that survives a retitling.

    Neither entry need carry the identifier: the arXiv ID was recorded against
    both keys when step 3 resolved them, and that is the only thing left
    connecting "Benchmark Agreement Testing Done Right" to "Do These LLM
    Benchmarks Agree".
    """
    store = IdentityStore({"acl2023foo": {"arxiv": "2301.00001"},
                           "arx2023foo": {"arxiv": "2301.00001"}})
    monkeypatch.setattr(IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    bib = (entry("acl2023foo", title="Benchmark Agreement Testing Done Right",
                 booktitle="Proc. ACL")
           + entry("arx2023foo", kind="misc", title="Do These LLM Benchmarks Agree"))
    df = table({"Name": "Benchmark Agreement Testing Done Right", "Bib": "acl2023foo"},
               {"Name": "Do These LLM Benchmarks Agree", "Bib": "arx2023foo"})
    drops, _unresolved, _suspected = dedupe.plan(df, bib)
    assert [(loser[1], winner[1]) for loser, winner, _ in drops] == [
        ("arx2023foo", "acl2023foo")]


def test_a_title_crossing_is_reported_but_never_removed(sandbox, capsys):
    """One row named what another row's entry is titled: either a duplicate or a
    row resolved to somebody else's paper, and the two need opposite fixes."""
    bib = (entry("slonim2021autonomous", title="An autonomous debating system")
           + entry("aharoni2021isaim", title="Project Debater", booktitle="ISAIM"))
    df = table({"Name": "Project Debater", "Bib": "slonim2021autonomous"},
               {"Name": "Autonomous debating", "Bib": "aharoni2021isaim"})
    drops, _unresolved, suspected = dedupe.plan(df, bib)
    assert drops == []
    assert len(suspected) == 1
    assert sandbox.run(df, bib) == 0
    assert sandbox.written is None, "reported groups must not be written"
    assert "not removing them" in capsys.readouterr().out


def test_nothing_to_do_says_so_and_writes_nothing(sandbox, capsys):
    df = table({"Name": "Foo, Revisited", "Bib": "acl2023foo"})
    assert sandbox.run(df, PUBLISHED) == 0
    assert sandbox.written is None
    assert "No duplicate rows to remove" in capsys.readouterr().out


def test_dry_run_reports_the_merge_without_making_it(sandbox, capsys):
    df = table({"Name": "Foo", "Bib": "acl2023foo"},
               {"Name": "Foo", "Bib": "arx2023foo"})
    assert sandbox.run(df, PUBLISHED + PREPRINT, "--dry-run") == 0
    assert sandbox.written is None
    out = capsys.readouterr().out
    assert "table not modified" in out
    assert "keep   [acl2023foo]" in out and "remove [arx2023foo]" in out


# ── the citations the merge moves ────────────────────────────────────────────

def _citations(path, *rows):
    """Write citations.csv through the real writer, so the format is the real one."""
    write_citation_rows([{"title": title, "citations": count, "year": "2023",
                          "authors": "", "venue": "", "scholar_id": sid}
                         for title, count, sid in rows], path)


def test_a_merge_that_would_lose_citations_is_refused(sandbox, capsys):
    """The dropped row can be the only title a Scholar record matches. Then its
    citations land nowhere, and the count on the CV silently falls."""
    bib = (entry("acl2023bar", title="Bar, Revisited", booktitle="Proc. ACL",
                 doi=DOI)
           + entry("arx2023bar", kind="misc", title="Bar (preprint)",
                   eprint="2301.00001", archivePrefix="arXiv", doi=DOI))
    df = table({"Name": "Bar, Revisited", "Bib": "acl2023bar"},
               {"Name": "Bar, A Wholly Different Spelling", "Bib": "arx2023bar"})
    _citations(dedupe.CITATIONS_CSV,
               ("Bar, Revisited", 7, ""),
               ("Bar, A Wholly Different Spelling", 15, ""))
    assert sandbox.run(df, bib) == 1
    assert sandbox.written is None, "refused and wrote anyway"
    out = capsys.readouterr().out
    assert "REFUSING to remove" in out and "15 citations" in out


def test_a_merge_that_keeps_every_citation_goes_through(sandbox, capsys):
    df = table({"Name": "Foo", "Bib": "acl2023foo"},
               {"Name": "Foo", "Bib": "arx2023foo"})
    _citations(dedupe.CITATIONS_CSV, ("Foo", 9, "sid-1"))
    assert sandbox.run(df, PUBLISHED + PREPRINT) == 0
    assert list(sandbox.written["Bib"]) == ["acl2023foo"]
    assert "9 before, 9 after — unchanged" in capsys.readouterr().out


def test_citation_effect_is_silent_with_no_citations_file(sandbox):
    """A fork has no citations.csv until its first fetch, and dedupe still has to
    run: there is no citation to lose, so there is nothing to check."""
    df = table({"Name": "Foo", "Bib": "acl2023foo"})
    assert dedupe.citation_effect(df, {("Foo", "acl2023foo")}) == ({}, [])


def test_the_dropped_row_s_scholar_id_follows_the_merge(sandbox):
    """Otherwise the merge undoes itself. The record was matched on the title that
    just left the table, so the next fetch has nothing to match it to and the
    paper's citations read zero until somebody re-matches it by hand."""
    df = table({"Name": "Foo, Revisited", "Bib": "acl2023foo"},
               {"Name": "Foo Revisited!", "Bib": "arx2023foo"})
    _citations(dedupe.CITATIONS_CSV, ("Foo Revisited!", 12, "sid-42"))
    drops, _unresolved, _suspected = dedupe.plan(df, PUBLISHED + PREPRINT)
    assert dedupe.bind_dropped_scholar_ids(df, drops) == 1
    assert sandbox.bound["acl2023foo"]["scholar_id"] == "sid-42"


def test_binding_is_skipped_with_no_citations_file(sandbox):
    df = table({"Name": "Foo", "Bib": "acl2023foo"})
    assert dedupe.bind_dropped_scholar_ids(df, []) == 0


# ── the reason printed for the removal ───────────────────────────────────────
#
# It is the only thing the person reading the output has to check the decision
# against, so it has to be the comparison that was actually made.

def test_the_reason_names_the_published_version():
    by_key = entries(PUBLISHED + PREPRINT)
    why = dedupe._why(by_key["acl2023foo"], by_key["arx2023foo"])
    assert "published version" in why and "@misc" in why


def test_between_two_preprints_the_reason_is_the_year():
    """`choose_published` prefers the newer preprint because it carries the
    current title. Reporting the rank instead printed "@misc rank -12 vs @misc at
    -12", which explains nothing and looks like a bug."""
    by_key = entries(
        entry("v1", kind="misc", year=2024, eprint="2301.1", archivePrefix="arXiv")
        + entry("v2", kind="misc", year=2025, eprint="2301.1", archivePrefix="arXiv"))
    assert "2025 is the current version" in dedupe._why(by_key["v2"], by_key["v1"])


def test_otherwise_the_reason_is_the_rank():
    by_key = entries(entry("a", kind="article", journal="Nature")
                     + entry("b", booktitle="A Workshop"))
    assert "ranks" in dedupe._why(by_key["a"], by_key["b"])


# ── a tie between equivalent entries ─────────────────────────────────────────

def test_a_tie_keeps_the_row_citations_are_bound_to(monkeypatch):
    """Two records of one published version tie, and `choose_published` breaks the
    tie on content length -- so a DBLP entry wins by its `bibsource` boilerplate.
    Keeping the row with the Scholar ID instead means the citations need no
    rebinding at all."""
    store = IdentityStore({"hand2023foo": {"scholar_id": "sid-1"}})
    monkeypatch.setattr(IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    bib = (entry("hand2023foo", title="Foo", booktitle="ACL", doi=DOI)
           + entry("DBLP:conf/acl/Foo23", title="Foo", booktitle="ACL",
                   doi=DOI, bibsource="dblp computer science bibliography",
                   timestamp="Mon, 01 Jan 2024 00:00:00 +0100"))
    df = table({"Name": "Foo, by hand", "Bib": "hand2023foo"},
               {"Name": "Foo, from DBLP", "Bib": "DBLP:conf/acl/Foo23"})
    drops, _unresolved, _suspected = dedupe.plan(df, bib)
    assert len(drops) == 1, drops
    loser, winner, why = drops[0]
    assert winner[1] == "hand2023foo", "kept the row with no Scholar ID bound"
    assert loser[1] == "DBLP:conf/acl/Foo23"
    assert "Scholar citations are bound to" in why


def test_a_tie_with_neither_row_cited_is_left_to_the_ranking(monkeypatch):
    """No citations to protect, so there is nothing to prefer and the ordinary
    rule decides. Reporting a tiebreak that did not happen would be a lie about
    why the row went."""
    monkeypatch.setattr(IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    bib = (entry("a2023foo", title="Foo", booktitle="ACL", doi=DOI)
           + entry("b2023foo", title="Foo", booktitle="ACL", doi=DOI))
    df = table({"Name": "Foo one", "Bib": "a2023foo"},
               {"Name": "Foo two", "Bib": "b2023foo"})
    drops, _unresolved, _suspected = dedupe.plan(df, bib)
    assert len(drops) == 1
    assert "Scholar citations are bound to" not in drops[0][2]
