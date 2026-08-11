"""Tests for scripts/worklist.py: the report that makes everything else visible.

WORKLIST.md is the only place the pipeline's open questions survive the run that
found them. That makes a missing section the worst failure in the repo: a
duplicate row, an unknown venue or an unpushed CV goes unreported, the run says
success, and the CV is quietly wrong. Nothing else would notice.

So these tests are mostly about presence -- each detector fires on the input it
exists for -- plus the two judgement calls the report makes: which sections count
as needing a decision, and which findings are noise that would make `--check`
permanently red and therefore ignored.
"""

import json
import os
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import worklist

from identity import MATCH_EXACT_ID, MATCH_FUZZY, IdentityStore

_DEFAULTS = {"Paper": 1, "Workshop-paper": 0, "Review, Survey and Position": 0}
_COLUMNS = ["Name", "Bib", "Venue", *_DEFAULTS]


def table(*rows):
    return pd.DataFrame([{**_DEFAULTS, **r} for r in rows], columns=_COLUMNS)


class Join:
    """Stand-in for identity.join_citations' result."""

    def __init__(self, **kwargs):
        self.needs_review = kwargs.get("needs_review", [])
        self.ambiguous = kwargs.get("ambiguous", [])
        self.unmatched = kwargs.get("unmatched", [])
        self.too_close = kwargs.get("too_close", [])
        self.aggregated = kwargs.get("aggregated", [])
        self.matched = kwargs.get("matched", {})
        self.method = kwargs.get("method", {})


@pytest.fixture
def wl(tmp_path, monkeypatch):
    """Point every file and every collaborator at something disposable.

    gather() reads orig.bib, citations.csv, identity.json, the publications
    table, and the overleaf/ submodule's git state. Left alone it would report on
    the author's real data, and --check would pass or fail for reasons that have
    nothing to do with the test.
    """
    (tmp_path / "orig.bib").write_text("")
    (tmp_path / "citations.csv").write_text("Title,Cited by,Year,Authors,Venue,Scholar ID\n")
    monkeypatch.setattr(worklist, "BIB_PATH", str(tmp_path / "orig.bib"))
    monkeypatch.setattr(worklist, "CITATIONS_CSV", str(tmp_path / "citations.csv"))
    monkeypatch.setattr(worklist, "WORKLIST_PATH", str(tmp_path / "WORKLIST.md"))
    # A path that does not exist, so the unpublished-output check is a no-op
    # unless a test asks for it.
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(tmp_path / "no-overleaf"))
    # Where the fetch date is read from. Left alone, the staleness section would
    # report on the author's own profile_stats.json -- passing today and failing
    # whenever he next goes two months without a fetch.
    monkeypatch.setattr(worklist, "ROOT", str(tmp_path))

    monkeypatch.setattr(worklist, "read_df", lambda: table())
    monkeypatch.setattr(worklist, "get_missing_bib_entries", lambda *a, **k: [])
    monkeypatch.setattr(worklist, "load_attempts", lambda *a, **k: {})
    monkeypatch.setattr(worklist, "join_citations", lambda *a, **k: Join())
    monkeypatch.setattr(worklist.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    return tmp_path


def titles(sections):
    return [s.title for s in sections]


def only(sections):
    """The single section gather() produced, so a test cannot pass on the wrong one."""
    assert len(sections) == 1, f"expected one section, got {titles(sections)}"
    return sections[0]


# --- nothing to report --------------------------------------------------

def test_a_clean_pipeline_produces_no_sections(wl):
    sections, total, _ = worklist.gather()
    assert sections == [] and total == 0


def test_nothing_open_renders_as_done(wl):
    assert "Nothing open" in worklist.render([], Join())


# --- how old the numbers are --------------------------------------------
# The weekly fetch runs on one particular Mac, so it stops without anything
# failing: no error, no red CI, just numbers that quietly stay put. This section
# is the only thing that notices.

def _fetched(wl, days_ago):
    (wl / "profile_stats.json").write_text(json.dumps(
        {"citations": 10, "h_index": 2,
         "fetched": (date.today() - timedelta(days=days_ago)).isoformat()}))


def test_counts_older_than_the_threshold_are_reported(wl):
    _fetched(wl, worklist.STALE_AFTER_DAYS + 3)
    section = only(worklist.gather()[0])
    assert "days old" in section.title
    assert "update.py" in section.blurb, "a report with no way to act on it"


def test_the_report_says_when_rather_than_only_that_it_is_old(wl):
    """"Stale" is not actionable; a date is. It also says whether the job stopped
    two months ago or two years ago, which decides what to look at."""
    _fetched(wl, worklist.STALE_AFTER_DAYS + 3)
    body = "\n".join(only(worklist.gather()[0]).lines)
    assert (date.today() - timedelta(days=worklist.STALE_AFTER_DAYS + 3)).isoformat() \
        in body


def test_counts_inside_the_threshold_are_not_reported(wl):
    """A few missed Mondays are a holiday. Reporting them trains the author to
    scroll past the section that matters."""
    _fetched(wl, worklist.STALE_AFTER_DAYS - 1)
    assert worklist.gather()[0] == []


def test_counts_of_unknown_age_are_not_reported(wl):
    """A fork has zeroed stats and no fetch date, and would otherwise open its
    first worklist with a complaint about data it has never had."""
    (wl / "profile_stats.json").write_text('{"citations": 0, "h_index": 0}')
    assert worklist.gather()[0] == []


def test_check_fails_on_stale_counts(wl):
    """Nobody has to decide anything, but nothing clears it either until the
    pipeline runs -- which is the definition of an open item here."""
    _fetched(wl, worklist.STALE_AFTER_DAYS + 3)
    assert worklist.main(["--check"]) == 1


# --- duplicates ---------------------------------------------------------

def test_two_rows_for_one_paper_are_reported_with_both_keys(wl, monkeypatch):
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "Same Paper", "Bib": "k1"}, {"Name": "same  paper", "Bib": "k2"}))
    section = only(worklist.gather()[0])
    assert "Duplicate rows" in section.title
    body = "\n".join(section.lines)
    assert "`k1`" in body and "`k2`" in body


