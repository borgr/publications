"""The one-time token install: where the URL comes from, and what is left behind.

The hand-over file is the part worth testing. It holds a live token in plaintext,
so two things have to hold and neither is visible by reading the happy path: it is
deleted once the token is safely in the credential store, and it is *not* deleted
when storing failed, because then it is the only copy left to retry from.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import install_overleaf_credential as installer

import overleaf_auth

URL = "https://git:olp_tok@git.overleaf.com/abc123"


@pytest.fixture
def stored(monkeypatch):
    """Record what would be stored, instead of touching the real keychain."""
    calls = []
    monkeypatch.setattr(installer.overleaf_auth, "store_credential",
                        lambda url, repo: calls.append(url) or "osxkeychain")
    monkeypatch.setattr(installer.overleaf_auth, "check_credential",
                        lambda repo: None)
    monkeypatch.delenv(overleaf_auth.ENV_VAR, raising=False)
    return calls


def test_the_url_file_is_used_and_then_removed(tmp_path, stored, capsys):
    path = tmp_path / "overleaf_git_url"
    path.write_text(URL + "\n")

    assert installer.main(["--url-file", str(path)]) == 0
    assert stored == [URL]
    assert not path.exists(), "the plaintext token was left on disk"


def test_the_token_is_not_printed(tmp_path, stored, capsys):
    path = tmp_path / "overleaf_git_url"
    path.write_text(URL + "\n")
    installer.main(["--url-file", str(path)])
    assert "olp_tok" not in capsys.readouterr().out


def test_keep_leaves_the_file_alone(tmp_path, stored):
    path = tmp_path / "overleaf_git_url"
    path.write_text(URL)
    assert installer.main(["--url-file", str(path), "--keep"]) == 0
    assert path.exists()


def test_a_rejected_token_leaves_the_file_to_retry_from(tmp_path, stored, monkeypatch):
    """Deleting it here would destroy the only copy of a token that may just have
    been pasted with a typo."""
    monkeypatch.setattr(installer.overleaf_auth, "check_credential",
                        lambda repo: "Cannot authenticate to Overleaf\n  git said: no")
    path = tmp_path / "overleaf_git_url"
    path.write_text(URL)
    assert installer.main(["--url-file", str(path)]) == 1
    assert path.exists()


def test_a_file_inside_the_repository_is_refused_rather_than_read(tmp_path, stored):
    """Because `git add -A` in step 7 would commit it to a public repo."""
    path = os.path.join(ROOT, "overleaf_git_url_test_should_not_be_read")
    assert installer.main(["--url-file", path]) == 1
    assert stored == []


def test_the_environment_is_the_fallback_and_leaves_nothing_to_delete(
        tmp_path, stored, monkeypatch):
    monkeypatch.setenv(overleaf_auth.ENV_VAR, URL)
    assert installer.main(["--url-file", str(tmp_path / "absent")]) == 0
    assert stored == [URL]
