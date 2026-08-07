"""Pruning orig.bib: what is unreachable, and what only looks unreachable.

orig.bib grows by about one dead entry per published paper -- the arXiv record stays
behind when step 3 moves the row to the DBLP key. Sixty-nine of a hundred and
seventy-eight entries were dead when this was written.

The risk in pruning is not the deletions, it is the wrong ones: an entry no table row
names can still be reachable through main.tex or through a Scholar binding, and
deleting one of those loses a citation or breaks a hand-written \\nocite. Each of the
three ways an entry stays reachable has a test here.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import prune_bib

from bib_utils import parse_bibtex
from identity import IdentityStore

BIB = (
    "@inproceedings{used2020paper,\n"
    "  title = {A Paper The Table Names},\n"
    "  author = {A Author},\n"
    "}\n\n"
    "@misc{orphan2020preprint,\n"
    "  title = {The Preprint It Superseded},\n"
    "  author = {A Author},\n"
    "}\n\n"
    "@misc{cited2020tex,\n"
    "  title = {Named Only By main.tex},\n"
    "  author = {A Author},\n"
    "}\n"
)


def _plan(bib=BIB, table_keys=("used2020paper",), store=None, tex=None):
    return prune_bib.plan(bib, set(table_keys), store or IdentityStore(),
                          tex or os.devnull)


def test_an_entry_the_table_names_is_not_even_considered():
    removable, protected = _plan()
    assert "used2020paper" not in {k for k, _, _ in removable + protected}


def test_an_entry_nothing_refers_to_is_removable():
    removable, _protected = _plan()
    assert "orphan2020preprint" in {k for k, _, _ in removable}


def test_an_entry_cited_in_main_tex_is_protected(tmp_path):
    """The CV's own \\nocite blocks are regenerated, but hand-written ones are not,
    and a commented-out one is a note to self rather than a dead reference."""
    tex = tmp_path / "main.tex"
    tex.write_text("% \\nocite{something,cited2020tex,other}\n")
    removable, protected = _plan(tex=str(tex))
    assert "cited2020tex" in {k for k, _, _ in protected}
    assert "cited2020tex" not in {k for k, _, _ in removable}


def test_a_key_that_is_only_a_prefix_of_a_cited_one_is_not_protected(tmp_path):
    """`wang2026mindgames` is a prefix of `wang2026mindgameslivearenaevaluating`,
    so substring matching protects the wrong entry and reports it as cited."""
    tex = tmp_path / "main.tex"
    tex.write_text("\\nocite{orphan2020preprintandmore}\n")
    removable, _protected = _plan(tex=str(tex))
    assert "orphan2020preprint" in {k for k, _, _ in removable}


def test_an_entry_holding_a_scholar_binding_is_protected():
    """Deleting it would leave the binding pointing at nothing and the paper's
    citations landing nowhere."""
    store = IdentityStore()
    store.record("orphan2020preprint", scholar_id="abc:123")
    removable, protected = _plan(store=store)
    assert "orphan2020preprint" in {k for k, _, _ in protected}
    assert "orphan2020preprint" not in {k for k, _, _ in removable}


def test_pruning_removes_exactly_the_named_entry():
    out = prune_bib.prune(BIB, {"orphan2020preprint"})
    keys = {e["item_name"] for e in parse_bibtex(out)}
    assert keys == {"used2020paper", "cited2020tex"}


def test_pruning_leaves_the_survivors_separated():
    """Consuming the blank line on both sides of a deletion runs two entries
    together, and `@misc{a,...}@misc{b,...}` parses as one."""
    out = prune_bib.prune(BIB, {"orphan2020preprint"})
    assert "}\n\n@misc{cited2020tex" in out
    assert out.endswith("\n")


def test_pruning_nothing_changes_nothing_but_the_trailing_newline():
    assert prune_bib.prune(BIB, set()) == BIB


@pytest.mark.parametrize("keys", [
    {"used2020paper"}, {"cited2020tex"}, {"used2020paper", "cited2020tex"},
    {"used2020paper", "orphan2020preprint", "cited2020tex"},
])
def test_every_combination_leaves_parseable_bibtex(keys):
    out = prune_bib.prune(BIB, keys)
    assert {e["item_name"] for e in parse_bibtex(out)} == \
        {e["item_name"] for e in parse_bibtex(BIB)} - keys


def test_a_generated_pretitle_field_is_stripped():
    """It is written by build_bib from the table's tag columns and stripped again on
    every build, so a copy in the source can only be a stale duplicate."""
    bib = ("@inproceedings{p,\n    pretitle={\\LANG\\META},\n"
           "  title = {A Paper},\n  author = {A Author},\n}\n")
    out, n = prune_bib.strip_generated_fields(bib)
    assert n == 1
    assert "pretitle" not in out
    assert "title = {A Paper}" in out and "author = {A Author}" in out


def test_stripping_leaves_an_entry_without_one_alone():
    out, n = prune_bib.strip_generated_fields(BIB)
    assert (out, n) == (BIB, 0)


# ── the command itself ────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A bib file and a one-row table, in place of the real ones."""
    import pandas as pd
    bib = tmp_path / "orig.bib"
    bib.write_text(BIB)
    monkeypatch.setattr(prune_bib, "BIB_PATH", str(bib))
    monkeypatch.setattr(prune_bib, "TEX_PATH", str(tmp_path / "main.tex"))
    monkeypatch.setattr(prune_bib, "read_table",
                        lambda: pd.DataFrame([{"Bib": "used2020paper"}]))
    monkeypatch.setattr(prune_bib.IdentityStore, "load",
                        classmethod(lambda cls: IdentityStore()))
    return bib


def test_the_default_is_to_report_and_change_nothing(repo, capsys):
    """A destructive default on a file this size is how you lose an entry you
    meant to keep."""
    assert prune_bib.main([]) == 0
    assert repo.read_text() == BIB
    assert "not modified" in capsys.readouterr().out


def test_apply_rewrites_the_file(repo, capsys):
    assert prune_bib.main(["--apply"]) == 0
    keys = {e["item_name"] for e in parse_bibtex(repo.read_text())}
    assert keys == {"used2020paper"}


def test_a_clean_file_says_so_and_is_left_alone(repo, monkeypatch, capsys):
    import pandas as pd
    monkeypatch.setattr(prune_bib, "read_table", lambda: pd.DataFrame(
        [{"Bib": k} for k in ("used2020paper", "orphan2020preprint", "cited2020tex")]))
    assert prune_bib.main(["--apply"]) == 0
    assert "Nothing to clean" in capsys.readouterr().out
    assert repo.read_text() == BIB


def test_a_rewrite_that_lost_an_entry_is_refused(repo, monkeypatch, capsys):
    """The count check is the only thing standing between a bug in `prune` and a
    bibliography with entries silently missing from it."""
    monkeypatch.setattr(prune_bib, "prune", lambda text, keys: "")
    assert prune_bib.main(["--apply"]) == 1
    assert "Refusing to write" in capsys.readouterr().out
    assert repo.read_text() == BIB