def test_two_rows_titled_identically_are_reported_with_both_keys(wl, monkeypatch):
    """The test above spells the duplicate two ways, which hides the common case:
    one title entered twice. A group is then the same string twice, and looking
    each member up by name read the same row both times -- so this list, the only
    thing telling the author which key to delete, named the surviving key twice
    and never mentioned the other one at all.
    """
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "Same Paper", "Bib": "k1"}, {"Name": "Same Paper", "Bib": "k2"}))
    section = only(worklist.gather()[0])
    body = "\n".join(section.lines)
    assert "2 rows" in body
    assert "`k1`" in body and "`k2`" in body
    assert body.count("Same Paper — `k1`") == 1
    assert body.count("Same Paper — `k2`") == 1


def test_a_duplicate_bibtex_key_is_reported(wl, monkeypatch):
    (wl / "orig.bib").write_text(
        "@misc{dup, title={A}}\n@misc{dup, title={B}}\n")
    section = only(worklist.gather()[0])
    assert "Duplicate BibTeX keys" in section.title
    assert "`dup` appears 2 times" in section.lines[0]


# --- papers with no entry ----------------------------------------------

def test_a_paper_with_no_bib_entry_is_listed_with_its_attempt_count(wl, monkeypatch):
    monkeypatch.setattr(worklist, "get_missing_bib_entries",
                        lambda *a, **k: [{"item_name": "p1", "title": "A Paper"}])
    monkeypatch.setattr(worklist, "load_attempts", lambda *a, **k: {"p1": 4})
    section = only(worklist.gather()[0])
    assert "no BibTeX entry" in section.title
    assert "4 failed lookup(s)" in section.lines[0]


def test_missing_entries_are_ordered_by_how_often_they_have_failed(wl, monkeypatch):
    """The ones no source indexes are the ones worth pasting by hand."""
    monkeypatch.setattr(worklist, "get_missing_bib_entries", lambda *a, **k: [
        {"item_name": "easy", "title": "Recent"},
        {"item_name": "hopeless", "title": "Obscure"}])
    monkeypatch.setattr(worklist, "load_attempts",
                        lambda *a, **k: {"easy": 1, "hopeless": 9})
    section = only(worklist.gather()[0])
    assert "hopeless" in section.lines[0] and "easy" in section.lines[1]


# --- the citation join's four kinds of doubt ---------------------------

@pytest.mark.parametrize("kwargs, expected_title", [
    ({"needs_review": [("A Paper", "a paper", MATCH_FUZZY, 0.91)]},
     "matched by title, not by identifier"),
    ({"ambiguous": [("A Paper", [("Other", MATCH_FUZZY, 0.9)], 7)]},
     "Ambiguous citation matches"),
    ({"unmatched": [("A Stranger", 12)]},
     "no row in the table"),
    ({"too_close": [("A Paper", 7, 0.95)]},
     "matching two rows equally well"),
    ({"aggregated": [("A Paper", [("A Paper preprint", MATCH_FUZZY, 0.9)], 30)]},
     "summed across Scholar records"),
])
def test_each_join_doubt_gets_its_own_section(wl, monkeypatch, kwargs, expected_title):
    monkeypatch.setattr(worklist, "join_citations", lambda *a, **k: Join(**kwargs))
    assert expected_title in only(worklist.gather()[0]).title


