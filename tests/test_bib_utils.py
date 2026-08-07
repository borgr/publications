"""BibTeX parsing: the shapes that used to silently fail.

Every case here is taken from the real orig.bib, where 10 of 169 entries parsed
with the sentinel title "Title not found" because the old regex only accepted
`title = {...},`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bib_utils import extract_field, find_duplicate_keys, normalize_text, parse_bibtex

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


# ── never write BibTeX we cannot read back ───────────────────────────────────
#
# A title containing an unbalanced brace produced an entry that did not parse,
# and because the brace scan then ran past the entry's end it took the rest of
# the file with it. One such title emptied an entire bibliography in testing.

from bib_utils import (
    choose_published,
    escape_field_value,
    is_preprint,
    is_wellformed_entry,
    publication_rank,
)


def test_escape_balances_a_stray_open_brace():
    value = escape_field_value("A Title With An Unbalanced { Brace")
    entry = f"@misc{{k,\n  title = {{{value}}},\n  year = {{2024}}\n}}"
    assert is_wellformed_entry(entry, "k")


def test_escape_balances_a_stray_close_brace():
    value = escape_field_value("A Title With A } Brace")
    entry = f"@misc{{k,\n  title = {{{value}}},\n  year = {{2024}}\n}}"
    assert is_wellformed_entry(entry, "k")


def test_escape_does_not_mangle_its_own_backslash_replacement():
    assert escape_field_value(r"a \command") == r"a \textbackslash{}command"


def test_escape_collapses_newlines_and_runs_of_space():
    assert escape_field_value("a\n   b\tc") == "a b c"


def test_escape_of_none_is_empty():
    assert escape_field_value(None) == ""


def test_wellformed_rejects_an_unparseable_entry():
    assert not is_wellformed_entry("@article{k, title = {Broken {")


def test_wellformed_rejects_two_entries():
    assert not is_wellformed_entry(BRACE_STYLE + "\n\n" + NO_TRAILING_COMMA)


def test_wellformed_rejects_a_titleless_entry():
    assert not is_wellformed_entry("@misc{k, year = {2024}}")


def test_wellformed_rejects_the_wrong_key():
    assert not is_wellformed_entry(BRACE_STYLE, expected_key="someothername")
    assert is_wellformed_entry(BRACE_STYLE, expected_key="example2024key")


def test_wellformed_rejects_trailing_garbage():
    """Content after the entry means the parser skipped something."""
    assert not is_wellformed_entry(BRACE_STYLE + "\ntrailing junk here")


def test_wellformed_accepts_a_quoted_acl_style_entry():
    assert is_wellformed_entry(ACL_STYLE, "charpentier-etal-2025-findings")


# ── prefer the published version ─────────────────────────────────────────────

def test_published_outranks_preprint():
    published = parse_bibtex(
        '@inproceedings{a, title={T}, booktitle={ACL}, pages={1--9}}')[0]
    preprint = parse_bibtex(
        '@misc{b, title={T}, eprint={2401.1}, archivePrefix={arXiv}}')[0]
    assert publication_rank(published) > publication_rank(preprint)


def test_corr_article_outranked_by_real_journal():
    corr = parse_bibtex('@article{a, title={T}, journal={CoRR}}')[0]
    real = parse_bibtex('@article{b, title={T}, journal={Nature}, doi={10.1/x}}')[0]
    assert publication_rank(real) > publication_rank(corr)


def test_choose_published_is_order_independent():
    entries = parse_bibtex(
        '@misc{b, title={T}, archivePrefix={arXiv}}\n\n'
        '@inproceedings{a, title={T}, booktitle={ACL}}')
    forward, _ = choose_published(entries)
    backward, _ = choose_published(list(reversed(entries)))
    assert forward["item_name"] == backward["item_name"] == "a"


def test_choose_published_on_empty_input():
    assert choose_published([]) == (None, [])


# ── which of several entries for one paper to keep ───────────────────────────

def test_published_beats_a_newer_preprint():
    """The version of record wins even when a preprint is more recent."""
    pub = parse_bibtex('@inproceedings{a, title={T}, booktitle={ACL}, year={2024}}')[0]
    pre = parse_bibtex('@misc{b, title={T}, archivePrefix={arXiv}, year={2025}}')[0]
    winner, _ = choose_published([pre, pub])
    assert winner["item_name"] == "a"


def test_newer_preprint_wins_between_two_preprints():
    """Two preprints of one paper are its v1 and v2; v2 has the current title.

    Real case: "Can You Trust Your Metric?" (2024) and "How Safe is Your Safety
    Metric?" (2025) share one arXiv ID. Keeping the 2024 row printed a title the
    authors had replaced.
    """
    old = parse_bibtex(
        '@article{a, title={Old Title}, journal={arXiv preprint}, year={2024}}')[0]
    new = parse_bibtex('@misc{b, title={New Title}, year={2025}}')[0]
    winner, _ = choose_published([old, new])
    assert winner["item_name"] == "b"


def test_is_preprint_classification():
    def one(src):
        return is_preprint(parse_bibtex(src)[0])
    assert not one('@inproceedings{a, title={T}, booktitle={ACL}}')
    assert not one('@article{a, title={T}, journal={Nature}}')
    assert one('@article{a, title={T}, journal={CoRR}}')
    assert one('@article{a, title={T}, journal={arXiv preprint arXiv:2401.1}}')
    assert one('@misc{a, title={T}, archivePrefix={arXiv}}')
    assert one('@misc{a, title={T}}')


def test_choose_published_is_stable_when_everything_ties():
    entries = parse_bibtex('@misc{bbb, title={T}, year={2024}}\n\n'
                           '@misc{aaa, title={T}, year={2024}}')
    first, _ = choose_published(entries)
    second, _ = choose_published(list(reversed(entries)))
    assert first["item_name"] == second["item_name"]


# ── editing a field without assuming its delimiter ───────────────────────────

from bib_utils import find_field_span


def test_find_field_span_reports_the_delimiter():
    assert find_field_span('title = {Braced}', "title") == (9, 15, '{')
    assert find_field_span('title = "Quoted"', "title") == (9, 15, '"')
    assert find_field_span('year = 2024,', "year")[2] == ''
    assert find_field_span('author = {Doe}', "title") is None


def test_find_field_span_balances_nested_braces():
    content = 'title = {A {Nested} Title}, year = {2024}'
    start, end, delim = find_field_span(content, "title")
    assert content[start:end] == "A {Nested} Title"
    assert delim == '{'


# ── truncated and malformed field values ─────────────────────────────────────
#
# Both readers are given whatever a remote source returned. A truncated response
# is the ordinary failure mode of an HTTP fetch, and the only safe answer to one
# is "no value" -- a partial title would be written to the CV as if it were the
# paper's name.

@pytest.mark.parametrize("content", [
    'title = ',                 # the response ended mid-field
    'title = {unbalanced',       # a braced value with no closing brace
    'title = "no closing quote',  # a quoted value with no closing quote
])
def test_a_truncated_field_reads_as_absent(content):
    assert extract_field(content, "title") == ""
    assert find_field_span(content, "title") is None


@pytest.mark.parametrize("reader, expected", [
    (extract_field, r'A \" quote'),
    (lambda c, f: c[slice(*find_field_span(c, f)[:2])], r'A \" quote'),
])
def test_an_escaped_quote_does_not_end_a_quoted_value(reader, expected):
    """Otherwise the value stops at the escape and the rest of the entry is read
    as if it were fields -- and for find_field_span, an edit lands mid-title."""
    assert reader(r'title = "A \" quote", year = 2024', "title") == expected


def test_a_commented_out_entry_is_not_a_publication():
    """@comment is how an entry gets shelved without deleting it, so its contents
    parse perfectly well as a record. Emitting it would put a paper the author
    removed back on the CV."""
    text = ('@comment{shelved2024, title = {A Draft, Not Submitted}}\n\n'
            + BRACE_STYLE)
    assert [e["item_name"] for e in parse_bibtex(text)] == ["example2024key"]


# ── entry types the ranking rules do not name ────────────────────────────────

def test_an_unnamed_entry_type_ranks_between_published_and_preprint():
    """@phdthesis, @mastersthesis, @online: real documents, but not the venue a
    published paper has. Ranking them as preprints would let an arXiv copy of a
    thesis win; ranking them as published would beat the paper it became."""
    thesis = parse_bibtex('@phdthesis{a, title={T}}')[0]
    preprint = parse_bibtex('@misc{b, title={T}}')[0]
    published = parse_bibtex('@inproceedings{c, title={T}, booktitle={ACL}}')[0]
    assert (publication_rank(preprint) < publication_rank(thesis)
            < publication_rank(published))


def test_an_unnamed_type_is_a_preprint_only_if_it_says_so():
    assert is_preprint(parse_bibtex('@phdthesis{a, title={T}, eprint={2401.1}}')[0])
    assert not is_preprint(parse_bibtex('@phdthesis{a, title={T}}')[0])


# ── read_df ──────────────────────────────────────────────────────────────────

import bib_utils


@pytest.fixture
def table_problems(monkeypatch):
    """Drive read_df with a stubbed table, and clear the once-per-run memory.

    The set is module state that outlives a single call, which is the whole point
    of it, so a test that did not reset it would depend on test order.
    """
    import table_io

    monkeypatch.setattr(bib_utils, "_reported_table_problems", set())

    def _run(problems, df="the frame"):
        monkeypatch.setattr(table_io, "read_table", lambda: df)
        monkeypatch.setattr(table_io, "validate", lambda _df: problems)
        return bib_utils.read_df()
    return _run


def test_read_df_returns_the_table_it_read(table_problems):
    assert table_problems([]) == "the frame"


def test_a_table_problem_is_reported_not_raised(table_problems, capsys):
    """An untidy table still has to build a CV; the problems go to WORKLIST.md."""
    df = table_problems(["two rows share a bib key"])
    assert df == "the frame"
    assert "two rows share a bib key" in capsys.readouterr().out


def test_the_same_problem_is_reported_once_per_run(table_problems, capsys):
    """Several modules call read_df in one run, and a warning printed three times
    reads as three problems."""
    table_problems(["two rows share a bib key"])
    table_problems(["two rows share a bib key", "row 12 has no year"])
    out = capsys.readouterr().out
    assert out.count("two rows share a bib key") == 1
    assert "row 12 has no year" in out
