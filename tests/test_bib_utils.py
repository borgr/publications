"""BibTeX parsing: the shapes that used to silently fail.

Every case here is taken from the real orig.bib, where 10 of 169 entries parsed
with the sentinel title "Title not found" because the old regex only accepted
`title = {...},`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bib_utils import (extract_field, find_duplicate_keys, normalize_text,
                       parse_bibtex)

# ACL Anthology exports quote their fields. This shape produced no title at all.
ACL_STYLE = '''@inproceedings{charpentier-etal-2025-findings,
    title = "Findings of the Third {B}aby{LM} Challenge: Accelerating Language Modeling Research",
    author = "Charpentier, Lucas  and
      Choshen, Leshem",
    year = "2025",
    url = "https://aclanthology.org/2025.babylm-main.1/"
}'''

BRACE_STYLE = '''@article{example2024key,
  title = {A Title With {Nested} Braces},
  author = {Doe, Jane},
  year = {2024},
}'''

# Title as the final field, with no trailing comma.
NO_TRAILING_COMMA = '''@misc{last2024field,
  author = {Doe, Jane},
  title = {The Last Field Has No Comma}
}'''


def test_quoted_title_is_parsed():
    (entry,) = parse_bibtex(ACL_STYLE)
    assert entry["item_name"] == "charpentier-etal-2025-findings"
    assert entry["title"].startswith("Findings of the Third {B}aby{LM} Challenge")
    assert entry["type"] == "inproceedings"


def test_braced_title_with_nested_braces_is_parsed_whole():
    (entry,) = parse_bibtex(BRACE_STYLE)
    assert entry["title"] == "A Title With {Nested} Braces"


def test_title_without_trailing_comma_is_parsed():
    (entry,) = parse_bibtex(NO_TRAILING_COMMA)
    assert entry["title"] == "The Last Field Has No Comma"


def test_no_sentinel_title_ever_leaks():
    """A missing title must be empty, not a string that looks like a title.

    "Title not found" was searchable: resolve() would query DBLP for it and a
    match could overwrite a good entry with an unrelated paper.
    """
    (entry,) = parse_bibtex("@misc{notitle2024, author = {Doe, Jane}, year = {2024}}")
    assert entry["title"] == ""


@pytest.mark.parametrize("source", [ACL_STYLE, BRACE_STYLE, NO_TRAILING_COMMA])
def test_beg_plus_rest_reconstructs_the_entry(source):
    """build_bib rewrites entries as beg + modified rest; the split must be lossless."""
    (entry,) = parse_bibtex(source)
    assert entry["beg"] + entry["rest"] == source


def test_multiple_entries_keep_source_order():
    entries = parse_bibtex(ACL_STYLE + "\n\n" + BRACE_STYLE)
    assert [e["item_name"] for e in entries] == ["charpentier-etal-2025-findings",
                                                 "example2024key"]


def test_non_entry_blocks_are_skipped():
    text = '@string{acl = "ACL"}\n@comment{ignored}\n' + BRACE_STYLE
    assert [e["item_name"] for e in parse_bibtex(text)] == ["example2024key"]


def test_unterminated_entry_is_skipped_not_guessed():
    assert parse_bibtex("@misc{broken2024, title = {No closing brace}") == []


def test_quoted_value_containing_braces_does_not_unbalance_the_scan():
    """A quoted field with unbalanced braces must not swallow the next entry."""
    text = '@misc{a2024, title = "Weird { brace"}\n\n' + BRACE_STYLE
    keys = [e["item_name"] for e in parse_bibtex(text)]
    assert "example2024key" in keys


def test_extract_field_handles_both_delimiters():
    assert extract_field('title = {Braced}', "title") == "Braced"
    assert extract_field('title = "Quoted"', "title") == "Quoted"
    assert extract_field('year = 2024,', "year") == "2024"
    assert extract_field('author = {Doe}', "title") == ""


def test_extract_field_collapses_wrapped_whitespace():
    assert extract_field('title = {A\n    wrapped   title}', "title") == "A wrapped title"


def test_normalize_text_strips_bibtex_capitalization_braces():
    """This is the equality that the citation-count bug turned on."""
    assert (normalize_text("Findings of the {B}aby{LM} Challenge")
            == normalize_text("Findings of the BabyLM challenge"))


def test_find_duplicate_keys():
    text = BRACE_STYLE + "\n\n" + BRACE_STYLE
    assert find_duplicate_keys(parse_bibtex(text)) == {"example2024key": 2}
    assert find_duplicate_keys(parse_bibtex(BRACE_STYLE)) == {}
