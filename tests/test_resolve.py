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
from bib_utils import parse_bibtex
from identity import IdentityStore
from resolve_arxiv import (
    _get_arxiv_id,
    _is_arxiv,
    gen_key,
    get_missing_bib_entries,
    placeholder_key,
    resolve,
    sort_by_attempts,
    update_bib_inplace,
)

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


def test_published_source_upgrades_the_entry():
    """The venue moves across; the curated title stays. See merge_published."""
    replacement = ('@inproceedings{doe2024preprint, title = {Now Published}, '
                   'booktitle = {ACL 2024}, pages = {1--9}}')
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DBLP")], [])
    assert replaced == 1
    assert "ACL 2024" in text and "1--9" in text
    assert text.lstrip().startswith("@inproceedings")
    assert "A Preprint" in text        # the title is not the source's to change
    assert "Now Published" not in text


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


def test_doi_source_may_upgrade_an_existing_entry():
    """A DOI is exact, so unlike an arXiv result it is allowed to write."""
    assert "DOI (clibib)" in resolve_arxiv._PUBLISHED_SOURCES
    replacement = ('@article{doe2024preprint, title = {Published Version}, '
                   'journal = {A Real Journal}, volume = {12}}')
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DOI (clibib)")], [])
    assert replaced == 1
    assert "A Real Journal" in text


# ── regex replacement must never be treated as a template ────────────────────
#
# A real run died with `re.error: bad escape \i` after making every lookup,
# because re.sub parses its *replacement* for escapes and BibTeX is full of
# backslashes. All of the run's work was lost.

# The booktitle carries the backslashes too, because the venue is the field the
# transplant actually moves -- a LaTeX-safe title is no use if the venue is not.
LATEX_HEAVY = (r'@inproceedings{doe2024preprint,' "\n"
               r'  title = {Mod{\`e}les de langue: an {\it italic} study},' "\n"
               r'  author = {Rapha{\"e}l Doe and Jos{\'e} Roe},' "\n"
               r'  booktitle = {Actes de la Conf{\'e}rence: an {\it italic} venue},' "\n"
               r'  year = {2024}' "\n"
               r'}')


def test_replacement_containing_latex_escapes_does_not_raise():
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", LATEX_HEAVY, "DBLP")], [])
    assert replaced == 1
    assert r"Conf{\'e}rence" in text
    assert r'{\it italic} venue' in text


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
    from bib_utils import extract_field, is_wellformed_entry
    assert "italic" in extract_field(entry["content"], "booktitle")
    assert is_wellformed_entry(entry["beg"] + entry["rest"],
                               expected_key="doe2024preprint")


# ── keys must be unique, and must never contain "nan" ────────────────────────

def test_gen_key_omits_an_unknown_year():
    """A NaN year used to be str()'d into the key: `arvivnanstop`."""
    assert gen_key("Arviv, O", "nan", "Stop Guessing When to Stop") == "arvivstop"
    assert gen_key("Arviv, O", float("nan"), "Stop Guessing") == "arvivstop"
    assert gen_key("Arviv, O", "", "Stop Guessing") == "arvivstop"
    assert "nan" not in placeholder_key("nan", "Some Title")


def test_gen_key_accepts_a_numeric_year():
    assert gen_key("Doe, J", 2024, "Some Paper") == "doe2024some"
    assert gen_key("Doe, J", "2024.0", "Some Paper") == "doe2024some"


def test_gen_key_disambiguates_a_collision_readably():
    """Two distinct 'Every Eval Ever' papers were handed the same key."""
    first = gen_key("Batzner, J", "2026", "Every Eval Ever: Toward a common language")
    second = gen_key("Batzner, J", "2026",
                     "Every Eval Ever: A Unifying Schema and Community Repository",
                     taken={first})
    assert first != second
    assert second.startswith(first), "should extend, not renumber, when it can"


def test_gen_key_falls_back_to_a_suffix_when_words_run_out():
    assert gen_key("Doe, J", "2024", "Alpha", taken={"doe2024alpha"}) == "doe2024alpha2"


def test_missing_entries_never_share_a_key():
    """The invariant: one pass must not hand two rows the same key."""
    df = _df([
        {"Name": "Every Eval Ever: Toward a common language", "Bib": None,
         "Authors": "Batzner, J", "year": 2026},
        {"Name": "Every Eval Ever: A Unifying Schema and Community Repository",
         "Bib": None, "Authors": "Batzner, J", "year": 2026},
        {"Name": "Every Eval Ever: Something Else Again", "Bib": None,
         "Authors": "Batzner, J", "year": 2026},
    ])
    keys = [e["item_name"] for e in get_missing_bib_entries("", df=df)]
    assert len(keys) == len(set(keys)) == 3, keys


