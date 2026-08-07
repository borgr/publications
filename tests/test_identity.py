"""The citation join, pinned against the real mismatches it was built to fix.

The titles in these fixtures are the actual strings from this repo's
publications table and Google Scholar profile. They are the regression suite for
a bug that silently reported 490 citations as zero, and for the three cases
where tightening the matching first went too far.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from identity import (
    MATCH_EXACT_ID,
    MATCH_FUZZY,
    MATCH_NORMALIZED,
    IdentityStore,
    classify_title,
    duplicate_groups_by_bib_identifier,
    find_duplicate_titles,
    harvest_ids_from_bibtex,
    harvest_ids_from_s2,
    join_citations,
    merge_overlapping_groups,
    normalize_identifier,
    normalize_title,
    title_stem,
    titles_match,
)

BABYLM_1_TABLE = "Findings of the {B}aby{LM} Challenge: Sample-Efficient Pretraining on Developmentally Plausible Corpora"
BABYLM_1_SCHOLAR = "Findings of the BabyLM challenge: Sample-efficient pretraining on developmentally plausible corpora"
BABYLM_2_TABLE = "Findings of the Second BabyLM Challenge: Sample-Efficient Pretraining on Developmentally Plausible Corpora"
BABYLM_2_SCHOLAR = "Findings of the second BabyLM challenge: Sample-efficient pretraining on developmentally plausible corpora"
BABYLM_3_TABLE = "Findings of the Third BabyLM Challenge: Accelerating Language Modeling Research with Cognitively Plausible Data"


def rows(*pairs):
    """Build citation rows from (title, count) or (title, count, scholar_id)."""
    out = []
    for pair in pairs:
        title, count = pair[0], pair[1]
        out.append({"title": title, "citations": count,
                    "scholar_id": pair[2] if len(pair) > 2 else ""})
    return out


# ── the original bug ─────────────────────────────────────────────────────────

def test_bibtex_braces_do_not_defeat_the_match():
    """490 citations were reported as 0 because `{B}aby{LM}` never matched."""
    result = join_citations(rows((BABYLM_1_SCHOLAR, 379)), [BABYLM_1_TABLE])
    assert result.matched == {BABYLM_1_TABLE: 379}
    assert result.method[BABYLM_1_TABLE] == MATCH_NORMALIZED


def test_same_series_papers_do_not_steal_each_others_counts():
    """The regression that made both BabyLM papers report 0.

    Their titles differ by one word, so a loose fuzzy match assigned one
    paper's count to the other and then overwrote it.
    """
    result = join_citations(
        rows((BABYLM_1_SCHOLAR, 379), (BABYLM_2_SCHOLAR, 111)),
        [BABYLM_1_TABLE, BABYLM_2_TABLE, BABYLM_3_TABLE],
    )
    assert result.matched == {BABYLM_1_TABLE: 379, BABYLM_2_TABLE: 111}
    assert BABYLM_3_TABLE not in result.matched


def test_result_is_independent_of_input_order():
    """The old join let the last writer win, so output depended on row order."""
    forward = join_citations(rows((BABYLM_1_SCHOLAR, 379), (BABYLM_2_SCHOLAR, 111)),
                            [BABYLM_1_TABLE, BABYLM_2_TABLE])
    backward = join_citations(rows((BABYLM_2_SCHOLAR, 111), (BABYLM_1_SCHOLAR, 379)),
                             [BABYLM_1_TABLE, BABYLM_2_TABLE])
    assert forward.matched == backward.matched


def test_row_order_of_the_table_does_not_change_the_winner():
    """A duplicate table row must pick the same winner every run.

    Passing a set here previously made the outcome depend on PYTHONHASHSEED.
    """
    first = join_citations(rows((BABYLM_2_SCHOLAR, 111)), [BABYLM_2_TABLE, BABYLM_2_SCHOLAR])
    second = join_citations(rows((BABYLM_2_SCHOLAR, 111)), [BABYLM_2_SCHOLAR, BABYLM_2_TABLE])
    assert first.matched == second.matched


# ── the over-tightening regressions ──────────────────────────────────────────

def test_short_title_still_matches_exactly():
    """"TextArena" normalizes to 9 chars; a length floor lost 51 citations."""
    result = join_citations(rows(("Textarena", 51)), ["TextArena"])
    assert result.matched == {"TextArena": 51}
    assert result.method["TextArena"] == MATCH_NORMALIZED


def test_renamed_paper_matches_but_is_flagged_for_review():
    """Same paper, retitled between preprint and publication: 97 citations."""
    table = "Will it Blend? Weak and Manual Labeled Data in a Neural Network for Argumentation Mining"
    scholar = "Will it blend? blending weak and strong labeled data in a neural network for argumentation mining"
    result = join_citations(rows((scholar, 97)), [table])
    assert result.matched == {table: 97}
    assert result.method[table] == MATCH_FUZZY
    assert [n for n, *_ in result.needs_review] == [table]


def test_colon_prefix_variation_matches():
    """Scholar drops project prefixes that the table keeps, and vice versa."""
    result = join_citations(rows(("An autonomous debating system", 12)),
                           ["Project Debater: An Autonomous Debating System"])
    assert result.matched == {"Project Debater: An Autonomous Debating System": 12}


def test_unrelated_title_does_not_match():
    result = join_citations(rows(("A Completely Different Paper About Bananas", 5)),
                           [BABYLM_1_TABLE])
    assert result.matched == {}
    assert result.unmatched == [("A Completely Different Paper About Bananas", 5)]


def test_indistinguishable_candidates_are_reported_not_guessed():
    """Two table rows equidistant from one incoming title: report, do not pick."""
    result = join_citations(rows(("Findings of the BabyLM Challenge", 10)),
                            ["Findings of the AbyLM Challenge",
                             "Findings of the CbyLM Challenge"])
    assert result.matched == {}
    assert result.too_close
    assert result.too_close[0][0] == "Findings of the BabyLM Challenge"


# ── stable identifiers ───────────────────────────────────────────────────────

def test_scholar_id_beats_a_title_that_no_longer_matches():
    """Once bound, a renamed paper resolves exactly and needs no review."""
    store = IdentityStore()
    store.record("babylm1", title=BABYLM_1_TABLE, scholar_id="USER:AAAA")
    result = join_citations(rows(("A Totally Rewritten Title", 379, "USER:AAAA")),
                            [BABYLM_1_TABLE], store=store)
    assert result.matched == {BABYLM_1_TABLE: 379}
    assert result.method[BABYLM_1_TABLE] == MATCH_EXACT_ID
    assert result.needs_review == []


def test_two_records_for_one_paper_are_summed():
    """Scholar keeps the preprint and published version as separate records.

    The real case: 14 citations on one, none on the other. Nothing to adjudicate.
    """
    table = "The Mighty ToRR: A Benchmark for Table Reasoning and Robustness"
    result = join_citations(
        rows(("The mighty torr: A benchmark for table reasoning and robustness", 14),
             ("The Mighty ToRR: A Benchmark for Table Reasoning and Robustness in LLMs",
              None)),
        [table])
    assert result.matched[table] == 14
    assert result.aggregated and not result.ambiguous


def test_two_counted_records_for_one_paper_are_added():
    table = "A Paper With Two Scholar Records And A Long Title"
    result = join_citations(
        rows(("A Paper With Two Scholar Records And A Long Title", 10),
             ("A paper with two scholar records and a long title!", 5)),
        [table])
    assert result.matched[table] == 15


def test_all_records_uncounted_stays_none_not_zero():
    table = "A Brand New Paper Nobody Has Cited Yet At All"
    result = join_citations(
        rows((table, None), (table + " v2", None)), [table])
    assert result.matched[table] is None


def test_different_papers_on_one_row_are_not_summed():
    """Summing here would invent a citation count that no source reported."""
    table = "Findings of the BabyLM Challenge: Sample-Efficient Pretraining"
    store = IdentityStore()
    store.record("babylm1", title=table, scholar_id="USER:AAAA")
    result = join_citations(
        rows((table, 1), ("Something Else Entirely Here And Unrelated", 379, "USER:AAAA")),
        [table], store=store)
    assert result.matched[table] == 379, "the exact-ID record wins outright"
    assert result.ambiguous and not result.aggregated


def test_exact_id_wins_over_a_competing_title_match():
    store = IdentityStore()
    store.record("babylm1", title=BABYLM_1_TABLE, scholar_id="USER:AAAA")
    result = join_citations(
        rows((BABYLM_1_TABLE, 1),
             ("Something Else Entirely Here And Unrelated", 379, "USER:AAAA")),
        [BABYLM_1_TABLE], store=store,
    )
    assert result.matched[BABYLM_1_TABLE] == 379
    assert result.method[BABYLM_1_TABLE] == MATCH_EXACT_ID


def test_missing_count_stays_none_and_is_not_confused_with_zero():
    result = join_citations(rows((BABYLM_1_SCHOLAR, None)), [BABYLM_1_TABLE])
    assert result.matched == {BABYLM_1_TABLE: None}


# ── the identity store ───────────────────────────────────────────────────────

def test_store_round_trips(tmp_path):
    path = str(tmp_path / "identity.json")
    store = IdentityStore()
    store.record("k1", title="A Title", scholar_id="U:1", arxiv="2401.00001")
    store.save(path)
    assert IdentityStore.load(path).records["k1"]["arxiv"] == "2401.00001"


def test_store_load_of_missing_file_is_empty_not_an_error(tmp_path):
    assert IdentityStore.load(str(tmp_path / "nope.json")).records == {}


def test_store_records_conflicts_instead_of_overwriting():
    """Two sources disagreeing about an ID means papers were conflated."""
    store = IdentityStore()
    store.record("k1", doi="10.1/aaa")
    store.record("k1", doi="10.1/bbb")
    assert store.records["k1"]["doi"] == "10.1/aaa"
    assert store.conflicts() == [("k1", "doi", ["10.1/aaa", "10.1/bbb"])]


def test_store_ignores_blank_and_unknown_fields():
    store = IdentityStore()
    store.record("k1", doi="", nonsense="x")
    assert "doi" not in store.records["k1"]
    assert "nonsense" not in store.records["k1"]


def test_rekey_moves_a_synthetic_record_onto_a_real_key():
    store = IdentityStore()
    store.record("~title:atitle", title="A Title", arxiv="2401.00001")
    store.rekey("~title:atitle", "doe2024title")
    assert "~title:atitle" not in store.records
    assert store.records["doe2024title"]["arxiv"] == "2401.00001"


# ── identifier harvesting ────────────────────────────────────────────────────

def test_harvest_from_s2_is_the_acl_arxiv_crosswalk():
    """One S2 response binds ACL and arXiv together even though neither
    source knows the other's identifier."""
    ids = harvest_ids_from_s2({"externalIds": {
        "ArXiv": "2404.06214", "ACL": "2024.acl-long.1",
        "DOI": "10.18653/v1/2024.acl-long.1", "CorpusId": 12345}})
    assert ids == {"arxiv": "2404.06214", "acl": "2024.acl-long.1",
                   "doi": "10.18653/v1/2024.acl-long.1", "s2": "12345"}


