"""notify.py: the only thing that tells a human an unattended run broke.

The contract worth testing is narrow but absolute. failure() runs at the point
where something has already gone wrong, from a launchd job with no terminal
attached, and it must never raise -- a notifier that throws turns a diagnosable
failure into a traceback about the notifier. So every test here is either "the
message got out" or "a broken channel is survivable".
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify


@pytest.fixture(autouse=True)
def no_ambient_channels(monkeypatch):
    """Neither channel may reach the real machine.

    Without this, running the suite on the author's own laptop posts to
    Notification Center, and running it inside CI prints ::error annotations
    that mark a green run as failed.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(notify.sys, "platform", "linux")


# --- the message always reaches stderr ----------------------------------

def test_the_failure_is_printed_even_with_every_channel_off(capsys):
    notify.failure("step 3 failed", "HTTP 503 from DBLP", enabled=False)
    err = capsys.readouterr().err
    assert "FAILED: step 3 failed" in err
    assert "HTTP 503 from DBLP" in err


def test_stderr_not_stdout(capsys):
    """update.py's own output goes to stdout; a failure has to be separable from
    it, both for a log reader and for `python update.py > log`."""
    notify.failure("broke")
    out, err = capsys.readouterr()
    assert out == "" and "broke" in err


def test_a_bare_summary_needs_no_detail(capsys):
    notify.failure("broke")
    assert "FAILED: broke" in capsys.readouterr().err


def test_failure_returns_none_so_it_cannot_be_mistaken_for_a_status():
    assert notify.failure("broke") is None


# --- the GitHub Actions channel ----------------------------------------

def test_an_annotation_is_emitted_under_actions(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    notify.failure("step 3 failed", "detail here")
    out = capsys.readouterr().out
    assert out.startswith("::error title=publications pipeline::")
    assert "step 3 failed" in out and "detail here" in out


def test_the_annotation_is_one_line(monkeypatch, capsys):
    """A newline would end the annotation, so everything after the first line
    would be printed as plain log text and lost from the run summary."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    notify.failure("summary", "line one\nline two\nline three")
    annotation = [ln for ln in capsys.readouterr().out.splitlines()
                  if ln.startswith("::error")]
    assert len(annotation) == 1
    assert "line one line two line three" in annotation[0]


def test_a_percent_sign_is_escaped(monkeypatch, capsys):
    """Actions decodes %XX in annotations, so a literal `%` in a message can eat
    the two characters after it -- or produce mojibake from a valid pair."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    notify.failure("citations dropped by 40% (2A3B)")
    out = capsys.readouterr().out
    assert "40%25 (2A3B)" in out


def test_a_very_long_message_is_truncated(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    notify.failure("x" * 50, "y" * 5000)
    annotation = next(ln for ln in capsys.readouterr().out.splitlines()
                      if ln.startswith("::error"))
    body = annotation.split("::", 2)[2]
    assert len(body) == notify._MAX_BODY


def test_no_annotation_outside_actions(capsys):
    notify.failure("broke")
    assert "::error" not in capsys.readouterr().out


def test_the_annotation_channel_reports_whether_it_fired(monkeypatch):
    assert notify._github_actions_annotation("m") is False
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert notify._github_actions_annotation("m") is True


# --- the macOS channel --------------------------------------------------

def test_notification_center_is_used_on_macos(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    assert notify._macos_notification("Title", "Message") is True
    assert calls[0][:2] == ["osascript", "-e"]
    assert 'display notification "Message" with title "Title"' == calls[0][2]


def test_quotes_and_backslashes_in_the_message_are_escaped(monkeypatch):
    """Unescaped, a quote closes the AppleScript string literal and osascript
    fails to compile -- so the one run that most needed the notification is the
    one that does not get it, since paths and titles are what carry quotes."""
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    notify._macos_notification('a "quoted" title', r'C:\path "x"')
    script = calls[0][2]
    assert r'\"quoted\"' in script
    assert r'C:\\path \"x\"' in script


def test_no_osascript_call_off_macos(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran osascript off macOS"))
    assert notify._macos_notification("T", "M") is False


def test_a_mac_without_osascript_is_not_an_error(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)
    assert notify._macos_notification("T", "M") is False


@pytest.mark.parametrize("boom", [
    OSError("no such binary"),
    subprocess.SubprocessError("died"),
    subprocess.TimeoutExpired("osascript", 10),
])
def test_a_broken_notifier_does_not_raise(monkeypatch, boom):
    """failure() is called from inside an except block. If a channel raises, the
    original failure is replaced by this one in the traceback."""
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")

    def explode(*_a, **_k):
        raise boom
    monkeypatch.setattr(notify.subprocess, "run", explode)
    assert notify._macos_notification("T", "M") is False
    assert notify.failure("broke") is None


def test_both_channels_fire_together(monkeypatch, capsys):
    """They are not alternatives: a scheduled Action and a local launchd run use
    different ones, and neither knows which it is."""
    posted = []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: posted.append(cmd))
    notify.failure("broke")
    assert "::error" in capsys.readouterr().out
    assert len(posted) == 1


def test_enabled_false_silences_the_channels_but_not_stderr(monkeypatch, capsys):
    """--no-notify has to stay usable for a person watching the terminal, who
    still needs to see what failed."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda *a, **k: pytest.fail("notified with enabled=False"))
    notify.failure("broke", "detail", enabled=False)
    out, err = capsys.readouterr()
    assert "::error" not in out
    assert "broke" in err and "detail" in err
