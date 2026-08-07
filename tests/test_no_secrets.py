"""Fails if a credential is ever committed.

The pipeline pushes to Overleaf, whose git access is a token used as a password
in a URL. That makes `https://git:TOKEN@git.overleaf.com/...` a natural thing to
paste into a config file or a README example -- and this repository is public and
meant to be forked, so a paste like that is a published credential.

The URL itself is fine and lives in .gitmodules: it is an address, and cloning it
without the token fails. What must never appear is the password half.

The scan covers this file too, so its own fixtures are built by concatenation
rather than written out.
"""

import os
import re
import subprocess

import pytest

FILE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# user:password@host -- the shape of a credential embedded in a URL.
_URL_CREDENTIAL_RE = re.compile(r'//([^@/\s:]+):([^@/\s]+)@')

# A documentation example is not a leak. Placeholders are all-caps with
# underscores or angle brackets: YOUR_TOKEN, PASTE_TOKEN_HERE, <token>, xxx.
_PLACEHOLDER_RE = re.compile(r'^(?:[A-Z][A-Z0-9_]*|<[^>]*>|x+|\.+|\*+)$')

# A token on its own, with no URL around it. The scan above only sees the
# `user:password@host` shape, and the natural mistake is simpler than that: the
# Overleaf settings page shows the token and the project URL as two separate
# things, so the token gets pasted somewhere on its own first -- into a note, a
# design doc, a shell snippet in the README. Every prefix here is issued by a
# service this repository actually touches, and each is followed by enough
# entropy that a real one cannot be a placeholder.
_BARE_TOKEN_RE = re.compile(
    r'\b('
    r'olp_[A-Za-z0-9]{20,}'          # Overleaf git token
    r'|ghp_[A-Za-z0-9]{36,}'         # GitHub personal access token (classic)
    r'|github_pat_[A-Za-z0-9_]{50,}'  # GitHub fine-grained token
    r'|gh[opsu]_[A-Za-z0-9]{36,}'    # other GitHub token classes
    r')')

# Files that legitimately discuss the shape of a credential.
_BINARY_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".ico")


def _tracked_files():
    out = subprocess.run(["git", "-C", FILE_DIR, "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p and not p.endswith(_BINARY_SUFFIXES)]


def _tracked_lines():
    for path in _tracked_files():
        full = os.path.join(FILE_DIR, path)
        try:
            with open(full, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (OSError, IsADirectoryError):
            continue  # a submodule entry, or a file removed from the worktree
        for lineno, line in enumerate(text.splitlines(), 1):
            yield path, lineno, line


_REVOKE = (
    "A credential appears to be committed. Revoke the token, then remove it "
    "from history -- deleting the line is not enough, because the old commit "
    "still has it:\n  ")


def test_no_credential_bearing_urls_are_committed():
    leaks = []
    for path, lineno, line in _tracked_lines():
        for m in _URL_CREDENTIAL_RE.finditer(line):
            if _PLACEHOLDER_RE.match(m.group(2)):
                continue
            leaks.append(f"{path}:{lineno}: {m.group(1)}:<redacted>@")
    assert not leaks, _REVOKE + "\n  ".join(leaks)


def test_no_bare_tokens_are_committed():
    """The same credential without a URL around it.

    Overleaf shows the token and the project URL separately, so the token gets
    pasted on its own before it is ever assembled into a URL -- which is the shape
    the scan above cannot see.
    """
    leaks = []
    for path, lineno, line in _tracked_lines():
        for m in _BARE_TOKEN_RE.finditer(line):
            prefix = m.group(1).split("_")[0]
            leaks.append(f"{path}:{lineno}: {prefix}_<redacted>")
    assert not leaks, _REVOKE + "\n  ".join(leaks)


def _url(secret):
    # Concatenated, never a literal: this file is scanned too, and a fixture
    # spelled out in full would make the scanner report itself.
    return "https://git:" + secret + "@git.overleaf.com/abc"


def test_the_scan_would_actually_catch_one():
    """The guard above is only reassuring if it can fail. Prove that it can."""
    m = _URL_CREDENTIAL_RE.search("url = " + _url("olp_9fJk2LmQ8xZ"))
    assert m is not None
    assert not _PLACEHOLDER_RE.match(m.group(2))


@pytest.mark.parametrize("placeholder", [
    "YOUR_TOKEN", "PASTE_TOKEN_HERE", "TOKEN", "<your-token>", "xxxx",
])
def test_documentation_placeholders_are_not_leaks(placeholder):
    m = _URL_CREDENTIAL_RE.search(_url(placeholder))
    assert m is not None and _PLACEHOLDER_RE.match(m.group(2))


# Concatenated for the same reason as _url: a literal here would make the bare
# token scan report this file. The bodies are the real *shapes*, built from
# counted filler -- never a value copied from a real token, split up. Splitting a
# real one would put it in a tracked file and walk it straight past this guard,
# which is the mistake the guard is for.
@pytest.mark.parametrize("token", [
    "olp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "ghp" + "_" + "0123456789abcdefghijklmnopqrstuvwxyzAB",
    "github" + "_pat_" + "11ABCDEFG0" + "abcdefghij" * 5,
])
def test_the_bare_token_scan_would_actually_catch_one(token):
    """As with the URL scan: a guard that cannot fail is not a guard.

    The Overleaf case is not hypothetical. A token was pasted into a tracked file
    in another public repository twice while this pipeline was being built, and
    both times it was caught by eye rather than by anything automatic.
    """
    assert _BARE_TOKEN_RE.search("token = " + token)


@pytest.mark.parametrize("not_a_token", [
    "YOUR_TOKEN", "<your-token>", "olp_YOUR_TOKEN_HERE", "ghp_xxx",
    "olp_short", "developing_a_pipeline", "help_me",
])
def test_the_bare_token_scan_does_not_fire_on_prose(not_a_token):
    """It scans every tracked file including the README, so a false positive
    would make the whole suite red over documentation."""
    assert not _BARE_TOKEN_RE.search(f"see {not_a_token} in the settings page")
