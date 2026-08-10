"""Finding a published version: which source is asked, and which answer is kept.

No network: every source is stubbed. What is worth pinning down is the ladder --
which source is tried when, and which of its answers is accepted -- because a
wrong answer accepted here becomes a CV that cites the wrong paper. Writing any
of it back to orig.bib is bib_edit's job, and tests/test_bib_edit.py's.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolve_arxiv
from bib_edit import _PUBLISHED_SOURCES
from identity import IdentityStore
from resolve_arxiv import resolve, sort_by_attempts

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

_JOURNAL_TITLE = "A Journal Paper With A Long Enough Title"


def _clibib_returning(monkeypatch, title=_JOURNAL_TITLE, year=""):
    """Stub clibib with an entry for `title`, echoing back the DOI it was asked for."""
    def _fetch(ident):
        return (f"@article{{zotero_key, title = {{{title}}}, "
                f"{f'year = {{{year}}}, ' if year else ''}doi = {{{ident}}}}}")
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: _fetch)


def test_doi_lookup_is_used_when_a_doi_is_known(monkeypatch):
    """Fills a real gap: this module has no other DOI resolver."""
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    _clibib_returning(monkeypatch)

    store = IdentityStore()
    store.record("k1", doi="10.1038/s41586-021-03215-w")
    bib, source = resolve(_JOURNAL_TITLE, None, "k1", "", store=store)
    assert source == "DOI (clibib)"
    assert "10.1038/s41586-021-03215-w" in bib
    assert bib.startswith("@article{k1,"), "the key must be rewritten to ours"


def test_doi_is_taken_from_the_existing_entry_when_not_yet_recorded(monkeypatch):
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    _clibib_returning(monkeypatch)
    bib, source = resolve(_JOURNAL_TITLE, None, "k1", "doi = {10.1234/abcd}")
    assert source == "DOI (clibib)"
    assert "10.1234/abcd" in bib


def test_title_is_never_passed_to_clibib(monkeypatch):
    """The measured failure mode: a title search silently returns another paper."""
    seen = []
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "search_openalex", lambda t: None)
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
    monkeypatch.setattr(resolve_arxiv, "search_openalex", lambda t: None)
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
    assert resolve_arxiv.fetch_by_doi("10.1/x", "k1", _JOURNAL_TITLE) is None


def test_clibib_non_bibtex_response_is_rejected(monkeypatch):
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch",
                        lambda: lambda i: "<html>error</html>")
    assert resolve_arxiv.fetch_by_doi("10.1/x", "k1", _JOURNAL_TITLE) is None


# ── a DOI can be exact and still be the wrong paper ───────────────────────────

_ASKED = "Every eval ever: Toward a common language for AI eval reporting"
_LANCET_BIB = (
    "@article{Collaborators2025,\n"
    "  title = {Global burden of 292 causes of death in 204 countries and "
    "territories and 660 subnational locations, 1990-2023: a systematic analysis "
    "for the Global Burden of Disease Study 2023},\n"
    "  journal = {The Lancet},\n  year = {2025},\n"
    "  doi = {10.1016/S0140-6736(25)01917-8},\n}"
)


def test_a_doi_that_resolves_to_a_different_paper_is_refused(monkeypatch):
    """The entry that reached the CV. Nothing about it is malformed -- it is a real
    paper, well-formed, with a real DOI and a real venue. The only thing wrong with
    it is that it is not the paper that was asked for, and until this check that
    was the one property nothing tested."""
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: lambda i: _LANCET_BIB)
    assert resolve_arxiv.fetch_by_doi(
        "10.1016/S0140-6736(25)01917-8", "batzner2026everyeval", _ASKED, 2026) is None


def test_a_retitled_published_version_is_still_accepted(monkeypatch):
    """The guard has to leave room for the thing this rung is for. Journals retitle
    papers: the Data Provenance Initiative's article scores 0.77 against its
    preprint's title, above the floor, and its year agrees."""
    bib = ("@article{z, title = {A large-scale audit of dataset licensing and "
           "attribution in AI}, year = {2024}, doi = {10.1038/s42256-024-00878-8}}")
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: lambda i: bib)
    asked = ("The Data Provenance Initiative: A Large Scale Audit of Dataset "
             "Licensing & Attribution in AI")
    assert resolve_arxiv.fetch_by_doi("10.1038/s42256-024-00878-8", "k1",
                                      asked, 2023) is not None