def test_generated_key_never_collides_with_an_existing_bib_entry():
    df = _df([{"Name": "A Published Paper Renamed", "Bib": None,
               "Authors": "Doe, Jane", "year": 2024}])
    existing = '@inproceedings{doe2024published, title = {X}}'
    (entry,) = get_missing_bib_entries(existing, df=df)
    assert entry["item_name"] != "doe2024published"


# ── the venue transplant ──────────────────────────────────────────────────────
# Every test here is a real regression: replacing an entry wholesale silently
# deleted seven `pretitle` macros in one run, and the DBLP title reader's brace
# handling decided whether a published version was found at all.

_CURATED = """@article{doe2023thing,
    pretitle={\\COL\\META},
  title        = {The Thing: A Hand-Repaired Title},
  author       = {Doe, Jane and Roe, Richard},
  journal      = {CoRR},
  volume       = {abs/2301.00001},
  year         = {2023},
  eprint       = {2301.00001},
  archiveprefix = {arXiv}
}"""

_DBLP_PUBLISHED = """@inproceedings{doe2023thing,
  author    = {Jane Doe and Richard Roe},
  title     = {\\texttt{Thing}: {A} {DBLP} Title With {B}races},
  booktitle = {Proceedings of Something Real},
  pages     = {1--10},
  publisher = {ACL},
  year      = {2023},
  doi       = {10.0000/REAL.1}
}"""


def test_transplant_keeps_pretitle_title_and_author():
    merged = resolve_arxiv.merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "pretitle={\\COL\\META}" in merged
    assert "A Hand-Repaired Title" in merged
    assert "Doe, Jane and Roe, Richard" in merged
    assert "DBLP Title" not in merged


def test_transplant_moves_the_venue_and_drops_the_preprint_one():
    merged = resolve_arxiv.merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "Proceedings of Something Real" in merged
    assert "pages" in merged and "1--10" in merged
    assert "CoRR" not in merged
    assert merged.lstrip().startswith("@inproceedings")


def test_transplant_keeps_the_arxiv_id():
    """It stays true after publication, and downstream tools match on it."""
    merged = resolve_arxiv.merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "2301.00001" in merged


def test_transplant_output_still_parses_as_one_entry():
    from bib_utils import is_wellformed_entry
    merged = resolve_arxiv.merge_published(_CURATED, _DBLP_PUBLISHED)
    assert is_wellformed_entry(merged, expected_key="doe2023thing")


def test_transplant_falls_back_to_the_original_on_unusable_input():
    assert resolve_arxiv.merge_published(_CURATED, "not bibtex at all") == _CURATED


def test_update_bib_inplace_preserves_pretitle():
    """The end-to-end shape of the seven-macro loss."""
    new_text, n_replaced, _ = update_bib_inplace(
        _CURATED + "\n", [("doe2023thing", _DBLP_PUBLISHED, "DBLP")], [])
    assert n_replaced == 1
    assert "pretitle" in new_text
    assert "Proceedings of Something Real" in new_text


# ── which DBLP result is accepted ─────────────────────────────────────────────

def test_dblp_title_reads_through_brace_groups():
    """`[^}]+` stopped at the first brace, so \\texttt{Holmes} became a 6-char title."""
    bib = '@article{x, title = {\\texttt{Holmes}: {A} Benchmark for Language}, year = {2024}}'
    assert "Benchmark for Language" in resolve_arxiv._dblp_title(bib)


def test_similar_title_from_a_different_year_is_rejected():
    """"Holistic Evaluation of Language Models" is not "Towards Holistic
    Evaluation of Large Audio-Language Models" three years later."""
    candidate = ('@inproceedings{other, title = {Towards Holistic Evaluation of '
                 'Large Audio-Language Models}, booktitle = {EMNLP}, year = {2025}}')
    published, _corr = resolve_arxiv.pick_published(
        [candidate], query_title="Holistic Evaluation of Language Models",
        query_year=2022)
    assert published is None


def test_an_identical_title_keeps_its_publication_lag():
    """ComPEFT's journal version is two years after its preprint."""
    title = "ComPEFT: Compression for Communicating Parameter Efficient Updates"
    candidate = ('@inproceedings{c, title = {' + title + '}, '
                 'booktitle = {Some Venue}, year = {2025}}')
    published, _corr = resolve_arxiv.pick_published(
        [candidate], query_title=title, query_year=2023)
    assert published is not None