def test_a_fuzzy_match_is_marked_as_resolving_itself(wl, monkeypatch):
    """It disappears once fetch_citations records the Scholar ID, so it is not a
    decision the author has to make."""
    monkeypatch.setattr(worklist, "join_citations", lambda *a, **k: Join(
        needs_review=[("A Paper", "a paper", MATCH_FUZZY, 0.91)]))
    assert only(worklist.gather()[0]).nature == worklist.SELF_RESOLVING


def test_a_summed_count_is_informational_not_a_task(wl, monkeypatch):
    monkeypatch.setattr(worklist, "join_citations", lambda *a, **k: Join(
        aggregated=[("A Paper", [("Preprint", MATCH_FUZZY, 0.9)], 30)]))
    assert only(worklist.gather()[0]).nature == worklist.INFORMATIONAL


# --- venues -------------------------------------------------------------

def test_an_unconfigured_venue_is_reported_as_missing_from_venues_yaml(wl, monkeypatch):
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "A Paper", "Bib": "p1", "Venue": "Widgetry"}))
    section = only(worklist.gather()[0])
    assert "missing from venues.yaml" in section.title


def test_a_raw_scholar_venue_string_is_a_different_problem(wl, monkeypatch):
    """Step 2 pastes Scholar's text verbatim; those want a `match:` phrase, not a
    new venue entry, so they are reported separately."""
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "A Paper", "Bib": "p1",
         "Venue": "Some Very Long Conference Name, Volume 2, pages 1-9"}))
    section = only(worklist.gather()[0])
    assert "raw Scholar string" in section.title


def test_a_venue_placed_by_cutting_a_word_in_half_is_surfaced(wl, monkeypatch):
    """`nature` from "Nature-inspired Computing" is right by luck, not by rule."""
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "A Paper", "Bib": "p1", "Venue": "Nature-inspired Computing"}))
    section = only(worklist.gather()[0])
    assert "cutting a word in half" in section.title
    assert "`nature`" in section.lines[0]


def test_a_year_after_the_venue_key_is_not_a_cut_word(wl, monkeypatch):
    """"ACL2022" truncates to `acl`, which is unambiguous and not worth a report."""
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "A Paper", "Bib": "p1", "Venue": "ACL2022"}))
    assert worklist.gather()[0] == []


@pytest.mark.parametrize("row", [
    {"Venue": "arXiv preprint arXiv:2401.00001"},
    {"Venue": "Under review"},
    {"Venue": "US Patent 1,234,567"},
    {"Venue": "Widgetry", "Workshop-paper": 1},
    {"Venue": "Widgetry", "Paper": 0},
])
def test_rows_whose_venue_does_not_matter_are_not_reported(wl, monkeypatch, row):
    """A preprint, a patent, a workshop paper (filed by its flag) and a
    non-paper are all categorised without a venue key."""
    monkeypatch.setattr(worklist, "read_df",
                        lambda: table({"Name": "A Paper", "Bib": "p1", **row}))
    assert worklist.gather()[0] == [], "reported a row whose venue is irrelevant"


# --- shared identifiers -------------------------------------------------

def test_one_identifier_on_two_live_rows_is_reported(wl, monkeypatch):
    store = IdentityStore({"a": {"arxiv": "2407.13696"}, "b": {"arxiv": "2407.13696"}})
    monkeypatch.setattr(worklist.IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "One", "Bib": "a"}, {"Name": "Two", "Bib": "b"}))
    assert "One identifier claimed by two papers" in only(worklist.gather()[0]).title


def test_a_shared_identifier_on_a_key_no_row_uses_is_not_reported(wl, monkeypatch):
    """identity.json keeps records for keys that have left the table. Reporting
    those kept the section red after dedupe.py had already fixed the real case."""
    store = IdentityStore({"a": {"arxiv": "2407.13696"},
                           "stale": {"arxiv": "2407.13696"}})
    monkeypatch.setattr(worklist.IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    monkeypatch.setattr(worklist, "read_df", lambda: table({"Name": "One", "Bib": "a"}))
    assert worklist.gather()[0] == []


# --- built but not published -------------------------------------------

def test_uncommitted_cv_output_is_reported(wl, monkeypatch):
    overleaf = wl / "overleaf"
    overleaf.mkdir()
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(overleaf))
    # Uncommitted *and* not what the remote holds: both, because either alone is
    # not a reason to report a file. `diff` is how the second is asked.
    monkeypatch.setattr(worklist, "_git", lambda repo, *args:
                        {"status": " M main.tex\n",
                         "diff": "main.tex\n"}.get(args[0], ""))
    section = only(worklist.gather()[0])
    assert "built but not committed" in section.title
    assert section.lines == ["- `overleaf/main.tex` has uncommitted changes"], (
        "the status column must not be stripped off the path")


