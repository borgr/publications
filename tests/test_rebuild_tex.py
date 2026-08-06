"""Updating main.tex — the step that writes the actual CV document.

This had no tests, and every substitution in it silently no-opped on a missing
anchor. main.tex is meant to be edited in the Overleaf editor, so reflowing one
line was enough to freeze a CV section with a stale paper list permanently, while
the run reported success and pushed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rebuild_tex
from build_bib import BibCategories
from rebuild_tex import (TexUpdateError, _find_author_names_in_bst,
                         _nocite_str, update_tex)

# The shape of the real document, reduced to what the code anchors on. Tabs and
# newlines in the stats block are load-bearing: the regex matches them literally.
TEX = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\noindent\\today\n\n"
    "Leshem Choshen\n"
    "\\textbf{Citations\t100\nh-index\t9\n}\n\n"
    "\\chapter*{Refereed Articles}\n"
    "% Journals:\n"
    "\\nocite{old_journal}\n"
    "% Conferences:\n"
    "\\nocite{old_conference}\n"
    "\\putbib\n\n"
    "\\chapter*{Review Papers}\n\\nocite{old_review}\n\\putbib\n\n"
    "\\chapter*{Workshop Papers}\n\\nocite{old_workshop}\n\\putbib\n\n"
    "\\chapter*{Non-reviewed or under review papers}\n\\nocite{old_draft}\n\\putbib\n"
    "\\end{document}\n"
)

CATS = BibCategories(journals=["j1", "j2"], conferences=["c1"], reviews=["r1"],
                     workshops=["w1"], drafts=["d1"])


@pytest.fixture
def stats(tmp_path, monkeypatch):
    path = tmp_path / "profile_stats.json"
    path.write_text('{"citations": 6286, "h_index": 39}', encoding="utf-8")
    monkeypatch.setattr(rebuild_tex, "STATS_PATH", str(path))
    monkeypatch.setattr(rebuild_tex.config, "AUTHOR_NAME", "Leshem Choshen")
    return str(path)


# ── the happy path ───────────────────────────────────────────────────────────

def test_every_section_is_updated(stats):
    out, problems = update_tex(TEX, CATS)
    assert problems == []
    assert "\\nocite{j1,j2}" in out
    assert "\\nocite{c1}" in out
    assert "\\nocite{r1}" in out
    assert "\\nocite{w1}" in out
    assert "\\nocite{d1}" in out
    for stale in ("old_journal", "old_conference", "old_review", "old_workshop",
                  "old_draft"):
        assert stale not in out


def test_journals_and_conferences_are_not_confused(stats):
    """Both live in the same chapter, distinguished only by a comment marker."""
    out, _ = update_tex(TEX, CATS)
    journals_at = out.index("\\nocite{j1,j2}")
    conferences_at = out.index("\\nocite{c1}")
    assert journals_at < out.index("% Conferences:") < conferences_at


def test_stats_are_written(stats):
    out, problems = update_tex(TEX, CATS)
    assert problems == []
    assert "Citations\t6286\nh-index\t39\n" in out


def test_running_twice_changes_nothing_more(stats):
    once, _ = update_tex(TEX, CATS)
    twice, problems = update_tex(once, CATS)
    assert problems == []
    assert once == twice


def test_a_commented_out_nocite_is_not_touched(stats):
    """The template keeps commented alternatives; they must survive."""
    tex = TEX.replace("\\chapter*{Review Papers}\n\\nocite{old_review}",
                      "\\chapter*{Review Papers}\n%\\nocite{commented_out}\n\\nocite{old_review}")
    out, problems = update_tex(tex, CATS)
    assert problems == []
    assert "%\\nocite{commented_out}" in out
    assert "\\nocite{r1}" in out


def test_empty_category_writes_an_empty_nocite(stats):
    """A section with no papers must still be rewritten, not left stale."""
    empty = CATS._replace(reviews=[])
    out, problems = update_tex(TEX, empty)
    assert problems == []
    assert "\\nocite{}" in out
    assert "old_review" not in out


# ── the silent-staleness failures this file exists for ───────────────────────

def test_a_renamed_chapter_is_an_error_not_a_warning(stats):
    """Previously: that section kept its old paper list, and the run succeeded."""
    tex = TEX.replace("\\chapter*{Refereed Articles}", "\\chapter*{Refereed articles}")
    out, problems = update_tex(tex, CATS)
    assert any("journals" in p and "not found" in p for p in problems)


def test_a_reflowed_stats_block_is_an_error(stats):
    """Editing that line in Overleaf silently froze the citation total."""
    tex = TEX.replace("\\textbf{Citations\t100\nh-index\t9\n}",
                      "\\textbf{Citations: 100, h-index: 9}")
    out, problems = update_tex(tex, CATS)
    assert any("Citations/h-index" in p for p in problems)


def test_a_missing_conferences_marker_is_an_error(stats):
    tex = TEX.replace("% Conferences:", "% Conference papers:")
    out, problems = update_tex(tex, CATS)
    assert any("conferences" in p for p in problems)


def test_a_chapter_with_no_nocite_is_an_error(stats):
    tex = TEX.replace("\\chapter*{Workshop Papers}\n\\nocite{old_workshop}",
                      "\\chapter*{Workshop Papers}")
    out, problems = update_tex(tex, CATS)
    assert any("workshops" in p for p in problems)


def test_a_missing_author_line_is_an_error(stats):
    tex = TEX.replace("\\noindent\\today\n\nLeshem Choshen\n", "")
    out, problems = update_tex(tex, CATS)
    assert any("author-name line" in p for p in problems)


def test_main_refuses_to_write_a_partially_updated_document(tmp_path, monkeypatch,
                                                            stats):
    """The file must be left alone rather than written half-updated."""
    tex_path = tmp_path / "main.tex"
    broken = TEX.replace("\\chapter*{Review Papers}", "\\chapter*{Reviews}")
    tex_path.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(rebuild_tex, "TEX_PATH", str(tex_path))
    monkeypatch.setattr(rebuild_tex, "patch_bst_author", lambda *a, **k: None)

    with pytest.raises(TexUpdateError) as excinfo:
        rebuild_tex.main(CATS)
    assert "reviews" in str(excinfo.value)
    assert tex_path.read_text(encoding="utf-8") == broken, "must not be rewritten"


def test_main_writes_when_everything_resolves(tmp_path, monkeypatch, stats):
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(TEX, encoding="utf-8")
    monkeypatch.setattr(rebuild_tex, "TEX_PATH", str(tex_path))
    monkeypatch.setattr(rebuild_tex, "patch_bst_author", lambda *a, **k: None)
    rebuild_tex.main(CATS)
    assert "\\nocite{j1,j2}" in tex_path.read_text(encoding="utf-8")


# ── author name substitution ─────────────────────────────────────────────────

def test_author_name_is_replaced_from_config(tmp_path, monkeypatch, stats):
    monkeypatch.setattr(rebuild_tex.config, "AUTHOR_NAME", "Ada Lovelace")
    out, problems = update_tex(TEX, CATS)
    assert problems == []
    assert "\n\nAda Lovelace\n" in out
    assert "Leshem Choshen" not in out


def test_author_name_with_a_backslash_does_not_break_the_substitution(
        monkeypatch, stats):
    """re.sub parses its replacement, so a LaTeX accent used to raise."""
    monkeypatch.setattr(rebuild_tex.config, "AUTHOR_NAME", r"Ren\'e Descartes")
    out, problems = update_tex(TEX, CATS)
    assert problems == []
    assert r"Ren\'e Descartes" in out


# ── the section list must stay in step with BibCategories ────────────────────

def test_every_bib_category_has_a_place_in_the_document():
    """Adding a category to BibCategories without an anchor would drop it."""
    covered = {field for field, _kind, _anchor in rebuild_tex._SECTIONS}
    assert covered == set(BibCategories._fields)


def test_the_live_document_still_has_every_anchor():
    """Guards against an Overleaf-side edit that would break a real run."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "overleaf", "main.tex")
    if not os.path.exists(path):
        pytest.skip("overleaf submodule not checked out")
    tex = open(path).read()
    for _field, kind, anchor in rebuild_tex._SECTIONS:
        needle = f"\\chapter*{{{anchor}}}" if kind == "chapter" else anchor
        assert needle in tex, f"anchor missing from the live main.tex: {anchor!r}"
    assert rebuild_tex._STATS_RE.search(tex), "stats block shape changed"
    assert rebuild_tex._AUTHOR_LINE_RE.search(tex), "author line shape changed"


def test_nocite_formatting():
    assert _nocite_str(["a", "b"]) == "\\nocite{a,b}"
    assert _nocite_str([]) == "\\nocite{}"


def test_bst_author_name_extraction():
    assert _find_author_names_in_bst('t "Leshem Choshen" =') == ["Leshem Choshen"]
    assert _find_author_names_in_bst("no names here") == []
