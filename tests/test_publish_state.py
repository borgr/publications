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


# ── output somebody else already published ──────────────────────────────────
#
# CI publishes to Overleaf as well as the local run does, which leaves this clone
# a commit behind holding files that are byte-identical to what Overleaf serves.
# Asked about the local HEAD, that is indistinguishable from a --no-push run, and
# it reported two unpublished files on every run for two days -- an item that no
# push could ever clear, in the one section whose whole job is to be trusted.

def _publish_from_elsewhere(clone, remote, text):
    """Commit `text` straight to the remote, as CI's own push would."""
    other = clone.parent / "ci-clone"
    subprocess.run(["git", "clone", str(remote), str(other)],
                   capture_output=True, check=True)
    git(other, "config", "user.email", "ci@example.com")
    git(other, "config", "user.name", "CI")
    (other / "main.tex").write_text(text)
    git(other, "add", "main.tex")
    git(other, "commit", "-m", "chore: publish publications data from CI")
    git(other, "push", "origin", "main")


def test_output_ci_already_published_is_not_reported(overleaf, tmp_path):
    """The local checkout is behind, but Overleaf serves exactly these bytes."""
    _publish_from_elsewhere(overleaf, tmp_path / "remote.git", "v2\n")
    (overleaf / "main.tex").write_text("v2\n")     # what a local rebuild produces
    git(overleaf, "fetch", "origin")
    assert worklist._unpublished_output() == []


def test_output_that_differs_from_what_ci_published_is_still_reported(overleaf, tmp_path):
    """The fix must not silence the real case: being behind is not being current."""
    _publish_from_elsewhere(overleaf, tmp_path / "remote.git", "v2\n")
    (overleaf / "main.tex").write_text("v3 with newer citation totals\n")
    git(overleaf, "fetch", "origin")
    sections = worklist._unpublished_output()
    assert any("not committed" in title for title, _b, _l in sections)


def test_a_file_the_project_does_not_have_yet_is_reported(overleaf):
    """A fresh fork's Overleaf has no Wzmn.bib. It is untracked, and `git diff`
    cannot see an untracked file, so it has to be looked up separately."""
    git(overleaf, "rm", "--quiet", "Wzmn.bib")
    git(overleaf, "commit", "-m", "as if the project never had it")
    git(overleaf, "push", "origin", "main")
    (overleaf / "Wzmn.bib").write_text("freshly built\n")
    sections = worklist._unpublished_output()
    assert any("Wzmn.bib" in line for _t, _b, lines in sections for line in lines)


def test_an_unreachable_remote_falls_back_to_the_local_state(overleaf, monkeypatch):
    """No upstream to compare against must not mean "nothing to report"."""
    git(overleaf, "branch", "--unset-upstream", "main")
    (overleaf / "main.tex").write_text("v2\n")
    sections = worklist._unpublished_output()
    assert any("not committed" in title for title, _b, _l in sections)


def test_the_remote_refresh_never_raises(monkeypatch, tmp_path):
    """It runs before every comparison, so a network failure here would take the
    whole worklist down with it -- including the sections that need no network."""
    monkeypatch.setattr(worklist, "OVERLEAF_DIR", str(tmp_path))
    worklist._refresh_remote_ref()      # not a git repo; must simply return


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
