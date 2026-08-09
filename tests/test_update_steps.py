"""The individual pipeline steps, as opposed to main()'s orchestration.

test_update_orchestration.py replaces every step with a recorder and checks what
runs. This file does the reverse: it runs the steps.

Two of them decide what the CV says without a human in the loop -- step 2 adds
rows for papers Scholar reports, step 2b overwrites cells on existing rows -- so
their guards are what stop Scholar's mistakes becoming the author's. The third,
step 7, is the only code here that writes to somebody else's repository.
"""

import os
import subprocess
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolve_arxiv
import update
from identity import IdentityStore

_COLUMNS = ["Name", "Bib", "Venue", "Authors", "year", "Paper", "Workshop-paper"]


def table(*rows):
    base = {"Bib": "", "Venue": "", "Authors": "", "year": 2024,
            "Paper": 1, "Workshop-paper": 0}
    return pd.DataFrame([{**base, **r} for r in rows], columns=_COLUMNS)


def scholar(*rows):
    base = {"title": "", "authors": "", "venue": "", "citations": "1",
            "year": "2024", "scholar_id": ""}
    return [{**base, **r} for r in rows]


class _Join:
    def __init__(self, source=None):
        self.source = source or {}


# ── preflight ────────────────────────────────────────────────────────────────

@pytest.fixture
def healthy(tmp_path, monkeypatch):
    """A machine on which preflight finds nothing wrong."""
    import config
    import rebuild_tex
    (tmp_path / "papers.csv").write_text("Name\n")
    (tmp_path / "orig.bib").write_text("")
    monkeypatch.setattr(update, "TABLE_PATH", str(tmp_path / "papers.csv"))
    monkeypatch.setattr(update, "BIB_PATH", str(tmp_path / "orig.bib"))
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(rebuild_tex, "check_overleaf_present", lambda: None)
    # Stubbed because the real one makes a network request to Overleaf; there is a
    # test below for what preflight does when it comes back with a problem.
    monkeypatch.setattr(update.overleaf_auth, "check_credential", lambda _dir: None)
    monkeypatch.setattr(config, "SCHOLAR_USER_ID", "abc123", raising=False)
    monkeypatch.setattr(config, "AUTHOR_NAME", "A. Author", raising=False)
    return tmp_path


def test_a_healthy_setup_has_no_problems(healthy):
    assert update.preflight() == []


def test_a_missing_curl_is_reported(healthy, monkeypatch):
    """Every HTTP fetch goes through curl, because Python's TLS fingerprint gets
    blocked by Scholar -- so its absence is not a degradation, it is step 1
    failing."""
    monkeypatch.setattr(update.shutil, "which",
                        lambda name: None if name == "curl" else "/usr/bin/git")
    assert any("curl is not on PATH" in p for p in update.preflight())


def test_a_missing_git_is_reported(healthy, monkeypatch):
    monkeypatch.setattr(update.shutil, "which",
                        lambda name: None if name == "git" else "/usr/bin/curl")
    assert any("git is not on PATH" in p for p in update.preflight())


def test_a_missing_table_is_reported(healthy, monkeypatch):
    monkeypatch.setattr(update, "TABLE_PATH", str(healthy / "gone.csv"))
    assert any("No publications table" in p for p in update.preflight())


def test_a_missing_bib_is_reported(healthy, monkeypatch):
    monkeypatch.setattr(update, "BIB_PATH", str(healthy / "gone.bib"))
    assert any("orig.bib is missing" in p for p in update.preflight())


def test_a_missing_overleaf_credential_is_reported(healthy, monkeypatch):
    """The check that did not exist. Step 7 pushes to Overleaf, and every other
    thing it needs was checked before step 1 except the ability to do that -- so a
    run with no stored token did a Scholar fetch and six steps first, then failed.
    """
    monkeypatch.setattr(update.overleaf_auth, "check_credential",
                        lambda _dir: "Cannot authenticate to Overleaf")
    assert "Cannot authenticate" in (update.check_push_credential() or "")


def test_a_missing_credential_does_not_block_the_run(healthy, monkeypatch):
    """It is reported and the run continues. The six steps still leave the data and
    the CV current on disk; freezing the pipeline over a rotated token would stop
    the data tracking reality too, which is the worse of the two failures."""
    monkeypatch.setattr(update.overleaf_auth, "check_credential",
                        lambda _dir: "Cannot authenticate to Overleaf")
    assert update.preflight() == []


def test_the_credential_is_not_checked_when_nothing_will_be_pushed(healthy, monkeypatch):
    """`--no-push` and `--dry-run` need no credential, so the network round trip
    and the warning are both pointless there."""
    called = []
    monkeypatch.setattr(update.overleaf_auth, "check_credential",
                        lambda _dir: called.append(1) or "Cannot authenticate")
    assert update.check_push_credential(False) is None
    assert not called


