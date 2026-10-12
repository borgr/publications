"""Unit tests for link_fill: adding a DOI or arXiv ID to entries that have none."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import link_fill
from bib_utils import extract_field, parse_bibtex
from link_fill import (
    ATTEMPT_PREFIX,
    _fields_for,
    add_fields,
    entries_without_link,
    fill_links,
    has_link,
    link_fields_from_openalex,
    link_fields_from_s2,
)

BARE = """@article{guertler2025textarena,
  title={TextArena},
  author={Guertler, Leon and Choshen, Leshem},
  journal={arXiv preprint},
  year={2025}
}
"""

LINKED = """@inproceedings{a2024linked,
  title={Linked},
  author={Choshen, Leshem},
  doi={10.18653/v1/2024.acl-long.1},
  year={2024}
}
"""


def test_has_link_sees_each_field():
    for field in ("doi", "eprint", "url"):
        assert has_link(f"  title={{T}},\n  {field}={{x}},\n")
    assert not has_link("  title={T},\n  year={2025},\n")


def test_an_empty_field_is_not_a_link():
    assert not has_link("  title={T},\n  url={},\n")


def test_entries_without_link_skips_linked_ones():
    keys = [e["item_name"] for e in entries_without_link(BARE + LINKED)]
    assert keys == ["guertler2025textarena"]


def test_published_doi_beats_arxiv():
    assert _fields_for("10.18653/v1/x", "2504.11442") == {"doi": "10.18653/v1/x"}


def test_arxiv_id_beats_the_arxiv_doi():
    assert _fields_for("10.48550/arXiv.2504.11442", "") == {
        "eprint": "2504.11442", "archiveprefix": "arXiv"}


def test_doi_url_prefix_is_stripped():
    assert _fields_for("https://doi.org/10.1016/j.jml.2025.104650") == {
        "doi": "10.1016/j.jml.2025.104650"}


def test_no_identifier_gives_no_fields():
    assert _fields_for("", "") == {}
    assert link_fields_from_s2(None) == {}
    assert link_fields_from_openalex(None) == {}


def test_s2_record_fields():
    rec = {"externalIds": {"ArXiv": "2504.11442", "DOI": "10.48550/arXiv.2504.11442"}}
    assert link_fields_from_s2(rec) == {"eprint": "2504.11442", "archiveprefix": "arXiv"}


def test_add_fields_appends_and_keeps_the_rest():
    out = add_fields(BARE + LINKED, "guertler2025textarena",
                     {"eprint": "2504.11442", "archiveprefix": "arXiv"})
    entries = {e["item_name"]: e for e in parse_bibtex(out)}
    assert extract_field(entries["guertler2025textarena"]["content"], "eprint") == "2504.11442"
    assert extract_field(entries["guertler2025textarena"]["content"], "title") == "TextArena"
    assert LINKED in out, "the other entry must be untouched"


def test_add_fields_never_overwrites():
    out = add_fields(LINKED, "a2024linked", {"doi": "10.9999/other"})
    assert out == LINKED


def test_fill_links_counts_only_completed_lookups(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "unanswered_lookups", lambda: 0)
    attempts = {}
    text, filled, missing = fill_links(BARE, attempts, lookup=lambda t, y: {})
    assert (filled, text) == (0, BARE)
    assert missing == [("TextArena", "guertler2025textarena")]
    assert attempts == {ATTEMPT_PREFIX + "guertler2025textarena": 1}


def test_fill_links_does_not_count_an_unanswered_lookup(monkeypatch):
    calls = iter([0, 1])  # one more unanswered lookup after the call than before
    monkeypatch.setattr(link_fill.resolve_arxiv, "unanswered_lookups", lambda: next(calls))
    attempts = {}
    fill_links(BARE, attempts, lookup=lambda t, y: {})
    assert attempts == {}


def test_fill_links_writes_what_it_finds(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "unanswered_lookups", lambda: 0)
    text, filled, missing = fill_links(
        BARE, {}, lookup=lambda t, y: {"eprint": "2504.11442", "archiveprefix": "arXiv"})
    assert filled == 1 and missing == []
    assert not entries_without_link(text)


ATOM = """<feed><title>arXiv Query</title>
<entry><id>http://arxiv.org/abs/2504.11442v2</id><published>2025-04-15T00:00:00Z</published>
<title>TextArena</title></entry>
<entry><id>http://arxiv.org/abs/2401.00001v1</id><published>2024-01-01T00:00:00Z</published>
<title>Something Else Entirely</title></entry>
</feed>"""


def test_arxiv_search_takes_the_entry_whose_title_agrees(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "_curl_get", lambda url, **kw: ATOM)
    assert link_fill.search_arxiv_by_title("TextArena", "2025") == "2504.11442"


def test_arxiv_search_rejects_a_different_paper(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "_curl_get", lambda url, **kw: ATOM)
    assert link_fill.search_arxiv_by_title("A Paper arXiv Never Saw", "2025") == ""


def test_arxiv_search_treats_no_reply_as_nothing(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "_curl_get", lambda url, **kw: None)
    assert link_fill.search_arxiv_by_title("TextArena", "2025") == ""


def test_find_link_falls_through_to_arxiv(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(link_fill.resolve_arxiv, "search_openalex", lambda t, y=None: None)
    monkeypatch.setattr(link_fill.resolve_arxiv, "_curl_get", lambda url, **kw: ATOM)
    assert link_fill.find_link("TextArena", "2025") == {
        "eprint": "2504.11442", "archiveprefix": "arXiv"}


def test_find_link_prefers_s2(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "query_s2_by_title",
                        lambda t, y="": {"externalIds": {"DOI": "10.18653/v1/x"}})
    assert link_fill.find_link("Anything", "2025") == {"doi": "10.18653/v1/x"}


def test_find_link_uses_openalex_when_s2_has_nothing(monkeypatch):
    monkeypatch.setattr(link_fill.resolve_arxiv, "query_s2_by_title", lambda t, y="": None)
    monkeypatch.setattr(link_fill.resolve_arxiv, "search_openalex",
                        lambda t, y=None: {"doi": "https://doi.org/10.1016/j.x"})
    assert link_fill.find_link("Anything", "2025") == {"doi": "10.1016/j.x"}


def test_an_id_already_in_the_journal_field_needs_no_lookup():
    entry = BARE.replace("journal={arXiv preprint}", "journal={arXiv preprint arXiv:2504.11442}")

    def _no_lookup(title, year):
        raise AssertionError("looked up a paper whose arXiv ID was already in the entry")

    text, filled, missing = fill_links(entry, {}, lookup=_no_lookup)
    assert filled == 1 and missing == []
    assert "eprint = {2504.11442}" in text
