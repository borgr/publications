"""Resolver behaviour that does not need the network.

The dangerous paths here are the ones that *write* to orig.bib, so the tests
concentrate on what may replace an existing entry and what may not.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolve_arxiv
from resolve_arxiv import (_is_arxiv, _get_arxiv_id, gen_key,
                          get_missing_bib_entries, placeholder_key, resolve,
                          sort_by_attempts, update_bib_inplace)
from bib_utils import parse_bibtex
from identity import IdentityStore

PUBLISHED = '''@inproceedings{doe2024paper,
  title = {A Published Paper},
  booktitle = {ACL},
  year = {2024}
}'''

PREPRINT = '''@misc{doe2024preprint,
  title = {A Preprint},
  eprint = {2401.00001},
  archivePrefix = {arXiv},
  year = {2024}
}'''


# ── which entries are candidates for upgrading ───────────────────────────────

def test_published_inproceedings_is_not_treated_as_a_preprint():
    (entry,) = parse_bibtex(PUBLISHED)
    assert not _is_arxiv(entry)


def test_preprint_is_detected():
    (entry,) = parse_bibtex(PREPRINT)
    assert _is_arxiv(entry)
    assert _get_arxiv_id(entry) == "2401.00001"


def test_arxiv_id_is_found_in_a_url_when_no_eprint_field():
    (entry,) = parse_bibtex(
        '@misc{k, title = {T}, url = {https://arxiv.org/abs/2312.09876}}')
    assert _get_arxiv_id(entry) == "2312.09876"


# ── the guard against searching on a non-title ───────────────────────────────

def test_resolve_refuses_to_search_on_an_empty_title(monkeypatch):
    """A title search on a placeholder returns an unrelated paper, and a match
    would then overwrite a good entry. Ten entries used to parse with the
    sentinel title "Title not found"."""
    called = []
    monkeypatch.setattr(resolve_arxiv, "search_dblp",
                        lambda t: called.append(t) or [])
    bib, source = resolve("", None, "somekey")
    assert called == []
    assert bib == ""
    assert source == "no usable title"


def test_resolve_with_no_title_but_an_arxiv_id_falls_back_to_arxiv(monkeypatch):
    monkeypatch.setattr(resolve_arxiv, "search_dblp",
                        lambda t: pytest.fail("must not search on a blank title"))
    monkeypatch.setattr(resolve_arxiv, "fetch_arxiv_bib",
                        lambda aid, key: f"@misc{{{key}, eprint={{{aid}}}}}")
    bib, source = resolve("", "2401.00001", "somekey")
    assert "2401.00001" in bib
    assert source == "arXiv (no usable title)"


# ── in-place rewriting of orig.bib ───────────────────────────────────────────

def test_only_published_sources_replace_an_existing_entry():
    """An arXiv-sourced result must never overwrite what is already there."""
    replacement = '@misc{doe2024preprint, title = {Should Not Land}}'
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "arXiv (export API)")], [])
    assert replaced == 0
    assert "Should Not Land" not in text


def test_published_source_replaces_the_entry():
    replacement = '@inproceedings{doe2024preprint, title = {Now Published}}'
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DBLP")], [])
    assert replaced == 1
    assert "Now Published" in text
    assert "A Preprint" not in text


def test_appending_skips_a_key_that_already_exists():
    text, _, appended = update_bib_inplace(
        PUBLISHED, [], [("doe2024paper", "@misc{doe2024paper, title = {Dup}}")])
    assert appended == 0
    assert "Dup" not in text


def test_appended_entry_is_parseable_afterwards():
    text, _, appended = update_bib_inplace(
        PUBLISHED, [], [("new2024key", '@misc{new2024key, title = {Fresh}}')])
    assert appended == 1
    assert {e["item_name"] for e in parse_bibtex(text)} == {"doe2024paper", "new2024key"}


# ── key generation ───────────────────────────────────────────────────────────

def test_gen_key_shape():
    assert gen_key("Yadav, Prateek and Choshen, Leshem", "2023",
                   "TIES-Merging: Resolving Interference") == "yadav2023ties"


def test_gen_key_skips_stopwords():
    assert gen_key("Doe, Jane", "2024", "On the Weaknesses of RL") == "doe2024weaknesses"


def test_placeholder_key_is_deterministic():
    assert placeholder_key("2024", "A Paper With No Authors") == \
           placeholder_key("2024", "A Paper With No Authors")


# ── the unified missing-entry query ──────────────────────────────────────────

def _df(rows):
    return pd.DataFrame(rows)


def test_missing_entry_preserves_a_hand_assigned_key():
    """A key typed into the table but not yet resolved must not be regenerated."""
    df = _df([{"Name": "A Paper", "Bib": "mykey2024", "Authors": "Doe, Jane",
               "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"] == "mykey2024"


def test_missing_entry_generates_a_key_from_authors():
    df = _df([{"Name": "TIES-Merging: Resolving Interference", "Bib": None,
               "Authors": "Yadav, Prateek", "year": 2023}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"] == "yadav2023ties"


def test_missing_entry_without_authors_uses_a_placeholder():
    df = _df([{"Name": "An Orphan Paper", "Bib": None, "Authors": None, "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"].startswith("unknown2024")


def test_row_already_present_in_the_bib_is_not_missing():
    df = _df([{"Name": "A Published Paper", "Bib": "doe2024paper",
               "Authors": "Doe, Jane", "year": 2024}])
    assert get_missing_bib_entries(PUBLISHED, df=df) == []


def test_nan_bib_cell_is_treated_as_empty():
    """pandas turns blank cells into the string 'nan' via str(); it is not a key."""
    df = _df([{"Name": "A Paper", "Bib": float("nan"), "Authors": "Doe, Jane",
               "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert "nan" not in entry["item_name"]


def test_rows_without_a_name_are_ignored():
    df = _df([{"Name": None, "Bib": None, "Authors": None, "year": 2024}])
    assert get_missing_bib_entries("", df=df) == []


# ── retry prioritisation ─────────────────────────────────────────────────────

def test_repeatedly_failing_entries_sort_last():
    """Fresh entries should get the rate-limited S2 quota first."""
    candidates = [{"item_name": "tired"}, {"item_name": "fresh"}]
    attempts = {"tired": resolve_arxiv._DEPRIORITIZE_AFTER + 1}
    assert [e["item_name"] for e in sort_by_attempts(candidates, attempts)] == \
           ["fresh", "tired"]


# ── clibib, used only for identifier lookups ─────────────────────────────────
#
# Measured against this repo's own unresolved papers, clibib's free-text title
# search returned a confidently wrong paper 2 times in 5 without raising, so it
# is wired in for DOIs only. These tests stub it out: no network in the suite.

class _NoSleep:
    @staticmethod
    def sleep(_seconds):
        pass


def test_doi_lookup_is_used_when_a_doi_is_known(monkeypatch):
    """Fills a real gap: this module has no other DOI resolver."""
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "time", _NoSleep)
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch",
                        lambda: lambda ident: f"@article{{zotero_key, doi={{{ident}}}}}")

    store = IdentityStore()
    store.record("k1", doi="10.1038/s41586-021-03215-w")
    bib, source = resolve("A Journal Paper With A Long Enough Title", None, "k1",
                          "", store=store)
    assert source == "DOI (clibib)"
    assert "10.1038/s41586-021-03215-w" in bib
    assert bib.startswith("@article{k1,"), "the key must be rewritten to ours"


def test_doi_is_taken_from_the_existing_entry_when_not_yet_recorded(monkeypatch):
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "time", _NoSleep)
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch",
                        lambda: lambda ident: f"@article{{z, doi={{{ident}}}}}")
    bib, source = resolve("A Journal Paper With A Long Enough Title", None, "k1",
                          "doi = {10.1234/abcd}")
    assert source == "DOI (clibib)"
    assert "10.1234/abcd" in bib


def test_title_is_never_passed_to_clibib(monkeypatch):
    """The measured failure mode: a title search silently returns another paper."""
    seen = []
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "time", _NoSleep)
    monkeypatch.setattr(resolve_arxiv, "fetch_arxiv_bib", lambda a, k: "")
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch",
                        lambda: lambda ident: seen.append(ident) or "@misc{z}")
    resolve("Every eval ever: Toward a common language for ai eval reporting",
            None, "k1", "")
    assert seen == [], f"clibib was called with a non-identifier: {seen}"


def test_doi_lookup_is_skipped_when_clibib_is_absent(monkeypatch):
    """clibib is optional; without it the resolver behaves as it did before."""
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "time", _NoSleep)
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: None)
    store = IdentityStore()
    store.record("k1", doi="10.1/x")
    bib, source = resolve("A Paper With Quite A Long Title Here", None, "k1", "",
                          store=store)
    assert source == "not found"


def test_clibib_failure_degrades_rather_than_raising(monkeypatch):
    def explode(_ident):
        raise RuntimeError("translation server down")
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: explode)
    assert resolve_arxiv.fetch_by_doi("10.1/x", "k1") is None


def test_clibib_non_bibtex_response_is_rejected(monkeypatch):
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch",
                        lambda: lambda i: "<html>error</html>")
    assert resolve_arxiv.fetch_by_doi("10.1/x", "k1") is None


def test_doi_source_may_replace_an_existing_entry():
    """A DOI is exact, so unlike an arXiv result it is allowed to replace."""
    assert "DOI (clibib)" in resolve_arxiv._PUBLISHED_SOURCES
    replacement = '@article{doe2024preprint, title = {Published Version}}'
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DOI (clibib)")], [])
    assert replaced == 1
    assert "Published Version" in text


# ── regex replacement must never be treated as a template ────────────────────
#
# A real run died with `re.error: bad escape \i` after making every lookup,
# because re.sub parses its *replacement* for escapes and BibTeX is full of
# backslashes. All of the run's work was lost.

LATEX_HEAVY = (r'@inproceedings{doe2024preprint,' "\n"
               r'  title = {Mod{\`e}les de langue: an {\it italic} study},' "\n"
               r'  author = {Rapha{\"e}l Doe and Jos{\'e} Roe},' "\n"
               r'  booktitle = {ACL},' "\n"
               r'  year = {2024}' "\n"
               r'}')


def test_replacement_containing_latex_escapes_does_not_raise():
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", LATEX_HEAVY, "DBLP")], [])
    assert replaced == 1
    assert r'{\it italic}' in text
    assert "A Preprint" not in text


def test_appending_an_entry_with_latex_escapes_does_not_raise():
    entry = LATEX_HEAVY.replace("doe2024preprint", "new2024latex")
    text, _, appended = update_bib_inplace(PUBLISHED, [], [("new2024latex", entry)])
    assert appended == 1
    assert {e["item_name"] for e in parse_bibtex(text)} == {"doe2024paper", "new2024latex"}


def test_replace_key_handles_latex_escapes():
    out = resolve_arxiv._replace_key(LATEX_HEAVY, "renamed2024key")
    assert out.startswith("@inproceedings{renamed2024key,")
    assert r'{\`e}' in out


def test_replace_key_preserves_a_key_with_punctuation():
    src = '@article{DBLP:journals/corr/abs-2404-1, title = {T}}'
    assert resolve_arxiv._replace_key(src, "DBLP:conf/acl/X24").startswith(
        "@article{DBLP:conf/acl/X24,")


def test_a_backslash_heavy_entry_survives_a_full_round_trip():
    """The invariant: what goes in must parse back out identically."""
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", LATEX_HEAVY, "DBLP")], [])
    (entry,) = [e for e in parse_bibtex(text) if e["item_name"] == "doe2024preprint"]
    assert "italic" in entry["title"]
