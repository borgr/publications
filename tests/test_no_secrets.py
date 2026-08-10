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

# Below this length a password in a URL cannot be a credential any service here
# issues, so it is a fixture. Needed because the tests for the credential code
# use lowercase fakes -- `olp_tok`, `olp_stored` -- which are more readable than
# shouting, and which the all-caps rule above rejects. This does not create a
# gap: test_no_bare_tokens_are_committed matches every real token shape wherever
# it appears, inside a URL or not, and each of those shapes is longer than this.
_MIN_REAL_SECRET = 20


def _is_fixture(password):
    """True if this password in a URL is documentation or a test double.

    One function rather than an inline condition, so the self-tests below check
    the rule the scan actually applies. They previously asserted on the
    all-caps pattern alone, which stayed true after the length floor was added
    and would have gone on passing while testing the wrong thing.
    """
    return bool(_PLACEHOLDER_RE.match(password)) or len(password) < _MIN_REAL_SECRET

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
            if _is_fixture(m.group(2)):
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


# A key with no recognisable shape, in the slot that invites it.
#
# The two scans above work by recognising a token: `olp_`, `ghp_` and the rest
# announce themselves. A Semantic Scholar key is forty-odd alphanumerics with no
# prefix, indistinguishable from a hash, a test fixture or a commit id, so no
# pattern can find one without firing on prose constantly.
#
# What can be checked is the slot. config.py ships `S2_API_KEY = ""` with a comment
# inviting a key, and config.py is tracked in a public repository -- so the
# documented place to put one is also the one place it must never go. That is the
# whole trap, and it is narrow enough to guard exactly: a tracked file may name a
# credential variable, and must not assign a value to it.
_SECRET_ASSIGNMENT_RE = re.compile(
    r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*'
    r'(?:API_KEY|_TOKEN|_SECRET|_PASSWORD|api_key|_token|_secret|_password))'
    r'\s*[:=]\s*["\']?([^"\'\s#]+)',
)

# The variable named as a default argument, a type annotation or a comparison is
# not a place a key gets pasted, and neither is one assigned from somewhere else.
_NOT_A_LITERAL_RE = re.compile(r'^(?:os\b|config\b|getattr|None|True|False|\d+$)')


def _assigned_secrets(path, line):
    """Any credential-named variable given a value on this line, minus the ones
    that are plainly not a pasted key."""
    m = _SECRET_ASSIGNMENT_RE.match(line)
    if not m:
        return []
    value = m.group(2)
    if _NOT_A_LITERAL_RE.match(value) or _is_fixture(value):
        return []
    return [f"{path}: {m.group(1)} = <redacted>"]


def test_no_tracked_file_assigns_a_credential_a_value():
    """Catches the key shapes the scans above cannot recognise.

    config.py's own `S2_API_KEY = ""` passes, because an empty slot is the shipped
    state. Filling it in is what fails -- which is the entire point, since filling
    it in is what the file's comment used to tell you to do.
    """
    leaks = []
    for path, lineno, line in _tracked_lines():
        leaks.extend(f"{path}:{lineno}" + leak[len(path):]
                     for leak in _assigned_secrets(path, line))
    assert not leaks, _REVOKE + "\n  ".join(leaks)


@pytest.mark.parametrize("line", [
    'S2_API_KEY = "aBcD1234eFgH5678iJkL9012mNoP3456qRsT"',
    "S2_API_KEY='aBcD1234eFgH5678iJkL9012mNoP3456qRsT'",
    'export GITHUB_TOKEN=aBcD1234eFgH5678iJkL9012mNoP3456',
    'my_api_key = "aBcD1234eFgH5678iJkL9012mNoP3456qRsT"',
])
def test_the_assignment_scan_would_actually_catch_one(line):
    assert _assigned_secrets("f.py", line), f"missed a pasted key: {line}"


@pytest.mark.parametrize("line", [
    'S2_API_KEY = ""',                       # the shipped slot
    "S2_API_KEY = ''",
    'S2_API_KEY = "YOUR_KEY_HERE"',          # documentation
    'S2_API_KEY = os.environ["S2_API_KEY"]',  # read, not pasted
    '    key = getattr(config, "S2_API_KEY", "") or ""',
    'S2_API_KEY: str = ""',
    '# set S2_API_KEY = your key from the settings page',
    'if not config.S2_API_KEY:',
])
def test_the_assignment_scan_does_not_fire_on_ordinary_code(line):
    assert not _assigned_secrets("f.py", line), f"false positive on: {line}"


def _url(secret):
    # Concatenated, never a literal: this file is scanned too, and a fixture
    # spelled out in full would make the scanner report itself.
    return "https://git:" + secret + "@git.overleaf.com/abc"


def test_the_scan_would_actually_catch_one():
    """The guard above is only reassuring if it can fail. Prove that it can."""
    m = _URL_CREDENTIAL_RE.search("url = " + _url("olp" + "_A1b2C3d4E5f6G7h8I9j0"))
    assert m is not None
    assert not _is_fixture(m.group(2))


@pytest.mark.parametrize("placeholder", [
    "YOUR_TOKEN", "PASTE_TOKEN_HERE", "TOKEN", "<your-token>", "xxxx",
])
def test_documentation_placeholders_are_not_leaks(placeholder):
    m = _URL_CREDENTIAL_RE.search(_url(placeholder))
    assert m is not None and _is_fixture(m.group(2))


@pytest.mark.parametrize("fake", ["olp_tok", "olp_stored", "hunter2"])
def test_a_short_test_double_is_not_a_leak(fake):
    """The tests for the credential code use lowercase fakes, which read better
    than shouting and are too short to be anything a real service issues."""
    assert _is_fixture(fake)


def test_the_length_floor_does_not_hide_a_real_token_in_a_url():
    """The floor is only safe because the bare-token scan has no floor. If that
    ever changes, a token inside a URL becomes invisible to both scans."""
    real_shape = "olp" + "_A1b2C3d4E5f6G7h8I9j0K1l2"
    assert not _is_fixture(real_shape)
    assert _BARE_TOKEN_RE.search(_url(real_shape))


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