def test_the_whole_chain_that_printed_a_lancet_paper_in_the_cv(monkeypatch):
    """End to end, with every source answering exactly as it did on the day.

    Each step was defensible on its own: S2's search returned its best guess, the
    crosswalk took the DOI from the record it was handed, and clibib resolved that
    DOI exactly. The paper still came out wrong, so the test is of the chain and
    not of any one link.
    """
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "search_openalex", lambda t: None)
    monkeypatch.setattr(resolve_arxiv, "_http_get_json", lambda url, **kw: {"data": [
        {"title": _LANCET_BIB.split("title = {")[1].split("},")[0],
         "year": 2025,
         "externalIds": {"DOI": "10.1016/S0140-6736(25)01917-8",
                         "CorpusId": 282069313}}]})
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: lambda i: _LANCET_BIB)

    store = IdentityStore()
    bib, source = resolve(_ASKED, None, "batzner2026everyeval",
                          "year = {2026}", store=store)

    assert source != "DOI (clibib)", "the CV got the global burden of disease"
    assert "0140-6736" not in (bib or "")
    record = store.records.get("batzner2026everyeval") or {}
    assert "doi" not in record, "a rejected crosswalk must not outlive the run"
    assert record.get("s2") is None


def test_a_wrong_doi_already_in_the_store_does_not_resurface(monkeypatch):
    """The half of the failure the upstream guard cannot reach.

    A DOI does not have to be learned this run to be wrong. It can be sitting in
    the identity store, learned by the version of this code that had no guard, or
    typed into orig.bib by hand. On that path S2 is never asked and nothing
    upstream gets a vote, so the check at the point of use is the only one there
    is -- without it, one bad run's DOI is permanent and reappears every week.
    """
    monkeypatch.setattr(resolve_arxiv, "search_dblp", lambda t: [])
    monkeypatch.setattr(resolve_arxiv, "search_openalex", lambda t: None)
    monkeypatch.setattr(resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(resolve_arxiv, "_clibib_fetch", lambda: lambda i: _LANCET_BIB)

    store = IdentityStore()
    store.record("batzner2026everyeval", doi="10.1016/S0140-6736(25)01917-8")
    bib, source = resolve(_ASKED, None, "batzner2026everyeval",
                          "year = {2026}", store=store)
    assert source == "not found"
    assert "0140-6736" not in (bib or "")

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


def _acl_entry(acl_id, key, title=_TITLE):
    """What the Anthology actually serves: an entry that names the paper.

    The stubs here used to return `@inproceedings{k, acl=...}` with no title at
    all, which let these tests pass while saying nothing about whether the entry
    was the right paper's -- the property the ladder now checks, and the one the
    Lancet mis-resolution turned on.
    """
    return f"@inproceedings{{{key},\n  title = {{{title}}},\n  acl = {{{acl_id}}}\n}}"


def _openreview_entry(forum_id, key, title=_TITLE):
    return (f"@inproceedings{{{key},\n  title = {{{title}}},\n"
            f"  url = {{https://openreview.net/forum?id={forum_id}}}\n}}")


@pytest.fixture
def ladder(monkeypatch):
    """Every source stubbed to return nothing; a test enables the ones it needs.

    Closed by default rather than listed per test: conftest.py fails a test that
    reaches the network, and a test about which rung wins should say which rungs
    answered, not discover it from whichever ones happened to be left open.
    """
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
           fetch_acl_bib=_acl_entry,
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
           fetch_openreview_bib=_openreview_entry)
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "OpenReview"
    assert "AbC123" in bib


def test_an_openreview_url_already_in_the_entry_is_used(ladder):
    """No lookup needed: a previous run, or the author, already recorded it."""
    ladder(fetch_openreview_bib=_openreview_entry)
    bib, source = resolve(_TITLE, None, "k1",
                          "url = {https://openreview.net/forum?id=XyZ789}")
    assert source == "OpenReview"
    assert "XyZ789" in bib


def test_an_acl_entry_for_a_different_paper_is_not_this_paper(ladder):
    """The ACL ID comes out of an S2 record, so it is only ever as good as the
    record it came from -- and for a paper with no arXiv ID of its own, that record
    came from a title search. The fetch is exact either way; being exact about the
    wrong ID is the failure mode, so the entry has to be checked, not the lookup.
    """
    ladder(query_s2_by_title=lambda t, y="": {"externalIds": {"ACL": "2024.acl-1.1"}},
           fetch_acl_bib=lambda i, k: _acl_entry(i, k, "Some Entirely Other Paper"),
           search_openalex=lambda t: None)
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source != "ACL Anthology"
    assert "2024.acl-1.1" not in (bib or "")


def test_an_openreview_entry_for_a_different_paper_is_not_this_paper(ladder):
    ladder(query_s2_by_title=lambda t, y="": {
               "externalIds": {},
               "publicationVenue": {"url": "https://openreview.net/forum?id=AbC123"}},
           fetch_openreview_bib=lambda i, k: _openreview_entry(i, k, "Another Paper"))
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source != "OpenReview"