def test_harvest_from_s2_tolerates_empty_payloads():
    assert harvest_ids_from_s2(None) == {}
    assert harvest_ids_from_s2({}) == {}


def test_harvest_from_bibtex():
    ids = harvest_ids_from_bibtex(
        '@misc{k, eprint = {2404.06214}, archivePrefix = {arXiv}, '
        'url = {https://arxiv.org/abs/2404.06214}, doi = {10.18653/v1/x.1}}')
    assert ids["arxiv"] == "2404.06214"
    assert ids["doi"] == "10.18653/v1/x.1"


@pytest.mark.parametrize("url, expected", [
    # The only form that matters, and the only one that used to fail: inside a
    # field, where the URL is followed by a brace rather than by a slash or the
    # end of the string. `harvest_ids_from_bibtex` has no other caller, so no ACL
    # id was ever harvested from a bibliography.
    ('url = {https://aclanthology.org/2022.coling-1.401},', "2022.coling-1.401"),
    ('url = "https://aclanthology.org/2022.coling-1.401",', "2022.coling-1.401"),
    ('url = {https://aclanthology.org/2022.coling-1.401}', "2022.coling-1.401"),
    ('url = {https://aclanthology.org/2022.coling-1.401.pdf},', "2022.coling-1.401"),
    ('url = {https://aclanthology.org/2022.coling-1.401/},', "2022.coling-1.401"),
    ('see https://aclanthology.org/2022.coling-1.401.', "2022.coling-1.401"),
])
def test_an_acl_url_is_harvested_from_a_bibtex_field(url, expected):
    assert harvest_ids_from_bibtex("@inproceedings{k, " + url + "}")["acl"] == expected


