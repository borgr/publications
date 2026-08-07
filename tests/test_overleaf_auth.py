"""The Overleaf push credential: found, kept out of logs, and never blocking.

The bug these cover: `preflight` checked everything step 7 needs except the one
thing step 7 does, so a run with no stored credential did a full Scholar fetch and
six steps before failing -- and failed by *asking* for a password, which under
launchd means a GUI dialog and an indefinite wait while holding the run lock.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import overleaf_auth


class TestNoninteractiveEnv:
    def test_both_prompts_are_closed_off(self):
        env = overleaf_auth.noninteractive_env({"PATH": os.environ["PATH"]})
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] and env["SSH_ASKPASS"]

    def test_the_askpass_program_exists_and_fails(self):
        """Naming a program that is not there would leave git prompting anyway."""
        askpass = overleaf_auth.noninteractive_env()["GIT_ASKPASS"]
        assert os.path.exists(askpass), askpass
        assert subprocess.run([askpass], capture_output=True).returncode != 0

    def test_the_rest_of_the_environment_survives(self):
        env = overleaf_auth.noninteractive_env({"KEEP": "me"})
        assert env["KEEP"] == "me"


class TestRedact:
    @pytest.mark.parametrize("text, expected", [
        ("https://git:olp_abc123@git.overleaf.com/p", "https://***@git.overleaf.com/p"),
        ("fatal: could not read Password for 'https://git@git.overleaf.com'",
         "fatal: could not read Password for 'https://***@git.overleaf.com'"),
        # Nothing to hide, so nothing is touched.
        ("https://git.overleaf.com/project", "https://git.overleaf.com/project"),
        ("remote: Permission denied", "remote: Permission denied"),
    ])
    def test_a_credential_in_a_url_is_blanked(self, text, expected):
        assert overleaf_auth.redact(text) == expected

    def test_the_token_is_gone_and_not_merely_shortened(self):
        out = overleaf_auth.redact("push to https://git:olp_SECRETVALUE@x.com/1 failed")
        assert "olp_SECRETVALUE" not in out


class TestSplitUrl:
    def test_a_complete_url_splits_into_helper_fields(self):
        protocol, host, user, password, path = overleaf_auth.split_url(
            "https://git:olp_tok@git.overleaf.com/67d33c3c")
        assert (protocol, host, user, password, path) == (
            "https", "git.overleaf.com", "git", "olp_tok", "/67d33c3c")

    def test_a_url_with_no_token_is_rejected_with_the_shape_it_needs(self):
        """The most likely mistake: Overleaf shows the URL and the token apart."""
        with pytest.raises(ValueError) as excinfo:
            overleaf_auth.split_url("https://git.overleaf.com/67d33c3c")
        assert "no token" in str(excinfo.value)
        assert "git:TOKEN@" in str(excinfo.value)

    @pytest.mark.parametrize("bad", ["", "git.overleaf.com/p", "not a url at all"])
    def test_something_that_is_not_a_url_is_rejected(self, bad):
        with pytest.raises(ValueError):
            overleaf_auth.split_url(bad)

    def test_a_missing_username_defaults_to_git(self):
        assert overleaf_auth.split_url("https://:olp_tok@git.overleaf.com/p")[2] == "git"


class TestCheckCredential:
    def test_a_directory_that_is_not_a_repo_is_not_this_check_s_business(self, tmp_path):
        """`check_overleaf_present` reports a missing submodule; two reports of one
        problem would send the reader to the wrong instruction."""
        assert overleaf_auth.check_credential(str(tmp_path)) is None

    def test_an_unreachable_remote_is_reported_with_the_fix(self, tmp_path):
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://git@git.overleaf.com/000000000000000000000000"],
                       check=True)

        problem = overleaf_auth.check_credential(str(repo))
        assert problem is not None
        assert "install_overleaf_credential.py" in problem

    def test_it_fails_rather_than_waiting_for_a_password(self, tmp_path):
        """The whole point: no terminal, no dialog, no wait.

        A prompt here is what made step 7 hang under launchd. If the
        non-interactive environment ever stops being applied, this test stops
        returning.
        """
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://git@git.overleaf.com/000000000000000000000000"],
                       check=True)
        problem = overleaf_auth.check_credential(str(repo))
        # Returning at all is most of the claim. What git says depends on where the
        # test runs: it asks for a password when it can reach Overleaf, and fails
        # to resolve the host on a runner with no network -- but it must never sit
        # waiting, which is the state the assertion below cannot be reached from.
        assert any(s in problem for s in (
            "terminal prompts disabled", "Authentication failed",
            "could not resolve", "Could not resolve")), problem

    def test_the_reported_error_carries_no_credential(self, tmp_path):
        """Whatever git says goes into a log and a desktop notification."""
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://git:olp_LEAKME@git.overleaf.com/0000"], check=True)
        assert "olp_LEAKME" not in (overleaf_auth.check_credential(str(repo)) or "")


class TestUrlFromEnv:
    def test_absent_is_empty_not_none(self, monkeypatch):
        monkeypatch.delenv(overleaf_auth.ENV_VAR, raising=False)
        assert overleaf_auth.url_from_env() == ""

    def test_surrounding_whitespace_is_dropped(self, monkeypatch):
        """A URL pasted into a shell or a secret often arrives with a newline."""
        monkeypatch.setenv(overleaf_auth.ENV_VAR, "  https://git:t@h/p\n")
        assert overleaf_auth.url_from_env() == "https://git:t@h/p"


class TestUrlFromFile:
    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert overleaf_auth.url_from_file(str(tmp_path / "nope")) == ""

    def test_the_url_is_read_without_its_trailing_newline(self, tmp_path):
        """Every editor adds one, and a newline inside a password field would be
        sent to the credential helper as part of the token."""
        path = tmp_path / "url"
        path.write_text("https://git:olp_tok@git.overleaf.com/p\n")
        assert overleaf_auth.url_from_file(str(path)) == \
            "https://git:olp_tok@git.overleaf.com/p"

    def test_the_default_location_is_outside_any_repository(self):
        """It is a plaintext token; inside a working tree, `git add -A` reaches it."""
        assert overleaf_auth.URL_FILE.startswith(os.path.expanduser("~"))
        assert not overleaf_auth.is_inside(overleaf_auth.URL_FILE, ROOT)


class TestIsInside:
    def test_a_file_in_the_directory_is_inside(self, tmp_path):
        assert overleaf_auth.is_inside(str(tmp_path / "f"), str(tmp_path))

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path):
        """String containment would call `/x/repo-notes` a child of `/x/repo`."""
        (tmp_path / "repo").mkdir()
        assert not overleaf_auth.is_inside(str(tmp_path / "repo-notes"),
                                          str(tmp_path / "repo"))

    def test_a_path_reached_through_dotdot_is_still_inside(self, tmp_path):
        """`--url-file repo/../repo/token` must not slip past the refusal."""
        assert overleaf_auth.is_inside(
            str(tmp_path / "sub" / ".." / "token"), str(tmp_path))


class TestStoreCredential:
    def test_a_repo_with_no_helper_is_refused_with_the_command_to_fix_it(self, tmp_path):
        """Storing into no store would report success and change nothing."""
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # An empty local value shadows any global helper, so the test does not
        # depend on how the machine running it is configured.
        subprocess.run(["git", "-C", str(repo), "config", "credential.helper", ""],
                       check=True)
        with pytest.raises(ValueError) as excinfo:
            overleaf_auth.store_credential(
                "https://git:olp_tok@git.overleaf.com/p", str(repo))
        assert "credential.helper" in str(excinfo.value)

    def test_the_token_reaches_the_store_git_would_read_it_back_from(self, tmp_path):
        """Against a real helper, so this covers what `git credential approve`
        actually does rather than that it was called.

        Uses `store --file=` -- a plaintext file -- because it is the one helper
        available on every platform the suite runs on. The real install uses
        whatever `credential.helper` is set to, which on macOS is the keychain.
        """
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        creds = tmp_path / "creds"
        subprocess.run(["git", "-C", str(repo), "config", "credential.helper",
                        f"store --file={creds}"], check=True)

        helper = overleaf_auth.store_credential(
            "https://git:olp_stored@git.overleaf.com/abc123", str(repo))
        assert "store" in helper

        # Ask git for it the way a push would, rather than reading the file: what
        # matters is that git finds it under the host and user it will look up.
        got = subprocess.run(
            ["git", "-C", str(repo), "credential", "fill"],
            input="protocol=https\nhost=git.overleaf.com\nusername=git\n\n",
            capture_output=True, text=True, env=overleaf_auth.noninteractive_env())
        assert "password=olp_stored" in got.stdout

    def test_a_tokenless_url_is_refused_before_anything_is_stored(self, tmp_path):
        repo = tmp_path / "overleaf"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        with pytest.raises(ValueError) as excinfo:
            overleaf_auth.store_credential("https://git.overleaf.com/p", str(repo))
        assert "no token" in str(excinfo.value)
