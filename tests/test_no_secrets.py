"""Fails if a credential is ever committed.

The pipeline pushes to Overleaf, whose git access is a token used as a password
in a URL. That makes `https://git:TOKEN@git.overleaf.com/...` a natural thing to
paste into a config file or a README example -- and this repository is public and
meant to be forked, so a paste like that is a published credential.

The URL itself is fine and lives in .gitmodules: it is an address, and cloning it
without the token fails. What must never appear is the password half.
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

# Files that legitimately discuss the shape of a credential.
_BINARY_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".ico")


def _tracked_files():
    out = subprocess.run(["git", "-C", FILE_DIR, "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p and not p.endswith(_BINARY_SUFFIXES)]


def test_no_credential_bearing_urls_are_committed():
    leaks = []
    for path in _tracked_files():
        full = os.path.join(FILE_DIR, path)
        try:
            with open(full, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (OSError, IsADirectoryError):
            continue  # a submodule entry, or a file removed from the worktree
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in _URL_CREDENTIAL_RE.finditer(line):
                if _PLACEHOLDER_RE.match(m.group(2)):
                    continue
                leaks.append(f"{path}:{lineno}: {m.group(1)}:<redacted>@")
    assert not leaks, (
        "A credential appears to be committed. Revoke the token, then remove it "
        "from history -- deleting the line is not enough, because the old commit "
        "still has it:\n  " + "\n  ".join(leaks))


def test_the_scan_would_actually_catch_one():
    """The guard above is only reassuring if it can fail. Prove that it can."""
    m = _URL_CREDENTIAL_RE.search(
        "url = https://git:olp_9fJk2LmQ8xZ@git.overleaf.com/abc")
    assert m is not None
    assert not _PLACEHOLDER_RE.match(m.group(2))


@pytest.mark.parametrize("placeholder", [
    "YOUR_TOKEN", "PASTE_TOKEN_HERE", "TOKEN", "<your-token>", "xxxx",
])
def test_documentation_placeholders_are_not_leaks(placeholder):
    m = _URL_CREDENTIAL_RE.search(f"https://git:{placeholder}@git.overleaf.com/abc")
    assert m is not None and _PLACEHOLDER_RE.match(m.group(2))