def test_a_doi_is_compared_case_insensitively():
    """DBLP writes the DOI upper-case where the ACL Anthology writes it lower.

    Compared literally they are two identifiers, so the duplicate they prove goes
    unnoticed -- which is how three papers reached the CV twice.
    """
    assert (normalize_identifier("doi", "10.18653/V1/2023.CONLL-1.29")
            == normalize_identifier("doi", "10.18653/v1/2023.conll-1.29"))
    # A DBLP key is case-sensitive, and Scholar's ids are opaque, so they are not
    # folded: doing so would merge two papers on a coincidence of spelling.
    assert normalize_identifier("dblp", "journals/corr/abs-1") != \
        normalize_identifier("dblp", "JOURNALS/CORR/ABS-1")


# ── duplicate detection from the bibliography ────────────────────────────────

_PAIR_BIB = (
    "@inproceedings{karidi2023muler,\n"
    "  title = {MuLER: Detailed and Scalable Reference-based Evaluation},\n"
    "  doi = {10.18653/v1/2023.conll-1.29},\n}\n\n"
    "@inproceedings{DBLP:conf/conll/KaridiCPA23,\n"
    "  title = {MuLER: Multi-Level Evaluation with Reference},\n"
    "  doi = {10.18653/V1/2023.CONLL-1.29},\n}\n\n"
    "@inproceedings{unrelated2020other,\n"
    "  title = {Something Else Entirely},\n"
    "  doi = {10.1000/other},\n}\n"
)


