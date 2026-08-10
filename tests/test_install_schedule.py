"""install_schedule.py: the only thing that makes this pipeline keep running.

Everything else in the repo answers "is the output right?". This script answers
"does anything produce output at all next Monday?", and it is the one step CI
cannot cover -- hosted runners get a CAPTCHA from Scholar, so the weekly fetch
lives on a laptop and is installed by exactly this file.

Its failures are all of the same shape: something looks installed and nothing
runs. A wrong interpreter, a PATH without git, an hour launchd will not schedule,
or a `launchctl load` that exits 0 having done nothing -- none of them raise
anything, and the symptom is a CV that quietly stops moving for a month. So the
tests are about the plist's contents and about not trusting the exit status.

Nothing here touches ~/Library/LaunchAgents or the real launchctl.
"""

import os
import plistlib
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import install_schedule


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A darwin install that writes into tmp_path and records launchctl calls.

    darwin is forced because the install path is macOS-only and CI runs on Linux,
    where it would take the --show branch and test nothing.
    """
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = stderr = ""
        return Result()

    monkeypatch.setattr(install_schedule.sys, "platform", "darwin")
    monkeypatch.setattr(install_schedule.subprocess, "run", fake_run)
    monkeypatch.setattr(install_schedule, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(install_schedule, "PLIST_PATH",
                        str(tmp_path / f"{install_schedule.LABEL}.plist"))
    monkeypatch.setattr(install_schedule, "LOG_DIR", str(tmp_path / "logs"))
    return calls


def _run(*argv) -> int:
    return install_schedule.main(list(argv))


def _installed_plist():
    with open(install_schedule.PLIST_PATH, "rb") as f:
        return plistlib.load(f)


# ── what the job will actually run ───────────────────────────────────────────

def test_the_job_runs_this_checkout_with_this_interpreter():
    """Resolved, not written down: a fork with its own virtualenv and its own
    directory has to work without editing this file, and a hardcoded
    /usr/bin/python3 would run the pipeline against the wrong site-packages."""
    plist = install_schedule.build_plist(1, 8, 37)
    assert plist["ProgramArguments"] == [sys.executable,
                                         os.path.join(install_schedule.ROOT, "update.py")]
    assert plist["WorkingDirectory"] == install_schedule.ROOT


def test_the_schedule_is_what_was_asked_for():
    plist = install_schedule.build_plist(3, 9, 47)
    assert plist["StartCalendarInterval"] == {"Weekday": 3, "Hour": 9, "Minute": 47}


def test_the_job_can_find_git_and_curl():
    """launchd starts jobs with a minimal PATH. Every HTTP fetch in the pipeline
    shells out to curl and step 7 shells out to git, so a PATH without Homebrew's
    prefixes gives a run that fails at preflight every week."""
    path = install_schedule.build_plist(1, 8, 37)["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path and "/usr/local/bin" in path


def test_the_output_is_logged_somewhere_a_human_can_read():
    """The run is unattended, so its stdout is the only account of what happened;
    a notification says a run failed but not which lookup or which push."""
    plist = install_schedule.build_plist(1, 8, 37)
    assert plist["StandardOutPath"] and plist["StandardErrorPath"]
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]


def test_installing_does_not_start_a_scrape():
    """RunAtLoad would fetch Scholar on install and again on every login, which is
    both rude to Scholar and a good way to earn a CAPTCHA on the day you set it
    up. `launchctl start` is the deliberate way to ask."""
    assert install_schedule.build_plist(1, 8, 37)["RunAtLoad"] is False


def test_the_cron_line_names_this_checkout():
    """The Linux path is copy-pasted by hand, so it has to be complete."""
    line = install_schedule.cron_equivalent(1, 8, 37)
    assert line.startswith("37 8 * * 1")
    assert install_schedule.ROOT in line and sys.executable in line


# ── a schedule launchd will not run ──────────────────────────────────────────
#
# launchd accepts Hour 25 and then never fires the job. Nothing raises, and the
# summary this script prints would have said "Runs every Monday at 25:37".

@pytest.mark.parametrize("day, hour, minute", [
    (8, 8, 37),     # 0-7; 7 is Sunday, 8 is nothing
    (1, 24, 37),
    (1, -1, 37),
    (1, 8, 60),
])
def test_a_schedule_launchd_cannot_run_is_refused(install, day, hour, minute):
    code = _run("--day", str(day), "--hour", str(hour), "--minute", str(minute))
    assert code == 1
    assert not os.path.exists(install_schedule.PLIST_PATH), "installed anyway"


@pytest.mark.parametrize("day", [0, 7])
def test_both_spellings_of_sunday_are_accepted(day):
    assert install_schedule.out_of_range(day, 8, 37) == []


def test_the_message_says_which_value_and_what_the_range_is(install, capsys):
    _run("--hour", "99")
    assert "--hour must be 0-23, not 99" in capsys.readouterr().err


# ── installed means launchd lists it ─────────────────────────────────────────

def test_a_successful_install_writes_a_plist_launchd_can_read(install):
    assert _run() == 0
    plist = _installed_plist()
    assert plist["Label"] == install_schedule.LABEL
    assert ["launchctl", "load", install_schedule.PLIST_PATH] in install


def test_load_reporting_success_is_not_taken_as_installed(install, monkeypatch):
    """`launchctl load` is deprecated and exits 0 in cases where it has done
    nothing -- the plist rejected, or the label already loaded from an older
    checkout. Believing it is how you get a schedule that exists on disk and
    nowhere in launchd, which is indistinguishable from a working one until the
    CV has been stale for a month."""
    monkeypatch.setattr(install_schedule, "is_registered", lambda label=None: False)
    assert _run() == 1


@pytest.mark.parametrize("returncode, registered", [(0, True), (1, False), (113, False)])
def test_registration_is_read_from_launchctl(monkeypatch, returncode, registered):
    """The whole point of the check is that it is not an assumption, so it has to
    be the one thing here that really asks."""
    seen = []

    def fake_run(cmd, *a, **k):
        seen.append(cmd)
        class Result:
            pass
        Result.returncode = returncode
        return Result()

    monkeypatch.setattr(install_schedule.subprocess, "run", fake_run)
    assert install_schedule.is_registered("com.example.job") is registered
    assert seen == [["launchctl", "list", "com.example.job"]]


def test_a_refused_load_is_reported_with_launchctl_s_own_reason(install, monkeypatch, capsys):
    """launchctl's message is the only diagnosis available -- "Load failed: 5:
    Input/output error" means something specific to whoever has to fix it."""
    def refuse(cmd, *a, **k):
        class Result:
            returncode = 1 if cmd[1] == "load" else 0
            stdout = ""
            stderr = "Load failed: 5: Input/output error"
        return Result()
    monkeypatch.setattr(install_schedule.subprocess, "run", refuse)
    assert _run() == 1
    assert "Load failed: 5" in capsys.readouterr().err