def test_the_credential_is_not_checked_when_the_submodule_is_absent(healthy, monkeypatch):
    """Two reports of one problem send the reader to the wrong instruction: there
    is no point storing a token for a submodule that is not there."""
    import rebuild_tex
    monkeypatch.setattr(rebuild_tex, "check_overleaf_present",
                        lambda: "overleaf/ is empty")
    called = []
    monkeypatch.setattr(update.overleaf_auth, "check_credential",
                        lambda _dir: called.append(1) or "credential problem")
    assert update.check_push_credential() is None
    assert update.preflight() == ["overleaf/ is empty"]
    assert not called, "checked the credential for a submodule that is not there"


def test_an_unpopulated_overleaf_submodule_is_reported(healthy, monkeypatch):
    """The documented fork path leaves overleaf/ empty until the forker points it
    at their own project; catching it here beats a FileNotFoundError in step 5."""
    import rebuild_tex
    monkeypatch.setattr(rebuild_tex, "check_overleaf_present",
                        lambda: "overleaf/ is empty")
    assert "overleaf/ is empty" in update.preflight()


@pytest.mark.parametrize("field", ["SCHOLAR_USER_ID", "AUTHOR_NAME"])
def test_an_unconfigured_fork_is_reported(healthy, monkeypatch, field):
    """These are the two values a forker must edit, and neither has a usable
    default."""
    import config
    monkeypatch.setattr(config, field, "", raising=False)
    assert any(field in p for p in update.preflight())


def test_every_problem_is_reported_at_once(healthy, monkeypatch):
    """Not just the first: the point is to tell somebody everything they need to
    fix before a long run, rather than one thing per attempt."""
    import config
    monkeypatch.setattr(update.shutil, "which", lambda _name: None)
    monkeypatch.setattr(config, "AUTHOR_NAME", "", raising=False)
    assert len(update.preflight()) >= 3


# ── step 1 ───────────────────────────────────────────────────────────────────

def test_step1_runs_the_fetcher(monkeypatch):
    calls = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    update.step1_fetch(False)
    assert calls[0][0] == sys.executable
    assert calls[0][1].endswith("fetch_citations.py")


def test_step1_passes_an_explicit_user(monkeypatch):
    calls = []
    monkeypatch.setattr(update.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    update.step1_fetch(False, user="OTHER_ID")
    assert calls[0][-1] == "OTHER_ID"


def test_step1_makes_no_network_call_in_a_dry_run(monkeypatch):
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: pytest.fail("fetched during a dry run"))
    update.step1_fetch(True)


# ── step 2: adding papers Scholar reports ────────────────────────────────────

@pytest.fixture
def step2(monkeypatch):
    """Read the table and Scholar from memory; record what would be appended."""
    added = []
    monkeypatch.setattr(update, "read_table", lambda: table())
    monkeypatch.setattr(update, "read_citation_rows", lambda path: [])
    monkeypatch.setattr(update, "append_rows",
                        lambda rows: added.extend(rows) or len(rows))
    monkeypatch.setattr(update.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    return added


def test_a_scholar_paper_absent_from_the_table_is_added(step2, monkeypatch):
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "A Brand New Paper", "authors": "A. Author", "venue": "ACL",
         "year": "2024"}))
    assert update.step2_add_new_papers(False) == 1
    assert step2[0]["Name"] == "A Brand New Paper"
    assert step2[0]["Paper"] == 1


def test_a_paper_already_in_the_table_is_not_added_again(step2, monkeypatch):
    monkeypatch.setattr(update, "read_table",
                        lambda: table({"Name": "An Existing Paper"}))
    monkeypatch.setattr(update, "read_citation_rows",
                        lambda path: scholar({"title": "An Existing Paper"}))
    assert update.step2_add_new_papers(False) == 0
    assert step2 == []


def test_a_bound_scholar_id_means_known_however_the_title_reads(step2, monkeypatch):
    """Retitled between preprint and publication, the fuzzy match may fail -- and
    without this the same paper is re-reported as new on every run forever,
    because step 2 compared only titles and never read the identifiers the
    pipeline had already recorded."""
    store = IdentityStore()
    store.record("k1", scholar_id="SID-1")
    monkeypatch.setattr(update.IdentityStore, "load",
                        classmethod(lambda cls, path=None: store))
    monkeypatch.setattr(update, "read_table", lambda: table({"Name": "Old Title"}))
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "A Completely Different Published Title", "scholar_id": "SID-1"}))
    assert update.step2_add_new_papers(False) == 0