def test_a_shared_doi_in_the_bibliography_is_a_duplicate():
    """The store-based detector cannot see this pair: the DBLP-keyed half has no
    record at all, because it never went through resolution."""
    groups = duplicate_groups_by_bib_identifier(_PAIR_BIB)
    assert groups == [["DBLP:conf/conll/KaridiCPA23", "karidi2023muler"]]


def test_an_entry_alone_with_its_identifier_is_not_a_duplicate():
    assert duplicate_groups_by_bib_identifier(
        "@misc{only, doi = {10.1000/x}}") == []


def test_keys_in_use_limits_the_search_to_the_table():
    """An entry left in orig.bib but no longer on the table is not a duplicate of
    anything -- build_bib never emits it."""
    assert duplicate_groups_by_bib_identifier(
        _PAIR_BIB, keys_in_use={"karidi2023muler", "unrelated2020other"}) == []


def test_the_same_paper_found_twice_over_becomes_one_group():
    """Two entries can share a DOI and an arXiv id at once. Reported separately,
    each overlap is its own decision about one paper, and the two can disagree
    about which entry survives."""
    bib = ("@misc{a, doi = {10.1000/x}, eprint = {2401.00001}}\n\n"
           "@misc{b, doi = {10.1000/x}, eprint = {2401.00001}}\n")
    assert duplicate_groups_by_bib_identifier(bib) == [["a", "b"]]


@pytest.mark.parametrize("groups, expected", [
    ([["a", "b"], ["b", "c"]], [["a", "b", "c"]]),          # chained
    ([["a", "b"], ["c", "d"]], [["a", "b"], ["c", "d"]]),   # disjoint
    ([["a", "b"], ["a", "b"]], [["a", "b"]]),               # the same pair twice
    ([["a"], ["b", "c"]], [["b", "c"]]),                    # a lone member is not a group
    ([["a", "b"], ["c", "d"], ["b", "d"]], [["a", "b", "c", "d"]]),  # joined late
])
def test_overlapping_groups_merge(groups, expected):
    assert merge_overlapping_groups(groups) == expected


# ── table hygiene ────────────────────────────────────────────────────────────

