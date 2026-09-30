"""The push step, against real git repositories in a temp directory.

This covers the failure that had the pipeline stuck: editing the project in
Overleaf's own editor advances its remote, after which every push from here is
rejected until a human pulls. The fix is to rebase and retry, and the fix needs
a test because the failure only appears when a remote has moved.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare remote with one commit, plus a working clone of it."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True, check=True)
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "main.tex").write_text("original\n")
    git(seed, "add", "main.tex")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], capture_output=True, check=True)
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "pull.rebase", "true")
    return remote, clone, seed


def test_clean_push_succeeds(remote_and_clone):
    _, clone, _ = remote_and_clone
    (clone / "main.tex").write_text("updated\n")
    assert update._git_commit_and_push(str(clone), ["main.tex"], "msg", "origin")
    assert "updated" in git(clone, "show", "origin/main:main.tex").stdout


def test_nothing_to_commit_still_succeeds(remote_and_clone):
    """A no-change run must not report failure and must not exit non-zero."""
    _, clone, _ = remote_and_clone
    assert update._git_commit_and_push(str(clone), ["main.tex"], "msg", "origin")


def test_push_rebases_when_the_remote_has_moved(remote_and_clone):
    """The Overleaf-editor case: previously this needed a manual pull forever."""
    _, clone, seed = remote_and_clone

    # Someone edits a different file in the Overleaf editor and it lands upstream.
    (seed / "notes.tex").write_text("edited in overleaf\n")
    git(seed, "add", "notes.tex")
    git(seed, "commit", "-m", "overleaf edit")
    git(seed, "push", "origin", "main")

    (clone / "main.tex").write_text("pipeline output\n")
    assert update._git_commit_and_push(str(clone), ["main.tex"], "msg", "origin")

    # Both changes survive: ours rebased on top of theirs.
    assert "pipeline output" in git(clone, "show", "origin/main:main.tex").stdout
    assert "edited in overleaf" in git(clone, "show", "origin/main:notes.tex").stdout


def test_conflicting_remote_change_fails_cleanly(remote_and_clone):
    """A genuine conflict must report failure, not leave a rebase half-applied."""
    _, clone, seed = remote_and_clone

    (seed / "main.tex").write_text("their version\n")
    git(seed, "add", "main.tex")
    git(seed, "commit", "-m", "their edit")
    git(seed, "push", "origin", "main")

    (clone / "main.tex").write_text("our version\n")
    assert not update._git_commit_and_push(str(clone), ["main.tex"], "msg", "origin")
    # No rebase left in progress, so the next run is not wedged.
    assert not os.path.exists(os.path.join(clone, ".git", "rebase-merge"))
    assert not os.path.exists(os.path.join(clone, ".git", "rebase-apply"))


def test_unreachable_remote_reports_failure(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    (repo / "f.txt").write_text("x\n")
    assert not update._git_commit_and_push(str(repo), ["f.txt"], "msg", "origin")


def test_missing_files_are_skipped_not_fatal(remote_and_clone):
    _, clone, _ = remote_and_clone
    assert update._git_commit_and_push(
        str(clone), ["main.tex", "does_not_exist.bib"], "msg", "origin")


def test_step7_returns_false_when_a_push_fails(tmp_path, monkeypatch, capsys):
    """update.py must exit non-zero on this, so unattended runs are noticed."""
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda *a, **k: False)
    assert update.step7_push(dry_run=False) is False


def test_step7_dry_run_does_not_touch_git(monkeypatch):
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda *a, **k: pytest.fail("dry run must not push"))
    assert update.step7_push(dry_run=True) is True


def test_a_detached_checkout_is_put_on_its_branch_and_pushed(remote_and_clone):
    """What `git submodule update --init` leaves: a detached HEAD, from which the
    commit belonged to no branch and the recovery rebase failed outright."""
    _, clone, seed = remote_and_clone
    git(clone, "checkout", "-q", "--detach")
    (seed / "notes.tex").write_text("edited in overleaf\n")
    git(seed, "add", "notes.tex")
    git(seed, "commit", "-m", "overleaf edit")
    git(seed, "push", "origin", "main")

    (clone / "main.tex").write_text("pipeline output\n")
    assert update._git_commit_and_push(str(clone), ["main.tex"], "msg", "origin")
    assert git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert "pipeline output" in git(clone, "show", "origin/main:main.tex").stdout
    assert "edited in overleaf" in git(clone, "show", "origin/main:notes.tex").stdout


def test_the_submodule_pointer_is_held_back_when_overleaf_refused(monkeypatch):
    """Otherwise GitHub records an Overleaf commit that exists on one machine only."""
    calls = []

    def fake(repo_dir, files, message, remote):
        calls.append(list(files))
        return repo_dir != update.OVERLEAF_DIR
    monkeypatch.setattr(update, "_git_commit_and_push", fake)
    assert update.step7_push(dry_run=False) is False
    assert "overleaf" not in calls[1]
    assert "orig.bib" in calls[1]


def test_the_submodule_pointer_is_pushed_when_overleaf_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(update, "_git_commit_and_push",
                        lambda repo_dir, files, message, remote: calls.append(list(files)) or True)
    assert update.step7_push(dry_run=False) is True
    assert "overleaf" in calls[1]