def test_a_patent_is_never_added(step2, monkeypatch):
    """Scholar lists patents alongside papers; the CV files them separately, by
    hand."""
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "A Method For Things", "venue": "US Patent 1,234,567"}))
    assert update.step2_add_new_papers(False) == 0


def test_a_fuzzy_already_known_verdict_is_printed(step2, monkeypatch, capsys):
    """Deciding a paper is already present on title similarity loses it
    permanently if the decision is wrong, so it is never silent.

    An exact-after-normalization match is silent on purpose; this pair differs by
    a real word, so only the fuzzy tier can join them.
    """
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "Attention Is All You Need"}))
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "Attention Is All You Really Need"}))
    update.step2_add_new_papers(False)
    out = capsys.readouterr().out
    assert "treated as already in the table on title similarity" in out


def test_a_new_paper_close_to_an_existing_row_is_flagged(step2, monkeypatch, capsys):
    """A near-miss is the shape of a duplicate row about to be created, so the
    closest existing title is printed next to it.

    The pair has to sit in the band between the two thresholds: too similar and
    it is joined to the existing row instead of being added, too different and it
    is a new paper with nothing worth pointing at.
    """
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "Attention Is All You Need"}))
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "Attention Is All You Need For Machine Translation"}))
    update.step2_add_new_papers(False)
    assert "closest existing row" in capsys.readouterr().out


def test_a_dry_run_reports_but_does_not_append(step2, monkeypatch, capsys):
    monkeypatch.setattr(update, "read_citation_rows",
                        lambda path: scholar({"title": "A Brand New Paper"}))
    assert update.step2_add_new_papers(True) == 1
    assert step2 == [], "a dry run wrote to the table"
    assert "table not modified" in capsys.readouterr().out


def test_a_non_numeric_year_is_not_written_as_one(step2, monkeypatch):
    """Scholar sometimes reports a range or a blank. The column is numeric, so a
    non-number has to become None rather than a string."""
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar(
        {"title": "A Brand New Paper", "year": "n.d."}))
    update.step2_add_new_papers(False)
    assert step2[0]["year"] is None


def test_an_empty_scholar_export_adds_nothing(step2, capsys):
    update.step2_add_new_papers(False)
    assert "No new papers found" in capsys.readouterr().out


# ── step 2b: filling gaps on existing rows ───────────────────────────────────

@pytest.fixture
def step2b(tmp_path, monkeypatch):
    """Record what step 2b would write, without touching the table."""
    written = {"authors": {}, "venue": {}}
    (tmp_path / "orig.bib").write_text("")
    monkeypatch.setattr(update, "BIB_PATH", str(tmp_path / "orig.bib"))
    monkeypatch.setattr(update, "read_table", lambda: table())
    monkeypatch.setattr(update, "read_citation_rows", lambda path: scholar({"title": "x"}))
    monkeypatch.setattr(update, "join_citations", lambda *a, **k: _Join())
    monkeypatch.setattr(update.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    monkeypatch.setattr(update, "fill_blanks",
                        lambda cols: written["authors"].update(cols["Authors"])
                        or len(cols["Authors"]))
    monkeypatch.setattr(update, "set_column",
                        lambda col, values: written["venue"].update(values)
                        or len(values))
    return tmp_path, written


def test_a_blank_authors_cell_is_filled_from_scholar(step2b, monkeypatch):
    """A row with no Authors cannot have a BibTeX key generated for it, so step 3
    files it under `unknown<year><title>`."""
    _tmp, written = step2b
    monkeypatch.setattr(update, "read_table", lambda: table({"Name": "A Paper"}))
    monkeypatch.setattr(update, "join_citations", lambda *a, **k: _Join(
        {"A Paper": {"authors": "A. Author and B. Other"}}))
    assert update.step2b_enrich(False) == 1
    assert written["authors"] == {"A Paper": "A. Author and B. Other"}


def test_an_authors_cell_a_human_filled_in_is_not_overwritten(step2b, monkeypatch):
    """Scholar is not more authoritative than the author about their own author
    list -- it truncates long ones with an ellipsis."""
    _tmp, written = step2b
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "A Paper", "Authors": "A. Author and B. Other and C. Third"}))
    monkeypatch.setattr(update, "join_citations", lambda *a, **k: _Join(
        {"A Paper": {"authors": "A Author, B Other, …"}}))
    assert update.step2b_enrich(False) == 0
    assert written["authors"] == {}