def test_find_duplicate_titles_catches_the_real_duplicate_row():
    """These two rows are in the live table and put one paper in the CV twice."""
    dups = find_duplicate_titles([BABYLM_2_TABLE, BABYLM_2_SCHOLAR, BABYLM_3_TABLE])
    assert len(dups) == 1
    assert sorted(next(iter(dups.values()))) == sorted([BABYLM_2_TABLE, BABYLM_2_SCHOLAR])


def test_find_duplicate_titles_is_quiet_when_clean():
    assert find_duplicate_titles([BABYLM_1_TABLE, BABYLM_2_TABLE]) == {}


# ── step 2's new-paper test ──────────────────────────────────────────────────

def test_titles_match_treats_series_papers_as_distinct():
    """Otherwise step 2 would never notice the next BabyLM paper."""
    assert not titles_match(BABYLM_3_TABLE, BABYLM_2_TABLE)


def test_titles_match_accepts_case_and_brace_variation():
    assert titles_match(BABYLM_1_SCHOLAR, BABYLM_1_TABLE)


def test_titles_match_accepts_an_abbreviated_title():
    assert titles_match("TextArena: A Framework for Text-Based Game Environments",
                        "TextArena: A Framework for Text-Based Game Environments and Agents")


def test_titles_match_rejects_blanks():
    assert not titles_match("", BABYLM_1_TABLE)
    assert not titles_match(BABYLM_1_TABLE, "")


@pytest.mark.parametrize("title,expected", [
    ("Project Debater: An Autonomous Debating System", "anautonomousdebatingsystem"),
    ("No colon here", "nocolonhere"),
])
def test_title_stem_keeps_the_longer_side_of_a_colon(title, expected):
    assert title_stem(title) == expected


def test_normalize_title_is_stable_under_punctuation_and_spacing():
    assert normalize_title("A  Title -- With: Punctuation!") == normalize_title(
        "a title with punctuation")


# ── the invariant that step 2 and the join must never disagree ────────────────

REAL_FUZZY_PAIRS = [
    # (Scholar title, table title) -- every non-exact match in the live data.
    ("Will it blend? blending weak and strong labeled data in a neural network for argumentation mining",
     "Will it Blend? Weak and Manual Labeled Data in a Neural Network for Argumentation Mining"),
    ("Cluster & tune: Boost cold start performance in text classification",
     "Cluster & Tune: Boost BERT Cold Start Performance in Topical Text Classification"),
    ("Dora the explorer: Directed outreaching reinforcement action-selection",
     "DORA The Explorer: Directed Outreaching Reinforcement Action"),
    ("Holmes⌕ A Benchmark to Assess the Linguistic Competence of Language Models",
     "Holmes: Benchmark the Linguistic Competence of Language Models"),
    ("The Mighty ToRR: A Benchmark for Table Reasoning and Robustness in LLMs",
     "The mighty torr: A benchmark for table reasoning and robustness"),
    (BABYLM_1_SCHOLAR, BABYLM_1_TABLE),
    ("Textarena", "TextArena"),
]


@pytest.mark.parametrize("scholar,table", REAL_FUZZY_PAIRS)
def test_step2_and_the_join_agree(scholar, table):
    """If the join treats these as one paper, step 2 must not call it new.

    Disagreement is not a cosmetic inconsistency: step 2 appends a row for
    anything it thinks is new, so a stricter bar there duplicated every
    fuzzy-matched paper on every run -- and each duplicate then made the citation
    join ambiguous. This is the invariant, checked on real title pairs.
    """
    joined = join_citations(rows((scholar, 7)), [table])
    known, _tier, _score = classify_title(scholar, [table])

    if joined.matched:
        assert known is not None, (
            "the join matched this paper but step 2 would add a duplicate row")
        assert known == table
    else:
        assert known is None, "step 2 matched a paper the join did not"


def test_titles_match_is_consistent_with_classify_title():
    for scholar, table in REAL_FUZZY_PAIRS:
        assert titles_match(scholar, table) == (
            classify_title(scholar, [table])[0] is not None)


def test_a_genuinely_different_paper_is_still_new():
    """The permissive bar must not swallow an actually-new paper wholesale."""
    known, _tier, _score = classify_title(
        "A Completely Unrelated Paper About Marine Biology And Fish",
        [BABYLM_1_TABLE, "TextArena"])
    assert known is None