def test_a_result_that_does_not_list_the_author_is_rejected():
    """The Slonim mis-resolution, which every title-based guard let through.

    A near-identical title, the right year, and a sole author who is one of the real
    paper's co-authors -- so the only thing that separates it from a genuine
    published version is the author list.
    """
    candidate = ('@inproceedings{isaim, author = {Noam Slonim}, '
                 'title = {Project Debater - an autonomous debating system}, '
                 'booktitle = {ISAIM 2022}, year = {2022}}')
    published, _corr = resolve_arxiv.pick_published(
        [candidate], query_title="An autonomous debating system", query_year=2021)
    assert published is None


def test_a_result_that_does_list_the_author_is_accepted():
    """So the guard above rejects on the author list and not on the title."""
    candidate = ('@inproceedings{ok, author = {Noam Slonim and Leshem Choshen}, '
                 'title = {Project Debater - an autonomous debating system}, '
                 'booktitle = {ISAIM 2022}, year = {2022}}')
    published, _corr = resolve_arxiv.pick_published(
        [candidate], query_title="An autonomous debating system", query_year=2021)
    assert published is not None


def test_year_guard_does_not_fire_without_a_query_year():
    candidate = ('@inproceedings{c, title = {A Somewhat Similar Paper Title Here}, '
                 'booktitle = {V}, year = {2025}}')
    published, _corr = resolve_arxiv.pick_published(
        [candidate], query_title="A Somewhat Similar Paper Title Here")
    assert published is not None


# ── the source ladder ─────────────────────────────────────────────────────────
#
# resolve() asks five services in a fixed order, and the order is the whole
# design: DBLP and the ACL Anthology give the version of record, OpenAlex may
# hand back a preprint, and arXiv always does. Only the labels in
# _PUBLISHED_SOURCES may overwrite an existing orig.bib entry, so a step that
# quietly moved up the ladder would let a preprint replace a published paper.

_TITLE = "A Paper With A Title Long Enough To Search On"


@pytest.fixture
def ladder(monkeypatch):
    """Every source stubbed to return nothing; a test enables the ones it needs.

    Anything left unstubbed would reach the network, so they are all closed by
    default rather than listed per test.
    """
    monkeypatch.setattr(resolve_arxiv, "time", _NoSleep)
    closed = {
        "search_dblp": lambda t: [],
        "query_s2_by_arxiv": lambda a: None,
        "query_s2_by_title": lambda t, y="": None,
        "fetch_acl_bib": lambda i, k: None,
        "fetch_openreview_bib": lambda i, k: None,
        "search_openalex": lambda t: None,
        "fetch_arxiv_bib": lambda a, k, known_title=None: f"@misc{{{k}, arxiv}}",
        "_clibib_fetch": lambda: None,
    }
    for name, stub in closed.items():
        monkeypatch.setattr(resolve_arxiv, name, stub)

    def _open(**stubs):
        for name, stub in stubs.items():
            monkeypatch.setattr(resolve_arxiv, name, stub)
    return _open


def test_an_acl_record_is_preferred_over_openreview_and_openalex(ladder):
    """The Anthology entry is the citation the venue itself publishes."""
    ladder(query_s2_by_title=lambda t, y="": {"externalIds": {"ACL": "2024.acl-1.1"}},
           fetch_acl_bib=lambda i, k: f"@inproceedings{{{k}, acl={i}}}",
           fetch_openreview_bib=lambda i, k: pytest.fail("asked OpenReview anyway"),
           search_openalex=lambda t: pytest.fail("asked OpenAlex anyway"))
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "ACL Anthology"
    assert "2024.acl-1.1" in bib


def test_s2_is_queried_by_arxiv_id_when_there_is_one(ladder):
    """An identifier cannot return the wrong paper; a title can."""
    asked = []
    ladder(query_s2_by_arxiv=lambda a: asked.append(a) or None,
           query_s2_by_title=lambda t, y="": pytest.fail(
               "searched by title with an arXiv id in hand"))
    resolve(_TITLE, "2401.00001", "k1", "")
    assert asked == ["2401.00001"]


def test_openreview_is_found_through_s2s_venue_url(ladder):
    ladder(query_s2_by_title=lambda t, y="": {
               "externalIds": {},
               "publicationVenue": {"url": "https://openreview.net/forum?id=AbC123"}},
           fetch_openreview_bib=lambda i, k: f"@inproceedings{{{k}, forum={i}}}")
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "OpenReview"
    assert "AbC123" in bib