def test_a_venue_is_taken_from_the_bibliography_not_from_scholar(step2b, monkeypatch):
    """The bib entry was fetched from DBLP or the ACL Anthology, so its booktitle
    names the venue in full where Scholar truncates it."""
    tmp, written = step2b
    (tmp / "orig.bib").write_text(
        "@inproceedings{k1,\n  title = {A Paper},\n"
        "  booktitle = {Proceedings of the 62nd Annual Meeting of the Association "
        "for Computational Linguistics},\n  year = {2024}\n}\n")
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "A Paper", "Bib": "k1",
         "Venue": "Proceedings of the 62nd Annual Meeting of the, pages 1-9"}))
    assert update.step2b_enrich(False) == 1
    assert written["venue"] == {"A Paper": "acl"}


def test_a_row_whose_venue_already_resolves_is_left_alone(step2b, monkeypatch):
    tmp, written = step2b
    (tmp / "orig.bib").write_text(
        "@inproceedings{k1,\n  title = {A Paper},\n  booktitle = {EMNLP},\n"
        "  year = {2024}\n}\n")
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "A Paper", "Bib": "k1", "Venue": "ACL"}))
    assert update.step2b_enrich(False) == 0
    assert written["venue"] == {}


def test_a_preprint_entry_has_no_venue_to_give(step2b, monkeypatch):
    """A @misc has no booktitle, so there is nothing to fill from and the cell is
    left for the worklist -- which is the guard that stopped a workshop paper
    being relabelled as an ACL main-conference one."""
    tmp, written = step2b
    (tmp / "orig.bib").write_text(
        "@misc{k1,\n  title = {A Paper},\n  eprint = {2401.00001},\n"
        "  archivePrefix = {arXiv}\n}\n")
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "A Paper", "Bib": "k1", "Venue": "arXiv preprint arXiv:2401.00001"}))
    assert update.step2b_enrich(False) == 0
    assert written["venue"] == {}


def test_a_row_with_no_bib_entry_is_left_alone(step2b, monkeypatch):
    _tmp, written = step2b
    monkeypatch.setattr(update, "read_table", lambda: table(
        {"Name": "A Paper", "Bib": "absent", "Venue": "Widgetry 2024, pages 1-9"}))
    assert update.step2b_enrich(False) == 0


def test_an_unreadable_bib_does_not_stop_the_step(step2b, monkeypatch):
    """The venue half of the step needs orig.bib; the authors half does not."""
    tmp, written = step2b
    monkeypatch.setattr(update, "BIB_PATH", str(tmp / "absent.bib"))
    monkeypatch.setattr(update, "read_table", lambda: table({"Name": "A Paper"}))
    monkeypatch.setattr(update, "join_citations", lambda *a, **k: _Join(
        {"A Paper": {"authors": "A. Author"}}))
    assert update.step2b_enrich(False) == 1


def test_nothing_to_fill_is_reported_as_such(step2b, capsys):
    assert update.step2b_enrich(False) == 0
    assert "Nothing to fill" in capsys.readouterr().out


def test_no_citation_data_means_nothing_to_enrich_from(step2b, monkeypatch, capsys):
    monkeypatch.setattr(update, "read_citation_rows", lambda path: [])
    assert update.step2b_enrich(False) == 0
    assert "No citation data" in capsys.readouterr().out


def test_step2b_dry_run_writes_nothing(step2b, monkeypatch, capsys):
    _tmp, written = step2b
    monkeypatch.setattr(update, "read_table", lambda: table({"Name": "A Paper"}))
    monkeypatch.setattr(update, "join_citations", lambda *a, **k: _Join(
        {"A Paper": {"authors": "A. Author"}}))
    assert update.step2b_enrich(True) == 1
    assert written["authors"] == {}
    assert "table not modified" in capsys.readouterr().out


# ── step 3: the dry-run branch, which must not touch the network ─────────────

def test_step3_dry_run_performs_no_lookups(tmp_path, monkeypatch, capsys):
    """Resolving every candidate is hundreds of requests and minutes of
    deliberate rate-limit sleeps, which is not what "show me what would change"
    should cost."""
    bib = tmp_path / "orig.bib"
    bib.write_text("@misc{k1,\n  title = {A Paper},\n  eprint = {2401.00001},\n"
                   "  archivePrefix = {arXiv}\n}\n")
    monkeypatch.setattr(update, "BIB_PATH", str(bib))
    monkeypatch.setattr(update, "load_attempts", lambda: {})
    monkeypatch.setattr(update.IdentityStore, "load",
                        classmethod(lambda cls, path=None: IdentityStore()))
    monkeypatch.setattr(update, "get_missing_bib_entries", lambda text: [
        {"item_name": "new1", "title": "An Unresolved Row"}])
    monkeypatch.setattr(update, "resolve",
                        lambda *a, **k: pytest.fail("resolved during a dry run"))
    upgraded, appended, still_arxiv, not_found = update.step3_resolve(True)
    assert (upgraded, appended, still_arxiv) == (0, 0, 1)
    out = capsys.readouterr().out
    assert "no lookups performed" in out
    assert "An Unresolved Row" in out


