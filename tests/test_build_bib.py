"""Tests for build_bib.py: which section a paper lands in, and what gets emitted.

This is the module that decides what the CV says. It was the least covered thing
in the repo (28%) and the most consequential: every mistake it makes is a wrong
claim on a public document -- a paper filed under Journals because a venue string
truncated to "nature", a paper listed twice because two table rows describe it, a
citation count silently rendered as 0.

Everything here builds its own table and its own BibTeX in memory, so no test
reads the author's real data.
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_bib
import citations_io
from bib_utils import parse_bibtex
from citations_io import STALE_AFTER_DAYS
from identity import IdentityStore

# The flag columns _process_entries reads, plus the tag columns it turns into
# \pretitle macros. Only these matter here; the full schema lives in table_io.
_DEFAULTS = {
    "Paper": 1,
    "Workshop-paper": 0,
    "Review, Survey and Position": 0,
    "Open": 0,
    "Language&Cognition": 0,
    "Enabling Low Budget Research": 0,
    "inter\\eval": 0,
}


_COLUMNS = ["Name", "Bib", "Venue", *_DEFAULTS]


def table(*rows):
    """A publications table with only the columns build_bib reads.

    Always carries its columns, even with no rows: that is what an emptied
    papers.csv looks like, and a column-less frame would make an empty table look
    like a different bug than the one being tested.
    """
    return pd.DataFrame([{**_DEFAULTS, **r} for r in rows], columns=_COLUMNS)


def bib(*entries):
    """BibTeX source for the given (key, extra-fields) pairs."""
    parts = []
    for key, body in entries:
        parts.append(f"@inproceedings{{{key},\n    title = {{T {key}}},{body}\n}}\n")
    return "\n".join(parts)


# --- categorisation ------------------------------------------------------

def test_a_known_journal_and_a_known_conference_are_separated():
    assert build_bib._categorize("tacl", False, False, False) == "journals"
    assert build_bib._categorize("acl", False, False, False) == "conferences"


def test_arxiv_wins_over_every_other_flag():
    """An unpublished preprint is a draft even if it is also a survey."""
    assert build_bib._categorize("tacl", True, True, True) == "drafts"


def test_a_review_outranks_a_workshop():
    assert build_bib._categorize("acl", False, True, True) == "reviews"


def test_a_workshop_paper_is_not_filed_by_its_venue():
    """An ACL workshop paper is not an ACL paper."""
    assert build_bib._categorize("acl", False, False, True) == "workshops"


def test_an_unranked_outlet_is_a_draft_not_an_unknown():
    assert build_bib._categorize("blog", False, False, False) == "drafts"


def test_an_unknown_venue_is_reported_as_unknown():
    """None, not a guess: the caller warns, and the worklist collects it."""
    assert build_bib._categorize("some-venue-nobody-configured",
                                 False, False, False) is None


# --- venue resolution ---------------------------------------------------

def test_a_full_official_name_is_matched_not_truncated():
    key, how = build_bib.venue_resolution(
        "Proceedings of the 62nd Annual Meeting of the Association for "
        "Computational Linguistics (Volume 1: Long Papers)")
    assert how == "matched" and key == "acl"


def test_a_short_form_falls_back_to_truncation():
    """What makes "EMNLP-Findings" resolve at all, at the cost of guessing."""
    assert build_bib.venue_resolution("EMNLP-Findings") == ("emnlp", "truncated")


def test_a_truncation_that_could_misfile_a_paper_is_reported_as_one():
    """"Nature-inspired Computing" truncates to a journal this author has not
    published in. The key is still used -- but "truncated" is what puts it in
    WORKLIST.md instead of silently onto the CV under Journals."""
    assert build_bib.venue_resolution("Nature-inspired Computing") == ("nature",
                                                                      "truncated")


def test_a_digit_in_the_venue_name_does_not_split_it():
    """Splitting on the digit `2` turned "K2 Workshop" into "k"."""
    key, _ = build_bib.venue_resolution("K2 Workshop")
    assert key == "k2 workshop"


def test_an_empty_venue_resolves_to_nothing_quietly():
    assert build_bib.venue_resolution("") == ("", "")
    assert build_bib.venue_resolution(float("nan")) == ("", "")


# --- what lands in Wzmn.bib ---------------------------------------------

def test_a_paper_gets_its_citation_count_and_tags():
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024", "Open": 1})
    parsed = parse_bibtex(bib(("p1", "")))
    out, cats, seen, arxiv_only, non_paper_rows = build_bib._process_entries(
        parsed, df, {"Paper One": 12})
    assert "citations={12}" in out
    assert "pretitle={\\COL}" in out
    assert cats.conferences == ["p1"] and seen == 1
    assert (arxiv_only, non_paper_rows) == (0, 0)


def test_a_matched_paper_with_no_count_renders_zero():
    """Scholar omits the cell for an uncited paper; the field must still exist."""
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"})
    out, *_ = build_bib._process_entries(parse_bibtex(bib(("p1", ""))), df,
                                         {"Paper One": None})
    assert "citations={0}" in out


def test_a_suppressed_key_is_not_emitted():
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"},
               {"Name": "Paper Two", "Bib": "p2", "Venue": "ACL 2024"})
    parsed = parse_bibtex(bib(("p1", ""), ("p2", "")))
    out, cats, seen, _, _ = build_bib._process_entries(parsed, df, {},
                                                       suppressed=("p2",))
    assert "p1" in out and "p2" not in out
    assert cats.conferences == ["p1"] and seen == 1


def test_a_bib_entry_with_no_table_row_is_dropped():
    """orig.bib accumulates resolve attempts; the table is the source of truth."""
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"})
    out, _, seen, _, _ = build_bib._process_entries(
        parse_bibtex(bib(("p1", ""), ("stranger", ""))), df, {})
    assert "stranger" not in out and seen == 1


def test_a_non_paper_is_passed_through_without_citations():
    df = table({"Name": "A Talk", "Bib": "t1", "Venue": "ACL 2024", "Paper": 0})
    out, cats, seen, _, non_papers = build_bib._process_entries(
        parse_bibtex(bib(("t1", ""))), df, {"A Talk": 5})
    assert "citations=" not in out
    assert non_papers == 1 and seen == 1
    assert cats.conferences == [] and cats.drafts == []


def test_a_preprint_counts_as_arxiv_only_and_files_as_a_draft():
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "arXiv preprint"})
    _, cats, _, arxiv_only, _ = build_bib._process_entries(
        parse_bibtex(bib(("p1", ""))), df, {})
    assert cats.drafts == ["p1"] and arxiv_only == 1


def test_duplicate_table_rows_for_one_key_warn_and_use_the_first(capsys):
    df = table({"Name": "First Spelling", "Bib": "p1", "Venue": "ACL 2024"},
               {"Name": "Second Spelling", "Bib": "p1", "Venue": "TACL"})
    _, cats, seen, _, _ = build_bib._process_entries(
        parse_bibtex(bib(("p1", ""))), df, {})
    assert "duplicate table rows" in capsys.readouterr().out
    assert seen == 1 and cats.conferences == ["p1"]


def test_an_unknown_venue_warns_rather_than_omitting_silently(capsys):
    df = table({"Name": "Paper One", "Bib": "p1",
                "Venue": "Journal of Things Nobody Configured"})
    _, cats, _, _, _ = build_bib._process_entries(
        parse_bibtex(bib(("p1", ""))), df, {})
    out = capsys.readouterr().out
    assert "unknown venue" in out and "cannot categorize" in out
    assert cats.drafts == ["p1"], "an uncategorisable paper must still appear"


def test_a_known_venue_with_no_description_does_not_warn(capsys):
    """`kind: other` has no ranking to state; warning made it look unconfigured."""
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "blog"})
    build_bib._process_entries(parse_bibtex(bib(("p1", ""))), df, {})
    assert "unknown venue" not in capsys.readouterr().out


def test_the_entry_text_is_rewritten_not_reformatted():
    """beg + rest must still reconstruct parseable BibTeX."""
    df = table({"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"})
    out, *_ = build_bib._process_entries(
        parse_bibtex(bib(("p1", "\n    booktitle = {Proc. of ACL 2024, Bangkok, pp. 1-9},"))),
        df, {"Paper One": 3})
    reparsed = parse_bibtex(out)
    assert [e["item_name"] for e in reparsed] == ["p1"]
    assert "Bangkok" not in out, "the booktitle tail should be trimmed at the year"


# --- duplicate resolution ----------------------------------------------

def _dup_df():
    return table({"Name": "Same Paper", "Bib": "published"},
                 {"Name": "Same  paper", "Bib": "preprint"})


def test_the_published_version_wins_and_the_preprint_is_suppressed(monkeypatch):
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    parsed = parse_bibtex(
        "@inproceedings{published,\n title={Same Paper},\n booktitle={ACL},\n}\n"
        "@misc{preprint,\n title={Same paper},\n eprint={2401.00001},\n}\n")
    suppressed, notes = build_bib.resolve_duplicate_rows(parsed, _dup_df())
    assert suppressed == {"preprint"}
    assert notes and notes[0][0] == "published" and notes[0][1] == ["preprint"]


def test_two_rows_titled_identically_suppress_the_preprint(monkeypatch):
    """The tests above spell the duplicate two ways ("Same Paper" / "Same  paper"),
    which is not the common case: the common case is one title entered twice.

    A group of the same string twice used to be looked up once per member and hit
    the same row both times, so the published entry was ranked against itself,
    named the winner and added to `suppressed` -- and since the preprint's row was
    never consulted, the CV printed the arXiv version of a paper whose ACL entry
    was in the table, with a note reading "published beats published".
    """
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    parsed = parse_bibtex(
        "@inproceedings{published,\n title={Same Paper},\n booktitle={ACL},\n}\n"
        "@misc{preprint,\n title={Same Paper},\n eprint={2401.00001},\n}\n")
    df = table({"Name": "Same Paper", "Bib": "published"},
               {"Name": "Same Paper", "Bib": "preprint"})
    suppressed, notes = build_bib.resolve_duplicate_rows(parsed, df)
    assert suppressed == {"preprint"}
    assert notes and notes[0][0] == "published" and notes[0][1] == ["preprint"]


def test_two_rows_sharing_one_entry_suppress_nothing(monkeypatch):
    """One entry cannot outrank itself, and suppressing it would drop the paper
    from the CV rather than the duplicate. `_report_duplicates` raises the shared
    Bib cell instead, which is a table problem with a table fix."""
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    df = table({"Name": "Same Paper", "Bib": "p1"}, {"Name": "Same  paper", "Bib": "p1"})
    suppressed, notes = build_bib.resolve_duplicate_rows(
        parse_bibtex(bib(("p1", ""))), df)
    assert suppressed == set() and notes == []


def test_one_row_per_paper_suppresses_nothing(monkeypatch):
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    df = table({"Name": "Paper One", "Bib": "p1"}, {"Name": "Paper Two", "Bib": "p2"})
    suppressed, notes = build_bib.resolve_duplicate_rows(
        parse_bibtex(bib(("p1", ""), ("p2", ""))), df)
    assert suppressed == set() and notes == []


def test_a_shared_identifier_catches_a_retitled_duplicate(monkeypatch):
    """Two titles too different to match, one arXiv ID: still one paper."""
    store = IdentityStore({"a": {"arxiv": "2407.13696"}, "b": {"arxiv": "2407.13696"}})
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    parsed = parse_bibtex(
        "@inproceedings{a,\n title={Benchmark Agreement Testing Done Right},\n"
        " booktitle={ACL},\n}\n"
        "@misc{b,\n title={Do These LLM Benchmarks Agree},\n}\n")
    df = table({"Name": "Benchmark Agreement Testing Done Right", "Bib": "a"},
               {"Name": "Do These LLM Benchmarks Agree", "Bib": "b"})
    suppressed, _ = build_bib.resolve_duplicate_rows(parsed, df)
    assert suppressed == {"b"}


# --- reporting the problems nothing downstream can fix ------------------

def test_two_rows_with_the_same_title_are_reported():
    df = table({"Name": "Same Paper", "Bib": "p1"}, {"Name": "same  paper", "Bib": "p2"})
    problems = build_bib._report_duplicates(parse_bibtex(bib(("p1", ""), ("p2", ""))), df)
    kinds = {kind for kind, _, _ in problems}
    assert "duplicate-table-row" in kinds


def test_a_bibtex_key_appearing_twice_is_reported():
    df = table({"Name": "Paper One", "Bib": "p1"})
    problems = build_bib._report_duplicates(parse_bibtex(bib(("p1", ""), ("p1", ""))), df)
    assert ("duplicate-bib-key", "p1", ["2 entries in orig.bib"]) in problems


def test_one_bib_key_used_by_two_rows_is_reported():
    df = table({"Name": "Paper One", "Bib": "shared"},
               {"Name": "Paper Two", "Bib": "shared"})
    problems = build_bib._report_duplicates(parse_bibtex(bib(("shared", ""))), df)
    assert ("duplicate-bib-cell", "shared", ["2 table rows"]) in problems


def test_a_clean_table_reports_no_problems():
    df = table({"Name": "Paper One", "Bib": "p1"}, {"Name": "Paper Two", "Bib": "p2"})
    assert build_bib._report_duplicates(parse_bibtex(bib(("p1", ""), ("p2", ""))), df) == []


def test_the_placeholder_bib_cells_are_not_duplicates():
    """"nan"/"none" mean "no key yet", so many rows legitimately share them."""
    df = table({"Name": "Paper One", "Bib": "nan"}, {"Name": "Paper Two", "Bib": "none"},
               {"Name": "Paper Three", "Bib": "nan"})
    assert build_bib._report_duplicates([], df) == []


# --- coverage warning ---------------------------------------------------

def test_a_row_whose_key_is_missing_from_orig_bib_is_named(capsys):
    df = table({"Name": "Paper One", "Bib": "p1"}, {"Name": "Missing One", "Bib": "gone"})
    build_bib._check_coverage(parse_bibtex(bib(("p1", ""))), df, bibs_seen=1)
    out = capsys.readouterr().out
    assert "'gone'" in out and "Missing One" in out


def test_full_coverage_says_nothing(capsys):
    df = table({"Name": "Paper One", "Bib": "p1"})
    build_bib._check_coverage(parse_bibtex(bib(("p1", ""))), df, bibs_seen=1)
    assert capsys.readouterr().out == ""


# --- binding Scholar IDs, which is what stops the fuzzy matching --------

class _Join:
    def __init__(self, source):
        self.source = source


def test_a_matched_paper_gets_its_scholar_id_recorded(tmp_path, capsys):
    store = IdentityStore()
    path = tmp_path / "identity.json"
    store.save = lambda p=str(path): IdentityStore.save(store, p)
    df = table({"Name": "Paper One", "Bib": "p1"})
    build_bib._bind_scholar_ids(_Join({"Paper One": {"scholar_id": "ABC123"}}), df, store)
    assert store.records["p1"]["scholar_id"] == "ABC123"
    assert "Bound 1" in capsys.readouterr().out
    assert json.loads(path.read_text())["records"]["p1"]["scholar_id"] == "ABC123"


def test_a_paper_with_no_scholar_id_is_counted_not_bound(tmp_path, capsys):
    store = IdentityStore()
    store.save = lambda p=str(tmp_path / "identity.json"): IdentityStore.save(store, p)
    df = table({"Name": "Paper One", "Bib": "p1"})
    build_bib._bind_scholar_ids(_Join({"Paper One": {}}), df, store)
    assert store.records == {}
    assert "no Scholar ID" in capsys.readouterr().out


def test_a_paper_with_no_bib_key_yet_is_skipped_quietly(tmp_path):
    """Step 3 assigns the key; a later run binds the ID."""
    store = IdentityStore()
    store.save = lambda p=str(tmp_path / "identity.json"): IdentityStore.save(store, p)
    df = table({"Name": "Paper One", "Bib": "nan"})
    build_bib._bind_scholar_ids(_Join({"Paper One": {"scholar_id": "ABC"}}), df, store)
    assert store.records == {}


def test_rebinding_an_already_bound_paper_is_not_counted_as_new(tmp_path, capsys):
    store = IdentityStore({"p1": {"scholar_id": "ABC"}})
    store.save = lambda p=str(tmp_path / "identity.json"): IdentityStore.save(store, p)
    df = table({"Name": "Paper One", "Bib": "p1"})
    build_bib._bind_scholar_ids(_Join({"Paper One": {"scholar_id": "ABC"}}), df, store)
    assert "Bound" not in capsys.readouterr().out


# --- reading citations.csv ---------------------------------------------

def test_a_missing_citations_file_is_a_warning_not_a_crash(tmp_path, capsys):
    rows = build_bib.load_citations(str(tmp_path / "nope.csv"))
    assert rows == []
    assert "not found" in capsys.readouterr().out


def _dated(tmp_path, days_ago):
    """A citations.csv beside a profile_stats.json fetched `days_ago` days ago."""
    (tmp_path / "citations.csv").write_text("title,citations\nPaper One,3\n")
    fetched = (date.today() - timedelta(days=days_ago)).isoformat()
    (tmp_path / "profile_stats.json").write_text(
        json.dumps({"citations": 3, "h_index": 1, "fetched": fetched}))
    return str(tmp_path / "citations.csv"), fetched


def test_a_stale_citations_file_is_flagged(tmp_path, capsys):
    path, fetched = _dated(tmp_path, STALE_AFTER_DAYS + 5)
    build_bib.load_citations(path)
    out = capsys.readouterr().out
    assert "days ago" in out
    assert fetched in out, "the date is the actionable part: it says how far behind"


def test_the_age_is_the_recorded_fetch_not_the_file_s_mtime(tmp_path, capsys):
    """The mtime this used to read is reset by `git clone`.

    So in CI, and in every fork, and in any fresh clone of the author's own repo,
    the file was modified at checkout -- and the warning could not fire however old
    the data actually was. Here the file is written now and the fetch was long ago,
    which is exactly that situation and must still warn.
    """
    path, _ = _dated(tmp_path, STALE_AFTER_DAYS + 400)
    assert time.time() - os.path.getmtime(path) < 60, "the file itself is new"
    build_bib.load_citations(path)
    assert "days ago" in capsys.readouterr().out


def test_a_fresh_citations_file_is_read_without_comment(tmp_path, capsys):
    path, _ = _dated(tmp_path, 3)
    rows = build_bib.load_citations(path)
    assert len(rows) == 1
    assert "days ago" not in capsys.readouterr().out


def test_an_undated_citations_file_is_not_called_stale(tmp_path, capsys):
    """A fork that has never fetched has zeroed stats and no date.

    Warning there would be a guess, and the first thing a stranger sees. Nothing
    is claimed about counts whose age is unknown.
    """
    (tmp_path / "citations.csv").write_text("title,citations\nPaper One,3\n")
    (tmp_path / "profile_stats.json").write_text('{"citations": 0, "h_index": 0}')
    build_bib.load_citations(str(tmp_path / "citations.csv"))
    assert "days ago" not in capsys.readouterr().out


# --- main(), start to finish --------------------------------------------

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """main() reads and writes real files. Point every one of them at tmp_path.

    IdentityStore.save() defaults to the repository's identity.json, and the
    default is bound at definition time, so patching the module constant is not
    enough -- the method itself has to be replaced or a test run would rewrite
    the author's harvested identifiers.
    """
    monkeypatch.setattr(build_bib, "FILE_DIR", str(tmp_path))
    (tmp_path / "overleaf").mkdir()
    (tmp_path / "citations.csv").write_text(
        ",".join(citations_io.HEADER) + "\nPaper One,12,2024,A. Author,ACL,\n")

    saved = {}
    monkeypatch.setattr(build_bib.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    monkeypatch.setattr(build_bib.IdentityStore, "save",
                        lambda self, path=None: saved.update(self.records))
    return tmp_path, saved


def test_main_writes_a_bibliography_and_returns_the_sections(sandbox, monkeypatch):
    tmp_path, _ = sandbox
    (tmp_path / "orig.bib").write_text(
        bib(("p1", "\n    booktitle = {ACL},"), ("p2", "")))
    monkeypatch.setattr(build_bib, "read_df", lambda: table(
        {"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"},
        {"Name": "Paper Two", "Bib": "p2", "Venue": "TACL"}))

    cats = build_bib.main()

    written = (tmp_path / "overleaf" / "Wzmn.bib").read_text()
    assert "citations={12}" in written, "the fetched count did not reach the CV"
    assert cats.conferences == ["p1"] and cats.journals == ["p2"]


def test_main_emits_a_nocite_block_per_section(sandbox, monkeypatch, capsys):
    """rebuild_tex.py pastes these into main.tex; an empty section still needs one."""
    tmp_path, _ = sandbox
    (tmp_path / "orig.bib").write_text(bib(("p1", "")))
    monkeypatch.setattr(build_bib, "read_df", lambda: table(
        {"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"}))

    build_bib.main()

    out = capsys.readouterr().out
    for label in build_bib._CATEGORY_LABELS.values():
        assert f"% {label}:" in out
    assert "\\nocite{p1}" in out


def test_main_binds_scholar_ids_it_learned(sandbox, monkeypatch):
    tmp_path, saved = sandbox
    # The full header on purpose: a file with fewer than four columns is read as
    # the historical three-rows-per-paper format, where column 2 is the year.
    (tmp_path / "citations.csv").write_text(
        ",".join(citations_io.HEADER) + "\nPaper One,12,2024,A. Author,ACL,ABC123\n")
    (tmp_path / "orig.bib").write_text(bib(("p1", "")))
    monkeypatch.setattr(build_bib, "read_df", lambda: table(
        {"Name": "Paper One", "Bib": "p1", "Venue": "ACL 2024"}))

    build_bib.main()

    assert saved.get("p1", {}).get("scholar_id") == "ABC123", (
        "without this the next run has to fuzzy-match the title again")


def test_main_survives_an_empty_repository(sandbox, monkeypatch):
    """The state of a fresh fork: no papers at all must still produce a file."""
    tmp_path, _ = sandbox
    (tmp_path / "orig.bib").write_text("")
    monkeypatch.setattr(build_bib, "read_df", lambda: table())

    cats = build_bib.main()

    assert (tmp_path / "overleaf" / "Wzmn.bib").read_text() == ""
    assert all(section == [] for section in cats)


# --- the join report, which is the only place a bad match is visible ----

@pytest.mark.parametrize("field, rows, expected", [
    ("needs_review", [("Paper One", "paper one", "fuzzy", 0.9)], "matched by title"),
    ("aggregated",   [("Paper One", [("t", "exact", 1.0)], 7)], "Summed across"),
    ("ambiguous",    [("Paper One", [("t", "fuzzy", 0.9)], 7)], "AMBIGUOUS"),
    ("too_close",    [("Paper One", 7, 0.9)], "equally well"),
    ("unmatched",    [("Paper One", 7)], "no row in the publications table"),
])
def test_every_join_anomaly_is_printed(monkeypatch, capsys, field, rows, expected):
    """Silence here is how a wrong citation count reaches the CV unnoticed."""
    class Result:
        needs_review = aggregated = ambiguous = too_close = unmatched = ()

        def __init__(self):
            self.matched = {}
            self.source = {}

        def tier_counts(self):
            return {}

    result = Result()
    setattr(result, field, rows)
    monkeypatch.setattr(build_bib, "join_citations",
                        lambda *a, **k: result)
    build_bib._build_name2cite([], ["Paper One"])
    assert expected in capsys.readouterr().out
