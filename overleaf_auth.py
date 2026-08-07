"""Authenticate the push to Overleaf without writing the token into a file.

Overleaf gives you one URL that carries its own credential:

    https://git:olp_TOKEN@git.overleaf.com/PROJECT_ID

CI passes that URL through the environment from a repository secret, so it never
lands on disk. A local run cannot do the same, because the run is unattended --
there is nobody there to export a variable at 08:37 on a Monday. The two obvious
places to put it instead are both wrong: the submodule's remote URL and
`.git/config` are untracked but plaintext, and a dotfile in the working tree is
one `git add -A` away from a public repo.

So the token goes in the operating system's credential store, via git's own
`credential approve`. `git push origin` then finds it through the configured
helper with no URL rewriting, nothing in `argv` for `ps` to show, and nothing to
redact from a log. `scripts/install_overleaf_credential.py` is the one-time setup;
everything here is what the pipeline uses afterwards.

The other half of the problem is that a *missing* credential must not hang. Git's
default is to ask, and under launchd there is no terminal to ask on -- so git
either blocks forever holding the run lock, or pops a GUI dialog behind whatever
you are doing. `noninteractive_env` turns asking into failing, and
`check_credential` does the failing early, before a Scholar fetch that takes
minutes.
"""

import os
import re
import shutil
import subprocess
from urllib.parse import urlsplit

ENV_VAR = "OVERLEAF_GIT_URL"

_CREDENTIAL_IN_URL = re.compile(r'//[^/@\s]*@')


def redact(text):
    """Blank out any `user:token@` in a URL before the text is printed.

    Git echoes the remote URL in most of its error messages, and a remote whose
    URL was set with the token in it -- the arrangement this module exists to
    replace, but which someone may already have -- would otherwise put that token
    in a log, a notification, or CI output.
    """
    return _CREDENTIAL_IN_URL.sub("//***@", str(text))


def noninteractive_env(env=None):
    """Return an environment in which git can never stop to ask for a password.

    Two independent prompts have to be closed off. `GIT_TERMINAL_PROMPT=0` covers
    the terminal one; `GIT_ASKPASS` covers the GUI helper, which on macOS is
    configured by default and would otherwise put a dialog on screen during an
    unattended run. Pointing it at `false` -- a program whose entire behaviour is
    to exit non-zero -- makes the lookup fail instantly instead.
    """
    env = dict(os.environ if env is None else env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    false = shutil.which("false") or "/usr/bin/false"
    env["GIT_ASKPASS"] = false
    env["SSH_ASKPASS"] = false
    return env


def url_from_env():
    """The Overleaf URL if it was exported, else "".

    Present so a local run can be driven the same way CI is -- useful for a
    one-off `OVERLEAF_GIT_URL=... python update.py` while setting things up. It is
    read, never stored and never printed.
    """
    return (os.environ.get(ENV_VAR) or "").strip()


# Outside the working tree, deliberately. A file to hand the URL over in has to
# live somewhere that cannot be committed by accident, and anything inside the
# repository can be: step 7 runs `git add -A`, and a .gitignore entry protects
# only the exact path whoever wrote it remembered to list.
URL_FILE = os.path.expanduser("~/.config/publications/overleaf_git_url")


def url_from_file(path=None):
    """The URL from the hand-over file, or "" if there is none.

    For the case where the person with the token and the person running the
    installer are not the same, or not at the same keyboard: write the file, then
    run the installer, which moves the token into the credential store and deletes
    the file. Nothing in between has to read the value or type it at a prompt.
    """
    try:
        with open(path or URL_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def is_inside(path, directory):
    """True if `path` is under `directory`.

    So the installer can refuse a hand-over file inside the repository rather than
    read it: a token there is one `git add -A` from a public remote.
    """
    path = os.path.realpath(os.path.expanduser(path))
    directory = os.path.realpath(directory)
    return path == directory or path.startswith(directory + os.sep)


def split_url(url):
    """Split an Overleaf git URL into the parts a credential helper is keyed on.

    Returns (protocol, host, username, password, path). Raises ValueError when the
    URL carries no password, which is the mistake worth catching: Overleaf's menu
    shows the project URL and the token in two different places, so the URL copied
    from the first alone looks complete and authenticates as nobody.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ValueError("not a URL: expected https://git:TOKEN@host/PROJECT_ID")
    if not parts.password:
        raise ValueError(
            f"the URL has no token in it. Overleaf shows the project URL and the "
            f"git token separately, and both are needed: "
            f"https://git:TOKEN@{parts.hostname}{parts.path}")
    return (parts.scheme, parts.hostname, parts.username or "git",
            parts.password, parts.path)


def store_credential(url, repo_dir=None):
    """Hand the token to git's configured credential helper.

    `git credential approve` reads the fields on stdin, so the token is written to
    a pipe rather than a command line or a file. Which store it ends up in is
    whatever `credential.helper` says -- osxkeychain on macOS, libsecret or a
    plaintext store elsewhere -- and that is deliberately git's decision, not
    ours: it is where git will look for it again.
    """
    protocol, host, username, password, _path = split_url(url)
    helper = subprocess.run(
        ["git"] + (["-C", repo_dir] if repo_dir else []) + ["config", "credential.helper"],
        capture_output=True, text=True).stdout.strip()
    if not helper:
        raise ValueError(
            "git has no credential.helper configured, so there is nowhere to put "
            "the token. On macOS: git config --global credential.helper osxkeychain")

    payload = (f"protocol={protocol}\nhost={host}\nusername={username}\n"
               f"password={password}\n\n")
    result = subprocess.run(
        ["git"] + (["-C", repo_dir] if repo_dir else []) + ["credential", "approve"],
        input=payload, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"git credential approve failed: {result.stderr.strip()[:200]}")
    return helper


def check_credential(repo_dir):
    """Return a problem description if the Overleaf remote cannot be reached.

    `ls-remote` rather than an inspection of the credential store: what matters is
    whether git can authenticate, and only git knows which of several helpers it
    would consult, in what order, under which URL. A revoked token is
    indistinguishable from a missing one by inspection, and identical here.

    Costs one network round trip, which is worth spending -- the alternative is
    discovering it after a Scholar fetch and six steps of work.
    """
    if not os.path.isdir(os.path.join(repo_dir, ".git")) and not os.path.exists(
            os.path.join(repo_dir, ".git")):
        return None  # No submodule at all; check_overleaf_present reports that.

    result = subprocess.run(
        ["git", "-C", repo_dir, "ls-remote", "--exit-code", "origin", "HEAD"],
        capture_output=True, text=True, env=noninteractive_env())
    if result.returncode == 0:
        return None

    stderr = result.stderr.strip().splitlines()
    detail = redact(stderr[-1][:160]) if stderr else f"git exited {result.returncode}"
    return (
        "Cannot authenticate to Overleaf, so step 7 would fail after doing all "
        "the work. Store the token once:\n"
        "      python scripts/install_overleaf_credential.py\n"
        f"    git said: {detail}")