# ── step 3: the live branch, which writes orig.bib ───────────────────────────

_ARXIV_ENTRY = ("@misc{{{key},\n  title = {{{title}}},\n  eprint = {{{eprint}}},\n"
                "  archivePrefix = {{arXiv}}\n}}\n")


def published(key, title, venue="ACL"):
    return (f"@inproceedings{{{key},\n  title = {{{title}}},\n"
            f"  booktitle = {{{venue}}},\n  year = {{2024}}\n}}\n")


@pytest.fixture
def step3(tmp_path, monkeypatch):
    """orig.bib on disk, everything else in memory, and no network or sleeping.

    Returns a helper that sets up the two inputs -- the arXiv entries already in
    orig.bib and the table rows with no entry -- and hands back what the run did.
    """
    bib = tmp_path / "orig.bib"
    monkeypatch.setattr(update, "BIB_PATH", str(bib))
    monkeypatch.setattr(update.time, "sleep", lambda _s: None)
    # Module-global, and main() is what resets it in a real run.
    monkeypatch.setitem(resolve_arxiv._net_state, "unanswered", 0)
    saved = {"attempts": [], "store": 0, "keys": {}}
    monkeypatch.setattr(update, "save_attempts",
                        lambda a: saved["attempts"].append(dict(a)))
    monkeypatch.setattr(update, "set_bib_keys",
                        lambda assignments: saved["keys"].update(assignments)
                        or len(assignments))

    class _Store(IdentityStore):
        def save(self, path=None):
            saved["store"] += 1

    def _run(bib_text="", missing=(), resolver=None, attempts=None):
        bib.write_text(bib_text)
        monkeypatch.setattr(update, "load_attempts", lambda: dict(attempts or {}))
        monkeypatch.setattr(update.IdentityStore, "load",
                            classmethod(lambda cls, path=None: _Store()))
        monkeypatch.setattr(update, "get_missing_bib_entries",
                            lambda text: [dict(e) for e in missing])
        monkeypatch.setattr(update, "resolve",
                            resolver or (lambda *a, **k: ("", "not found")))
        result = update.step3_resolve(False)
        return result, bib.read_text(), saved
    return _run


def test_a_published_version_replaces_the_arxiv_entry(step3):
    """The whole point of the step: a preprint that has since appeared somewhere
    stops being cited as a preprint on the CV."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    resolver = lambda *a, **k: (published("k1", "A Paper"), "DBLP")  # noqa: E731
    (upgraded, appended, still_arxiv, not_found), bib, _s = step3(text, resolver=resolver)
    assert (upgraded, appended, still_arxiv) == (1, 0, 0)
    assert not_found == []
    assert "@inproceedings{k1" in bib and "@misc{k1" not in bib


def test_an_entry_with_no_published_version_yet_is_left_as_a_preprint(step3):
    """Most preprints are still preprints, so this is the common path: counted as
    unresolved, but not reported as a problem and not rewritten."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    resolver = lambda *a, **k: (text, "arXiv (export API)")  # noqa: E731
    (upgraded, _a, still_arxiv, _nf), bib, _s = step3(text, resolver=resolver)
    assert (upgraded, still_arxiv) == (0, 1)
    assert "@misc{k1" in bib


def test_an_entry_no_source_knows_is_reported_by_title(step3):
    """This list is what reaches WORKLIST.md, so it has to carry the title -- a
    bare key is not enough to paste an entry in by hand."""
    text = _ARXIV_ENTRY.format(key="k1", title="An Obscure Paper", eprint="2401.1")
    (_u, _a, _sa, not_found), _bib, _s = step3(text)
    assert not_found == [("An Obscure Paper", "k1")]


def test_a_row_with_no_entry_gets_one_appended(step3):
    resolver = lambda *a, **k: (published("new1", "A New Paper"), "DBLP")  # noqa: E731
    (_u, appended, _sa, not_found), bib, _s = step3(
        "", missing=[{"item_name": "new1", "title": "A New Paper"}], resolver=resolver)
    assert appended == 1 and not_found == []
    assert "@inproceedings{new1" in bib


def test_a_resolved_key_is_written_back_onto_its_row(step3):
    """Without this the row stays keyless, so the next run looks the same paper up
    again -- forever, and the CV never cites it."""
    resolver = lambda *a, **k: (published("new1", "A New Paper"), "DBLP")  # noqa: E731
    _r, _bib, saved = step3(
        "", missing=[{"item_name": "new1", "title": "A New Paper"}], resolver=resolver)
    assert saved["keys"] == {"A New Paper": "new1"}


