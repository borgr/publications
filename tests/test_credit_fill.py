"""Unit tests for credit_fill: restoring the author list of an arXiv stub."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import credit_fill
from bib_utils import extract_field, parse_bibtex
from credit_fill import ATTEMPT_PREFIX, entries_without_credits, fill_credits, has_credits

STUB = """@misc{gaber2026why,
  title = {Why Pretraining Fails to Share Cross-Lingual Knowledge},
  year = {2026},
  eprint = {2609.19291},
  archivePrefix = {arXiv},
  url = {https://arxiv.org/abs/2609.19291}
}
"""

NO_ID = """@misc{nobody2026,
  title = {A Stub With No Identifier},
  year = {2026}
}
"""

EDITED = """@proceedings{ed2024,
  title = {Proceedings of Something},
  editor = {Choshen, Leshem},
  year = {2024}
}
"""


def test_author_or_editor_counts_as_credits():
    assert has_credits("  author = {A. Person},\n")
    assert has_credits("  editor = {A. Person},\n")
    assert not has_credits("  title = {T},\n  year = {2026},\n")


def test_an_edited_volume_is_not_a_stub():
    assert entries_without_credits(EDITED) == []


def test_fill_credits_restores_the_authors(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "unanswered_lookups", lambda: 0)
    attempts = {}
    text, filled, missing = fill_credits(
        STUB, attempts, lookup=lambda arxiv_id, key, title: "Adam Gaber and Leshem Choshen")
    entry = parse_bibtex(text)[0]
    assert filled == 1 and missing == []
    assert extract_field(entry["content"], "author") == "Adam Gaber and Leshem Choshen"
    assert extract_field(entry["content"], "eprint") == "2609.19291"
    assert attempts == {ATTEMPT_PREFIX + "gaber2026why": 1}


def test_an_unread_page_leaves_the_stub_for_next_run(monkeypatch):
    calls = iter([0, 1])  # the arXiv page did not answer
    monkeypatch.setattr(credit_fill.resolve_arxiv, "unanswered_lookups", lambda: next(calls))
    attempts = {}
    text, filled, missing = fill_credits(STUB, attempts, lookup=lambda *a: "")
    assert (text, filled) == (STUB, 0)
    assert missing == [("Why Pretraining Fails to Share Cross-Lingual Knowledge", "gaber2026why")]
    assert attempts == {}, "a lookup the source never answered is not an attempt"


def test_a_stub_with_no_arxiv_id_is_left_for_the_worklist():
    def _no_lookup(*a):
        raise AssertionError("looked up an entry with no arXiv ID")

    text, filled, missing = fill_credits(NO_ID, {}, lookup=_no_lookup)
    assert (text, filled) == (NO_ID, 0)
    assert missing == [("A Stub With No Identifier", "nobody2026")]


def test_authors_from_arxiv_reads_the_fetched_entry(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_arxiv_bib",
                        lambda arxiv_id, key, title="":
                        "@misc{k,\n  title = {T},\n  author = {A and B},\n}")
    assert credit_fill.authors_from_arxiv("2609.19291", "k", "T") == "A and B"


VOLUME = """@misc{choshen2026proceedings,
  title = {Proceedings of the Second BabyLM Workshop},
  year = {2026},
  doi = {10.18653/2026.babylm-main},
}
"""

ANTHOLOGY_VOLUME = """@proceedings{babylm-2026-main,
    title = "Proceedings of the Second {B}aby{LM} Workshop",
    editor = "Choshen, Leshem  and
      Cotterell, Ryan",
    year = "2026",
    doi = "10.18653/2026.babylm-main.0",
}"""


def test_a_doi_only_volume_gains_its_editors(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "unanswered_lookups", lambda: 0)
    seen = []

    def _doi_lookup(doi, key, title, year):
        seen.append((doi, year))
        return {"editor": "Choshen, Leshem and Cotterell, Ryan"}

    attempts = {}
    text, filled, missing = fill_credits(
        VOLUME, attempts, lookup=lambda *a: pytest.fail("no arXiv ID to look up"),
        doi_lookup=_doi_lookup)
    assert (filled, missing) == (1, [])
    assert seen == [("10.18653/2026.babylm-main", 2026)]
    entry = parse_bibtex(text)[0]
    assert extract_field(entry["content"], "editor") == "Choshen, Leshem and Cotterell, Ryan"
    assert not extract_field(entry["content"], "author")
    assert attempts == {ATTEMPT_PREFIX + "choshen2026proceedings": 1}


def test_an_arxiv_id_is_preferred_to_the_doi(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "unanswered_lookups", lambda: 0)
    both = STUB.replace("year = {2026},", "year = {2026},\n  doi = {10.1/x},")
    text, filled, _ = fill_credits(
        both, {}, lookup=lambda *a: "A B",
        doi_lookup=lambda *a: pytest.fail("asked the DOI despite an arXiv ID"))
    assert filled == 1


def test_an_acl_volume_doi_is_read_from_the_anthology_volume_entry(monkeypatch):
    asked = []
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_acl_bib",
                        lambda acl_id, key: asked.append(acl_id) or ANTHOLOGY_VOLUME)
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_by_doi_crossref",
                        lambda *a: pytest.fail("Crossref names nobody for an ACL volume"))
    got = credit_fill.credits_from_doi(
        "10.18653/2026.babylm-main", "k", "Proceedings of the Second BabyLM Workshop", 2026)
    assert asked == ["2026.babylm-main.0"]
    assert set(got) == {"editor"} and got["editor"].startswith("Choshen, Leshem")


def test_an_acl_paper_doi_keeps_its_own_id(monkeypatch):
    asked = []
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_acl_bib",
                        lambda acl_id, key: asked.append(acl_id) or None)
    assert credit_fill.credits_from_doi("10.18653/v1/2023.acl-long.5", "k", "T") == {}
    assert asked == ["v1/2023.acl-long.5"]


def test_a_doi_answering_for_another_work_credits_nobody(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_acl_bib",
                        lambda acl_id, key: ANTHOLOGY_VOLUME)
    assert credit_fill.credits_from_doi(
        "10.18653/2026.babylm-main", "k", "Something Else Entirely", 2026) == {}


def test_a_non_acl_doi_goes_through_crossref(monkeypatch):
    monkeypatch.setattr(credit_fill.resolve_arxiv, "fetch_by_doi_crossref",
                        lambda doi, key, title, year: "@article{k,\n  title = {T},\n  author = {X Y},\n}")
    assert credit_fill.credits_from_doi("10.1145/1", "k", "T") == {"author": "X Y"}