def test_a_rejected_rung_lets_a_later_one_answer(ladder):
    """Rejecting an entry is not the same as concluding the paper is unpublished:
    the ladder has to carry on to the sources that have not answered yet."""
    ladder(query_s2_by_title=lambda t, y="": {"externalIds": {"ACL": "2024.acl-1.1"}},
           fetch_acl_bib=lambda i, k: _acl_entry(i, k, "Some Entirely Other Paper"),
           search_openalex=lambda t: {"doi": "https://doi.org/10.1/x"},
           openalex_to_bibtex=lambda w, k: (
               f"@article{{{k}, title = {{{_TITLE}}}, journal = {{A Real Journal}}}}",
               True))
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "OpenAlex"


# ── a table row with no bib entry, whose arXiv ID the store already knows ──────

def test_a_remembered_arxiv_id_is_used_instead_of_a_title_search(ladder):
    """Step 3's Part B resolves table rows that have no entry to read an ID from,
    so it passes None -- but a previous run may have learned the ID anyway, from
    DBLP or from S2's crosswalk, and asking by identifier is both exact and
    cheaper than asking S2 to search."""
    asked = []
    ladder(query_s2_by_arxiv=lambda a: asked.append(a) or None,
           query_s2_by_title=lambda t, y="": pytest.fail(
               "searched by title with a remembered arXiv id in hand"))
    store = IdentityStore()
    store.record("k1", arxiv="2510.26183")
    resolve(_TITLE, None, "k1", "", store=store)
    assert asked == ["2510.26183"]


def test_the_callers_arxiv_id_wins_over_the_remembered_one(ladder):
    """The caller's comes from the entry being refreshed, which is this paper by
    construction. The store's is whatever some earlier run concluded."""
    asked = []
    ladder(query_s2_by_arxiv=lambda a: asked.append(a) or None)
    store = IdentityStore()
    store.record("k1", arxiv="2510.26183")
    resolve(_TITLE, "2401.00001", "k1", "", store=store)
    assert asked == ["2401.00001"]


def test_a_remembered_arxiv_id_does_not_become_a_preprint_entry(ladder):
    """It is used to ask questions, not to answer them. An entry built straight
    from the ID would be a preprint nothing checked, published into the CV on the
    authority of an earlier run's guess -- and for these rows there is no existing
    entry to compare it against, which is why they are in Part B."""
    ladder(fetch_arxiv_bib=lambda a, k, known_title=None: pytest.fail(
        f"built an entry from a remembered id: {a}"))
    store = IdentityStore()
    store.record("k1", arxiv="2510.26183")
    bib, source = resolve(_TITLE, None, "k1", "", store=store)
    assert (bib, source) == ("", "not found")


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
    assert source not in _PUBLISHED_SOURCES, \
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


# ── a source that did not answer is not a source that said no ────────────────
#
# DBLP rate-limits a long run, and used to do it invisibly: the refusal read as
# "DBLP has no published version", the ladder fell through to the preprint, and a
# paper that came out at ACL went back to being cited as arXiv with nothing logged.

def _silent_dblp(_title):
    resolve_arxiv._note_unanswered("dblp.org")
    return None


def test_a_silent_dblp_does_not_read_as_dblp_having_nothing(ladder):
    """The distinction has to survive all the way up: `not found` here would be
    recorded as a failed attempt and reported as needing a hand-pasted entry."""
    ladder(search_dblp=_silent_dblp)
    bib, source = resolve(_TITLE, None, "k1", "")
    assert (bib, source) == ("", resolve_arxiv.UNANSWERED)


def test_a_silent_dblp_still_lets_the_rest_of_the_ladder_answer(ladder):
    """One source going quiet must not abandon the lookup -- a later source may
    well have the paper, and then there is nothing unknown about it."""
    ladder(search_dblp=_silent_dblp,
           query_s2_by_title=lambda t, y="": {"externalIds": {"ACL": "2024.acl-1.1"}},
           fetch_acl_bib=_acl_entry)
    bib, source = resolve(_TITLE, None, "k1", "")
    assert source == "ACL Anthology"


def test_a_silent_source_does_not_taint_the_next_paper(ladder):
    """resolve() compares against the count at its own entry, not against zero,
    so one quiet lookup does not mark every later paper in the run unknown."""
    ladder(search_dblp=_silent_dblp)
    resolve(_TITLE, None, "k1", "")
    ladder(search_dblp=lambda t: [])
    bib, source = resolve(_TITLE, None, "k2", "")
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