def test_no_key_is_written_for_a_row_nothing_resolved(step3):
    """A key on the row with no entry under it in orig.bib builds a CV with an
    unresolved \\cite, which is worse than a paper that is merely missing."""
    _r, _bib, saved = step3("", missing=[{"item_name": "new1", "title": "A New Paper"}])
    assert saved["keys"] == {}


def test_an_unresolved_row_is_reported_by_title(step3):
    (_u, _a, _sa, not_found), _bib, _s = step3(
        "", missing=[{"item_name": "new1", "title": "A New Paper"}])
    assert not_found == [("A New Paper", "new1")]


def test_an_unchanged_bib_is_not_rewritten(step3, capsys):
    """orig.bib is tracked in git, so an unconditional write dirties the tree on
    every run and makes CI's staleness check fire for no reason."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    resolver = lambda *a, **k: (text, "arXiv (export API)")  # noqa: E731
    _r, bib, _s = step3(text, resolver=resolver)
    assert bib == text
    assert "left untouched" in capsys.readouterr().out


def test_every_completed_attempt_is_counted_even_when_it_finds_nothing(step3):
    """The count is what sorts hopeless lookups last and what WORKLIST.md reports,
    so it has to rise on a negative answer -- that is the case it exists for."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    _r, _bib, saved = step3(text, missing=[{"item_name": "new1", "title": "New"}],
                            attempts={"k1": 3})
    assert saved["attempts"][-1] == {"k1": 4, "new1": 1}


# ── a lookup that could not be made ──────────────────────────────────────────
#
# "No source has this paper" and "no source answered" look identical at the call
# site, and treating the second as the first is expensive twice over: the retry
# counter rises for something that is not the paper's fault, and the step records
# itself as done, so the answer stays frozen in until an unrelated input changes.

def _silent_source(*_a, **_k):
    resolve_arxiv._note_unanswered("dblp.org")
    return "", resolve_arxiv.UNANSWERED


def test_a_lookup_nobody_answered_is_not_counted_as_an_attempt(step3):
    """One outage would otherwise push every unresolved entry past the
    deprioritization threshold at once, for a week when no source was even asked."""
    _r, _bib, saved = step3("", missing=[{"item_name": "new1", "title": "New"}],
                            attempts={"new1": 2}, resolver=_silent_source)
    assert saved["attempts"][-1] == {"new1": 2}


def test_a_lookup_nobody_answered_is_not_reported_as_needing_a_manual_entry(step3):
    """WORKLIST.md's list is "paste an entry in by hand". A question that was
    never asked does not belong on it."""
    (_u, _a, _sa, not_found), _bib, _s = step3(
        "", missing=[{"item_name": "new1", "title": "New"}], resolver=_silent_source)
    assert not_found == []


def test_a_silent_source_is_reported_as_such(step3, capsys):
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    step3(text, resolver=_silent_source)
    out = capsys.readouterr().out
    assert "got no answer" in out and "k1" in out


def test_progress_is_checkpointed_before_the_run_ends(step3):
    """Resolving ~90 entries is minutes of deliberate rate-limit sleeps. Saving
    only at the end meant a Ctrl-C threw away every lookup the run had made."""
    entries = "".join(_ARXIV_ENTRY.format(key=f"k{i}", title=f"Paper {i}",
                                          eprint=f"2401.{i:05d}")
                      for i in range(12))
    _r, _bib, saved = step3(entries)
    assert len(saved["attempts"]) >= 2, "no mid-loop checkpoint was written"
    assert saved["attempts"][0] == {f"k{i}": 1 for i in range(10)}, (
        "the checkpoint must hold the first ten lookups, not an empty dict")
    assert saved["store"] >= 2


def test_the_second_loop_checkpoints_too(step3):
    """Part B is the slower half -- a row with no key has no arXiv ID to look up,
    so every source gets tried -- which makes it the likelier half to be
    interrupted."""
    missing = [{"item_name": f"new{i}", "title": f"Paper {i}"} for i in range(12)]
    _r, _bib, saved = step3("", missing=missing)
    assert any(len(a) == 10 for a in saved["attempts"]), (
        "no checkpoint after the tenth row with no entry")


def test_the_identifiers_learned_while_resolving_are_saved(step3):
    """Recording them is what makes the next run's citation join an exact lookup
    instead of a title-similarity guess."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    _r, _bib, saved = step3(text)
    assert saved["store"] >= 1


def test_the_resolver_gets_the_arxiv_id_and_the_existing_entry(step3):
    """It needs the entry text because a DOI already recorded in it resolves the
    paper without a search, and the ID because that is the cheapest lookup."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    seen = []

    def resolver(title, arxiv_id, key, content="", store=None):
        seen.append((title, arxiv_id, key, "archivePrefix" in content))
        return ("", "not found")
    step3(text, resolver=resolver)
    assert seen == [("A Paper", "2401.00001", "k1", True)]