def test_an_openreview_url_already_in_the_entry_is_used(ladder):
    """No lookup needed: a previous run, or the author, already recorded it."""
    ladder(fetch_openreview_bib=lambda i, k: f"@inproceedings{{{k}, forum={i}}}")
    bib, source = resolve(_TITLE, None, "k1",
                          "url = {https://openreview.net/forum?id=XyZ789}")
    assert source == "OpenReview"
    assert "XyZ789" in bib


def test_a_source_that_returns_nothing_falls_through_to_the_next(ladder):
    """Each fetch can fail on its own; the ladder must keep descending rather
    than treating an empty response as a resolution."""
    ladder(query_s2_by_title=lambda t, y="": {
               "externalIds": {"ACL": "2024.acl-1.1"},
               "publicationVenue": {"url": "https://openreview.net/forum?id=AbC"}},
           fetch_acl_bib=lambda i, k: None,
           fetch_openreview_bib=lambda i, k: None,
           search_openalex=lambda t: {"doi": "https://doi.org/10.1/x"},
           openalex_to_bibtex=lambda w, k: (
               f"@article{{{k}, title = {{T}}, journal = {{A Real Journal}}}}", True))
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "OpenAlex"
    assert "A Real Journal" in bib


def test_an_openalex_preprint_is_labelled_as_one(ladder):
    """OpenAlex indexes preprints alongside published work, and the label is the
    only thing standing between one and an existing published entry."""
    ladder(search_openalex=lambda t: {"doi": "https://doi.org/10.48550/arXiv.2401.1"},
           openalex_to_bibtex=lambda w, k: (f"@misc{{{k}, title = {{T}}}}", False))
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "OpenAlex (preprint)"
    assert source not in resolve_arxiv._PUBLISHED_SOURCES, \
        "a preprint must never replace an existing entry"


def test_an_arxiv_doi_from_openalex_is_not_recorded(ladder):
    """10.48550/... only ever resolves back to the preprint, so recording it
    would make the next run spend a request being rejected by the rank guard."""
    ladder(search_openalex=lambda t: {"doi": "https://doi.org/10.48550/arXiv.2401.1"},
           openalex_to_bibtex=lambda w, k: (f"@misc{{{k}, title = {{T}}}}", False))
    store = IdentityStore()
    resolve(_TITLE, None, "k1", "", store=store)
    assert not (store.records.get("k1") or {}).get("doi")


def test_a_known_arxiv_doi_is_never_looked_up(ladder):
    """It did this 10 times on a real run, and every result was then rejected."""
    asked = []
    ladder(_clibib_fetch=lambda: lambda ident: asked.append(ident) or "@misc{z}")
    store = IdentityStore()
    store.record("k1", doi="10.48550/arXiv.2401.00001")
    resolve(_TITLE, None, "k1", "", store=store)
    assert asked == []


def test_dblps_corr_entry_is_used_before_the_export_api(ladder):
    """Same preprint, one fewer request -- DBLP already returned it."""
    corr = ('@article{DBLP:journals/corr/abs-2401-00001, title = {T}, '
            'journal = {CoRR}, volume = {abs/2401.00001}}')
    ladder(search_dblp=lambda t: [corr],
           fetch_arxiv_bib=lambda a, k, known_title=None: pytest.fail(
               "went to the arXiv API with a CoRR entry already in hand"))
    bib, source = resolve(_TITLE, "2401.00001", "k1", "")
    assert source == "arXiv (DBLP/CoRR)"
    assert bib.startswith("@article{k1,"), "the key must be rewritten to ours"


def test_the_arxiv_fallback_is_given_the_title_it_searched_on(ladder):
    """The export API's own metadata can be a stub; the table's title is better."""
    seen = {}
    ladder(fetch_arxiv_bib=lambda a, k, known_title=None:
           seen.update(id=a, title=known_title) or f"@misc{{{k}}}")
    bib, source = resolve(_TITLE, "2401.00001", "k1", "")
    assert source == "arXiv (export API)"
    assert seen == {"id": "2401.00001", "title": _TITLE}


def test_nothing_anywhere_is_reported_as_not_found(ladder):
    """Not an exception, and not an empty entry written to orig.bib."""
    bib, source = resolve(_TITLE, None, "k1", "")
    assert (bib, source) == ("", "not found")


