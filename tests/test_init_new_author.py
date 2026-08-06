"""Tests for the fork path: init_new_author.py.

This is the one script a person adapting the pipeline has to run, and it is the
least exercised by the original author, who runs it once and never again. It also
does the two things worth being careful about: deleting data, and rewriting a git
submodule so the fork stops pointing at someone else's Overleaf project.

Everything here works on a throwaway repository and a local bare "Overleaf"
remote, so the suite stays offline and cannot touch the real project.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import init_new_author as ina


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, check=check)


@pytest.fixture(autouse=True)
def _allow_local_submodules(monkeypatch):
    """git refuses file:// submodules by default (CVE-2022-39253)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


@pytest.fixture
def fork(tmp_path):
    """A repository shaped like a fresh fork, with personal data still in it."""
    root = tmp_path / "publications"
    (root / "templates").mkdir(parents=True)
    (root / "overleaf").mkdir()

    (root / "templates" / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nBLANK\n\\end{document}\n")
    (root / "papers.csv").write_text("Title,Venue,Year,bibkey\nA Paper,ACL,2024,a24\n")
    (root / "citations.csv").write_text("title,citations\nA Paper,7\n")
    (root / "orig.bib").write_text("@inproceedings{a24, title = {A Paper} }\n")
    (root / "profile_stats.json").write_text(json.dumps({"citations": 5829,
                                                         "h_index": 38}))
    (root / "overleaf" / "Wzmn.bib").write_text("@inproceedings{a24, title={A Paper}}\n")
    for name in ("identity.json", "resolve_attempts.json",
                 ".pipeline_state.json", "WORKLIST.md", "tmp.csv"):
        (root / name).write_text("{}" if name.endswith(".json") else "stuff")

    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


@pytest.fixture
def empty_overleaf_remote(tmp_path):
    """A brand-new Overleaf project: one empty commit, no files."""
    src = tmp_path / "empty-src"
    src.mkdir()
    _git(src.parent, "init", "-q", str(src))
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    _git(src, "commit", "-q", "--allow-empty", "-m", "empty project")
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def overleaf_remote(tmp_path):
    """A bare repo standing in for an Overleaf project."""
    src = tmp_path / "ovl-src"
    src.mkdir()
    _git(src.parent, "init", "-q", str(src))
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    (src / "main.tex").write_text("theirs\n")
    _git(src, "add", "main.tex")
    _git(src, "commit", "-qm", "init")
    bare = tmp_path / "ovl.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


# --- wiping personal data ------------------------------------------------

def test_the_table_is_emptied_but_keeps_its_columns(fork):
    ina.wipe_contributions_xlsx(root=str(fork))
    text = (fork / "papers.csv").read_text()
    assert "Title" in text and "bibkey" in text
    assert "A Paper" not in text


def test_citations_keep_a_header_row(fork):
    ina.wipe_citations_csv(root=str(fork))
    lines = [l for l in (fork / "citations.csv").read_text().splitlines() if l.strip()]
    assert len(lines) == 1, "expected a header and no data rows"
    assert "A Paper" not in lines[0]


def test_profile_stats_are_zeroed_rather_than_deleted(fork):
    ina.wipe_profile_stats(root=str(fork))
    stats = json.loads((fork / "profile_stats.json").read_text())
    assert stats == {"citations": 0, "h_index": 0}


def test_machine_state_is_deleted(fork):
    ina.wipe_resolve_attempts(root=str(fork))
    for name in ("identity.json", "resolve_attempts.json",
                 ".pipeline_state.json", "WORKLIST.md"):
        assert not (fork / name).exists(), name


def test_no_personal_file_survives_a_full_wipe(fork):
    """Drives off PERSONAL_FILES, so a newly added data file cannot be forgotten."""
    ina.wipe_citations_csv(root=str(fork))
    ina.wipe_profile_stats(root=str(fork))
    ina.wipe_orig_bib(root=str(fork))
    ina.wipe_wzmn_bib(root=str(fork))
    ina.wipe_tmp_csv(root=str(fork))
    ina.wipe_resolve_attempts(root=str(fork))
    ina.wipe_contributions_xlsx(root=str(fork))

    survivors = []
    for name, _ in ina.PERSONAL_FILES:
        path = fork / name
        if not path.exists():
            continue
        body = path.read_text()
        # What may remain is structure: a CSV header, or zeroed stats.
        if "A Paper" in body or "5829" in body or body.strip() == "stuff":
            survivors.append(name)
    assert not survivors, f"personal data left in {survivors}"


# --- the template, which is what breaks for a fork -----------------------

def test_the_template_comes_from_the_repo_not_the_submodule(fork):
    """A fork's overleaf/ is its own empty project, so template.tex is not there."""
    assert not (fork / "overleaf" / "template.tex").exists()
    assert ina.reset_main_tex(root=str(fork)) is True
    assert "BLANK" in (fork / "overleaf" / "main.tex").read_text()


def test_the_submodule_copy_is_still_accepted(fork):
    (fork / "templates" / "main.tex").unlink()
    (fork / "overleaf" / "template.tex").write_text("FROM SUBMODULE\n")
    assert ina.reset_main_tex(root=str(fork)) is True
    assert "FROM SUBMODULE" in (fork / "overleaf" / "main.tex").read_text()


def test_a_missing_template_is_a_failure_not_a_warning(fork, capsys):
    (fork / "templates" / "main.tex").unlink()
    assert ina.reset_main_tex(root=str(fork)) is False
    assert "ERROR" in capsys.readouterr().out


def test_main_tex_is_written_even_with_no_overleaf_directory(fork):
    for f in (fork / "overleaf").iterdir():
        f.unlink()
    (fork / "overleaf").rmdir()
    assert ina.reset_main_tex(root=str(fork)) is True
    assert (fork / "overleaf" / "main.tex").exists()


# --- repointing the submodule -------------------------------------------

def test_the_submodule_points_at_the_new_project(fork, overleaf_remote):
    assert ina.replace_overleaf_submodule(str(overleaf_remote), root=str(fork)) is True
    assert str(overleaf_remote) in (fork / ".gitmodules").read_text()
    assert (fork / "overleaf" / "main.tex").read_text() == "theirs\n"


def test_it_works_when_the_submodule_was_never_initialised(fork, overleaf_remote):
    """The normal fork state: cloning the original submodule needs credentials."""
    _git(fork, "submodule", "add", "-q", str(overleaf_remote), "overleaf", check=False)
    _git(fork, "add", "-A")
    _git(fork, "commit", "-qm", "with submodule")
    _git(fork, "submodule", "deinit", "-f", "overleaf")
    assert ina.replace_overleaf_submodule(str(overleaf_remote), root=str(fork)) is True
    assert (fork / "overleaf" / "main.tex").exists()


def test_an_unreachable_project_is_reported_as_a_failure(fork, tmp_path, capsys):
    missing = tmp_path / "does-not-exist.git"
    assert ina.replace_overleaf_submodule(str(missing), root=str(fork)) is False
    assert "ERROR" in capsys.readouterr().out


# --- main() --------------------------------------------------------------

def test_a_failed_swap_exits_nonzero(fork, tmp_path, monkeypatch):
    """Printing 'Done' over a failed swap sent forks on with overleaf/ unchanged."""
    monkeypatch.setattr(ina, "FILE_DIR", str(fork))
    code = ina.main(["--yes", "--overleaf-url", str(tmp_path / "nope.git")])
    assert code == 1


def test_a_clean_run_exits_zero(fork, overleaf_remote, monkeypatch):
    monkeypatch.setattr(ina, "FILE_DIR", str(fork))
    assert ina.main(["--yes", "--overleaf-url", str(overleaf_remote)]) == 0
    assert "A Paper" not in (fork / "papers.csv").read_text()


def test_the_new_project_is_left_with_a_cv(fork, empty_overleaf_remote, monkeypatch):
    """A real new Overleaf project is empty.

    The swap deletes overleaf/ and re-clones over it, so a main.tex written
    before it is gone -- and the run still reported Done, leaving the fork
    nothing to build.
    """
    monkeypatch.setattr(ina, "FILE_DIR", str(fork))
    assert ina.main(["--yes", "--overleaf-url", str(empty_overleaf_remote)]) == 0
    assert "BLANK" in (fork / "overleaf" / "main.tex").read_text()


def test_declining_the_prompt_changes_nothing(fork, monkeypatch):
    monkeypatch.setattr(ina, "FILE_DIR", str(fork))
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert ina.main([]) == 0
    assert "A Paper" in (fork / "papers.csv").read_text()