def test_committed_but_unpushed_cv_output_is_reported(wl, monkeypatch):
    overleaf = wl / "overleaf"
    overleaf.mkdir()
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(overleaf))
    monkeypatch.setattr(worklist, "_git", lambda repo, *args:
                        "abc1234 publish\n" if args[0] == "log" else "")
    section = only(worklist.gather()[0])
    assert "committed but not pushed" in section.title


def test_a_missing_overleaf_directory_is_not_an_error(wl):
    assert worklist._unpublished_output() == []


# --- identifier conflicts ----------------------------------------------

def test_conflicting_identifiers_are_reported(wl, monkeypatch):
    # The shape record() produces when a second source disagrees: the first value
    # stays, the rejected one is kept under _conflicts rather than overwriting it.
    store = IdentityStore()
    store.record("a", doi="10.1/x")
    store.record("a", doi="10.1/y")
    monkeypatch.setattr(worklist.IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    monkeypatch.setattr(worklist, "read_df", lambda: table({"Name": "One", "Bib": "a"}))
    assert any("Conflicting identifiers" in t for t in titles(worklist.gather()[0]))


# --- rendering ----------------------------------------------------------

def test_the_summary_counts_only_the_sections_needing_something():
    sections = [
        worklist.Section("Needs you", "b", ["- x"], nature=worklist.ONE_OFF),
        worklist.Section("Recurs", "b", ["- x"], nature=worklist.RECURRING),
        # Needs an action rather than a decision -- a run -- and counts for the
        # same reason: nothing else clears it.
        worklist.Section("Old", "b", ["- x"], nature=worklist.STALE),
        worklist.Section("Self", "b", ["- x"], nature=worklist.SELF_RESOLVING),
        worklist.Section("FYI", "b", ["- x"], nature=worklist.INFORMATIONAL),
        worklist.Section("External", "b", ["- x"], nature=worklist.EXTERNAL),
    ]
    text = worklist.render(sections, Join(matched={"a": 1}, method={"a": MATCH_EXACT_ID}))
    assert "**3 of 6 sections need something from you**" in text


def test_every_section_renders_its_nature_and_its_lines():
    section = worklist.Section("A Title", "why it matters", ["- an item"],
                              nature=worklist.RECURRING)
    text = worklist.render([section], Join())
    assert "## A Title" in text
    assert f"*{worklist.RECURRING[0]}*" in text
    assert "why it matters" in text and "- an item" in text


def test_the_join_summary_reports_how_many_matched_exactly():
    text = worklist.render([], Join(matched={"a": 1, "b": 2},
                                   method={"a": MATCH_EXACT_ID, "b": MATCH_FUZZY}))
    assert "2 papers matched, 1 by stable Scholar ID" in text


# --- main() -------------------------------------------------------------

def test_main_writes_the_file(wl, monkeypatch):
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "Same Paper", "Bib": "k1"}, {"Name": "same paper", "Bib": "k2"}))
    assert worklist.main(["--quiet"]) == 0
    assert "Duplicate rows" in (wl / "WORKLIST.md").read_text()


def test_main_does_not_rewrite_an_unchanged_file(wl):
    """An unconditional write would dirty the git tree on every run and make CI's
    staleness check fire at random."""
    worklist.main(["--quiet"])
    path = wl / "WORKLIST.md"
    before = path.stat().st_mtime_ns
    worklist.main(["--quiet"])
    assert path.stat().st_mtime_ns == before


def test_check_exits_nonzero_when_something_needs_a_decision(wl, monkeypatch):
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "Same Paper", "Bib": "k1"}, {"Name": "same paper", "Bib": "k2"}))
    assert worklist.main(["--check"]) == 1


def test_check_ignores_items_that_resolve_themselves(wl, monkeypatch):
    """Otherwise --check is permanently red, and a permanently red check is one
    nobody reads."""
    monkeypatch.setattr(worklist, "join_citations", lambda *a, **k: Join(
        needs_review=[("A Paper", "a paper", MATCH_FUZZY, 0.91)],
        aggregated=[("B Paper", [("Preprint", MATCH_FUZZY, 0.9)], 30)]))
    assert worklist.main(["--check"]) == 0


def test_check_writes_nothing(wl, monkeypatch):
    monkeypatch.setattr(worklist, "read_df", lambda: table(
        {"Name": "Same Paper", "Bib": "k1"}, {"Name": "same paper", "Bib": "k2"}))
    worklist.main(["--check"])
    assert not (wl / "WORKLIST.md").exists()


def test_quiet_prints_nothing_but_still_writes(wl, capsys):
    worklist.main(["--quiet"])
    assert capsys.readouterr().out == ""
    assert (wl / "WORKLIST.md").exists()