def test_the_s2_crosswalk_is_recorded_even_when_nothing_resolves(ladder):
    """The point of asking S2 at all: one response binds every identifier this
    paper has, so a later run matches on an id instead of guessing from a title.
    """
    ladder(query_s2_by_title=lambda t, y="": {
        "externalIds": {"ArXiv": "2401.00001", "DOI": "10.1/x",
                        "ACL": "2024.acl-1.1"}})
    store = IdentityStore()
    resolve(_TITLE, None, "k1", "", store=store)
    record = store.records.get("k1") or {}
    assert record.get("arxiv") == "2401.00001"
    assert record.get("doi") == "10.1/x"


def test_the_version_of_record_from_dblp_wins_outright(ladder):
    """The first rung, and the one that resolves most papers: a DBLP hit stops the
    ladder before a single further request is spent."""
    published = ('@inproceedings{DBLP:conf/acl/DoeJ24, '
                 f'title = {{{_TITLE}}}, booktitle = {{ACL}}, year = {{2024}}}}')
    ladder(search_dblp=lambda t: [published],
           query_s2_by_title=lambda t, y="": pytest.fail("kept going after DBLP"))
    bib, source = resolve(_TITLE, None, "k1", "year = {2024}")
    assert source == "DBLP"
    assert bib.startswith("@inproceedings{k1,"), "the key must be rewritten to ours"


def test_dblps_identifiers_are_recorded(ladder):
    published = ('@inproceedings{DBLP:conf/acl/DoeJ24, '
                 f'title = {{{_TITLE}}}, booktitle = {{ACL}}, year = {{2024}}, '
                 'doi = {10.18653/v1/2024.acl-1.1}}')
    ladder(search_dblp=lambda t: [published])
    store = IdentityStore()
    resolve(_TITLE, None, "k1", "year = {2024}", store=store)
    assert (store.records.get("k1") or {}).get("doi") == "10.18653/v1/2024.acl-1.1"


# ── rewriting orig.bib when the file has moved on ────────────────────────────
#
# The lookups happen over minutes, against a file the author may be editing. An
# update that cannot find what it meant to change must skip that entry, not
# guess at where it went.

def test_an_update_for_an_entry_no_longer_in_the_file_is_skipped():
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("some_other_key", PUBLISHED, "DBLP")], [])
    assert (replaced, appended) == (0, 0)
    assert text == PREPRINT


def test_an_update_that_would_change_nothing_is_not_counted():
    """merge_published transplants a venue. If the entry already has that venue
    there is nothing to do, and reporting it as upgraded would be a lie."""
    text, replaced, _ = update_bib_inplace(
        PUBLISHED, [("doe2024paper", PUBLISHED, "DBLP")], [])
    assert replaced == 0
    assert text == PUBLISHED


def test_a_quoted_acl_style_entry_survives_the_transplant():
    """The Anthology quotes its fields. Assuming braces here is what previously
    replaced a closing quote with a brace and produced unparseable BibTeX."""
    quoted = ('@misc{doe2024preprint,\n    title = "A Preprint",\n'
              '    year = "2024",\n    volume = 12\n}')
    replacement = ('@inproceedings{doe2024preprint, title = "A Preprint", '
                   'booktitle = "ACL", year = "2024"}')
    text, replaced, _ = update_bib_inplace(
        quoted, [("doe2024preprint", replacement, "ACL Anthology")], [])
    assert replaced == 1
    (entry,) = parse_bibtex(text)
    assert entry["title"] == "A Preprint"
    assert "ACL" in entry["content"]


# ── table cells that are not what the column promises ────────────────────────

def test_a_non_numeric_paper_flag_does_not_exclude_the_row():
    """The column means "is this a paper", and only an explicit 0 says no. A note
    typed into it must not silently drop the paper from the CV."""
    df = _df([{"Name": "A Paper", "Bib": None, "Authors": "Doe, Jane",
               "year": 2024, "Paper": "yes"}])
    assert len(get_missing_bib_entries("", df=df)) == 1


def test_a_paper_flag_of_zero_excludes_the_row():
    df = _df([{"Name": "A Proceedings Volume", "Bib": None, "Authors": "Doe, Jane",
               "year": 2024, "Paper": 0}])
    assert get_missing_bib_entries("", df=df) == []


def test_a_year_that_is_not_a_number_is_kept_as_written():
    """"to appear", "2024a", "in press": all real cells. The key generator has to
    take them rather than raise in the middle of a run."""
    df = _df([{"Name": "A Forthcoming Paper", "Bib": None, "Authors": "Doe, Jane",
               "year": "to appear"}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"].startswith("doe")
    assert "nan" not in entry["item_name"]