def test_the_failure_says_how_to_load_it_by_hand(install, monkeypatch, capsys):
    monkeypatch.setattr(install_schedule, "is_registered", lambda label=None: False)
    _run()
    assert "launchctl bootstrap" in capsys.readouterr().err


def test_a_registered_job_is_reported_as_installed(install, capsys):
    _run("--day", "3", "--hour", "9", "--minute", "47")
    out = capsys.readouterr().out
    assert "Wednesday at 09:47" in out
    assert install_schedule.PLIST_PATH in out


def test_reinstalling_unloads_the_previous_job_first(install):
    """Two loads of one label leave launchd with the older plist still in force,
    so changing the hour silently keeps the old one."""
    _run()
    install.clear()
    _run()
    assert ["launchctl", "unload", install_schedule.PLIST_PATH] in install
    assert install.index(["launchctl", "unload", install_schedule.PLIST_PATH]) \
        < install.index(["launchctl", "load", install_schedule.PLIST_PATH])


# ── --show, and the platforms that only get the text ─────────────────────────

def test_show_installs_nothing(install, capsys):
    assert _run("--show") == 0
    assert not os.path.exists(install_schedule.PLIST_PATH)
    assert install == [], f"--show called launchctl: {install}"
    assert "cron equivalent" in capsys.readouterr().out


def test_off_macos_the_equivalents_are_printed_instead(install, monkeypatch, capsys):
    monkeypatch.setattr(install_schedule.sys, "platform", "linux")
    assert _run() == 0
    out = capsys.readouterr().out
    assert "Not macOS" in out and "cron equivalent" in out
    assert not os.path.exists(install_schedule.PLIST_PATH)


def test_the_printed_plist_is_the_one_that_would_be_installed(install, capsys):
    """Otherwise --show is a description of something else, and the Linux user
    copying it out gets a schedule that was never tested."""
    _run("--show", "--day", "5", "--hour", "6", "--minute", "7")
    printed = plistlib.loads(capsys.readouterr().out.split("cron equivalent")[0]
                             .encode())
    assert printed == install_schedule.build_plist(5, 6, 7)


# ── uninstall ────────────────────────────────────────────────────────────────

def test_uninstall_unloads_and_removes(install):
    _run()
    assert os.path.exists(install_schedule.PLIST_PATH)
    assert _run("--uninstall") == 0
    assert not os.path.exists(install_schedule.PLIST_PATH)
    assert ["launchctl", "unload", install_schedule.PLIST_PATH] in install


def test_uninstalling_what_was_never_installed_is_not_a_failure(install, capsys):
    """It runs in teardown paths and in init_new_author's neighbourhood; exiting
    non-zero for "already absent" makes a clean-up script fail on success."""
    assert _run("--uninstall") == 0
    assert "Nothing installed" in capsys.readouterr().out