def test_a_deprioritised_entry_is_announced(step3, capsys):
    """Sorting the hopeless ones last is invisible unless it is said, and it is the
    reason a run's output order changes between weeks."""
    text = _ARXIV_ENTRY.format(key="k1", title="A Paper", eprint="2401.00001")
    step3(text, attempts={"k1": update._DEPRIORITIZE_AFTER})
    assert "prior attempts sorted last" in capsys.readouterr().out


# ── step 6 ───────────────────────────────────────────────────────────────────

def test_step6_counts_the_open_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(update, "WORKLIST_PATH", str(tmp_path / "WORKLIST.md"))
    (tmp_path / "WORKLIST.md").write_text("# Title\n\n- one\n- two\nprose\n")
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    update.step6_worklist(False)
    assert "2 open item(s)" in capsys.readouterr().out


def test_a_failed_worklist_is_a_warning_not_a_failure(tmp_path, monkeypatch, capsys):
    """The generated CV is still correct; only the to-do summary is missing. It
    must not be the reason an unattended run reports failure."""
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    update.step6_worklist(False)
    assert "Warning: could not generate WORKLIST.md" in capsys.readouterr().out


def test_step6_dry_run_runs_nothing(monkeypatch):
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran the worklist in a dry run"))
    update.step6_worklist(True)


# ── step 7 and the git plumbing, against real local repositories ─────────────

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


