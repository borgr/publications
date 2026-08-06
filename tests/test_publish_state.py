"""Detecting output that was built but never published."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
import worklist


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def overleaf(tmp_path, monkeypatch):
    """A bare 'Overleaf' remote plus a clone standing in for overleaf/."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)
    clone = tmp_path / "overleaf"
    subprocess.run(["git", "clone", str(remote), str(clone)],
                   capture_output=True, check=True)
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "T")
    (clone / "main.tex").write_text("v1\n"); (clone / "Wzmn.bib").write_text("v1\n")
    git(clone, "add", "main.tex", "Wzmn.bib")
    git(clone, "commit", "-m", "seed")
    git(clone, "push", "origin", "main")
    git(clone, "branch", "--set-upstream-to=origin/main", "main")
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(clone))
    return clone


def test_a_clean_published_state_reports_nothing(overleaf):
    assert worklist._unpublished_output() == []


def test_uncommitted_output_is_reported(overleaf):
    """Exactly the state a --no-push run leaves behind."""
    (overleaf / "main.tex").write_text("v2 with new citation totals\n")
    sections = worklist._unpublished_output()
    assert any("not committed" in title for title, _b, _l in sections)
    assert any("main.tex" in line for _t, _b, lines in sections for line in lines)


def test_committed_but_unpushed_output_is_reported(overleaf):
    (overleaf / "main.tex").write_text("v2\n")
    git(overleaf, "add", "main.tex"); git(overleaf, "commit", "-m", "update")
    sections = worklist._unpublished_output()
    assert any("not pushed" in title for title, _b, _l in sections)


def test_both_conditions_are_reported_together(overleaf):
    (overleaf / "main.tex").write_text("v2\n")
    git(overleaf, "add", "main.tex"); git(overleaf, "commit", "-m", "update")
    (overleaf / "Wzmn.bib").write_text("v2\n")
    titles = [t for t, _b, _l in worklist._unpublished_output()]
    assert len(titles) == 2


def test_changes_to_other_files_are_ignored(overleaf):
    """Only the two files whose staleness shows up in the compiled CV."""
    (overleaf / "notes.txt").write_text("scratch\n")
    assert worklist._unpublished_output() == []


def test_a_missing_overleaf_dir_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(tmp_path / "nope"))
    assert worklist._unpublished_output() == []


def test_a_non_git_overleaf_dir_is_not_an_error(monkeypatch, tmp_path):
    d = tmp_path / "plain"; d.mkdir()
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(d))
    assert worklist._unpublished_output() == []