@pytest.fixture
def repos(tmp_path):
    """A real repo with a real remote, so the push paths are actually exercised.

    Stubbing subprocess here would test the stub: the behaviour worth proving is
    what git does when the remote has moved, which is the case that used to leave
    every later push rejected until someone pulled by hand.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "master", str(work)], check=True)
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "tracked.txt").write_text("one\n")
    git(work, "add", "tracked.txt")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "master")
    return work, remote


def test_a_change_is_committed_and_pushed(repos, capsys):
    work, remote = repos
    (work / "tracked.txt").write_text("two\n")
    assert update._git_commit_and_push(str(work), ["tracked.txt"], "msg", "origin")
    assert "msg" in git(remote, "log", "--oneline").stdout


def test_nothing_to_commit_is_still_a_success(repos, capsys):
    """The CV may be unchanged since the last run. That is not a failure, and
    step 7 returning False would exit the whole pipeline non-zero."""
    work, _remote = repos
    assert update._git_commit_and_push(str(work), ["tracked.txt"], "msg", "origin")
    assert "Nothing to commit" in capsys.readouterr().out


def test_a_file_that_does_not_exist_is_skipped(repos):
    """The file lists are the same for every fork, and a fork legitimately lacks
    some of them -- venues.yaml, before the first run creates it.
    `git add` on a missing path fails and would take the whole push with it."""
    work, _remote = repos
    (work / "tracked.txt").write_text("two\n")
    assert update._git_commit_and_push(
        str(work), ["tracked.txt", "never_existed.json"], "msg", "origin")


def test_a_moved_remote_is_rebased_onto_rather_than_failing(repos, capsys):
    """Editing the project in Overleaf's own editor advances its remote, after
    which every push from here is rejected. Rebasing makes that self-healing
    instead of a standing manual chore."""
    work, remote = repos
    other = work.parent / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "o")
    (other / "elsewhere.txt").write_text("from overleaf\n")
    git(other, "add", "elsewhere.txt")
    git(other, "commit", "-q", "-m", "edited in Overleaf")
    git(other, "push", "-q", "origin", "master")

    (work / "tracked.txt").write_text("two\n")
    assert update._git_commit_and_push(str(work), ["tracked.txt"], "msg", "origin")
    out = capsys.readouterr().out
    assert "rebasing onto remote" in out and "ok (after rebase)" in out
    # Both sides survived: the rebase must not discard the remote's own edit.
    log = git(remote, "log", "--oneline").stdout
    assert "msg" in log and "edited in Overleaf" in log


def test_a_conflicting_remote_edit_fails_loudly(repos, capsys):
    """A conflict is a real decision, so it stops rather than guessing -- and
    aborts the rebase, leaving the working tree usable."""
    work, remote = repos
    other = work.parent / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "o")
    (other / "tracked.txt").write_text("their version\n")
    git(other, "commit", "-q", "-am", "their edit")
    git(other, "push", "-q", "origin", "master")

    (work / "tracked.txt").write_text("our version\n")
    assert update._git_commit_and_push(str(work), ["tracked.txt"], "msg", "origin") is False
    assert "pull --rebase failed" in capsys.readouterr().out
    assert git(work, "status", "--porcelain").returncode == 0
    assert "rebase" not in git(work, "status").stdout.lower(), (
        "a half-finished rebase would block every later run"
    )


def test_an_unknown_remote_is_reported_not_raised(repos, capsys):
    work, _remote = repos
    (work / "tracked.txt").write_text("two\n")
    assert update._git_commit_and_push(str(work), ["tracked.txt"], "msg", "nope") is False
    assert "FAILED" in capsys.readouterr().out


def test_step7_pushes_the_submodule_before_the_outer_repo(monkeypatch):
    """Overleaf first, so the submodule pointer the outer commit records already
    exists on its remote."""
    order = []
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda repo, files, msg, remote: order.append(repo) or True)
    assert update.step7_push(False) is True
    assert order == [update.OVERLEAF_DIR, update.FILE_DIR]


@pytest.mark.parametrize("results, expected", [
    ([True, True], True),
    ([False, True], False),
    ([True, False], False),
])
def test_step7_succeeds_only_when_both_pushes_do(monkeypatch, results, expected):
    """Half a publish is the failure this pipeline exists to catch: the data on
    GitHub says one thing and the compiled CV says another."""
    pending = list(results)
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda *a, **k: pending.pop(0))
    assert update.step7_push(False) is expected


def test_step7_dry_run_pushes_nothing(monkeypatch):
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda *a, **k: pytest.fail("pushed during a dry run"))
    assert update.step7_push(True) is True


def test_the_overleaf_file_list_includes_the_bst_files():
    """patch_bst_author() edits them on disk, so a fork that changes AUTHOR_NAME
    has its name-bolding rewritten -- and leaving them out of the push meant the
    fix never reached the project that compiles."""
    assert {"planyr-rev.bst", "planyr.bst", "iclr-based.bst"} <= set(update._OVERLEAF_FILES)


def test_the_outer_file_list_includes_the_state_files():
    """identity.json and resolve_attempts.json are generated but deliberately
    committed: losing them means re-resolving every paper and restarting every
    backoff."""
    assert {"identity.json", "resolve_attempts.json", ".pipeline_state.json",
            "WORKLIST.md", "overleaf"} <= set(update._OUTER_FILES)


def test_no_credential_bearing_path_is_in_either_file_list():
    """A public repo. Nothing that could carry an Overleaf token gets staged."""
    for name in update._OUTER_FILES + update._OVERLEAF_FILES:
        assert "config" not in name and "token" not in name.lower()
        assert not name.startswith(".git")


# ── steps 4 and 5 ────────────────────────────────────────────────────────────

def test_step4_returns_what_build_bib_returns(monkeypatch):
    import build_bib
    monkeypatch.setattr(build_bib, "main", lambda: "categories")
    assert update.step4_build_bib(False) == "categories"


def test_step4_dry_run_builds_nothing(monkeypatch):
    import build_bib
    monkeypatch.setattr(build_bib, "main", lambda: pytest.fail("built in a dry run"))
    assert update.step4_build_bib(True) is None


def test_step5_passes_the_categories_through(monkeypatch):
    import rebuild_tex
    seen = []
    monkeypatch.setattr(rebuild_tex, "main", lambda cats: seen.append(cats))
    update.step5_rebuild_tex(False, "categories")
    assert seen == ["categories"]


def test_step5_dry_run_rewrites_nothing(monkeypatch):
    import rebuild_tex
    monkeypatch.setattr(rebuild_tex, "main",
                        lambda cats: pytest.fail("rewrote main.tex in a dry run"))
    update.step5_rebuild_tex(True, None)


# ── the state file the auto-skip depends on ──────────────────────────────────

def test_every_step_input_is_a_path_that_could_exist():
    """A typo in STEP_INPUTS is invisible: a path that never exists hashes to
    nothing consistently, so the step is auto-skipped forever and its output
    silently stops tracking its inputs."""
    for step, inputs in update.STEP_INPUTS.items():
        assert inputs, step
        for path in inputs:
            assert os.path.isabs(path), (step, path)
            assert os.path.isdir(os.path.dirname(path)), (step, path)


def test_build_bib_reruns_when_the_citation_counts_change():
    """It ignored citations.csv, so refreshed counts never reached the
    bibliography."""
    assert update.CITATIONS_CSV in update.STEP_INPUTS["build_bib"]


def test_rebuild_tex_reruns_when_the_h_index_changes():
    """It ignored profile_stats.json, so a new h-index never reached the CV."""
    assert update.STATS_PATH in update.STEP_INPUTS["rebuild_tex"]
