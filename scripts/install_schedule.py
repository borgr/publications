#!/usr/bin/env python3
"""Install (or remove) a weekly local run of the publications pipeline.

The Scholar fetch has to happen from a residential IP -- hosted CI runners get a
CAPTCHA -- so the scheduled job lives on your own machine. GitHub Actions covers
the parts that do not need Scholar; see .github/workflows/ci.yml.

    python scripts/install_schedule.py            # install, weekly
    python scripts/install_schedule.py --show     # print the plist, install nothing
    python scripts/install_schedule.py --uninstall
    python scripts/install_schedule.py --day 3 --hour 9 --minute 47

Paths are resolved from this checkout and the running interpreter, so a fork
needs no editing. Failures surface as a macOS notification (see notify.py) and
in the log file, and the run exits non-zero.

On Linux, use cron or a systemd timer instead; the equivalent line is printed
by --show.
"""

import argparse
import os
import plistlib
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = "com.publications.update"
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
PLIST_PATH = os.path.join(AGENTS_DIR, f"{LABEL}.plist")
LOG_DIR = os.path.expanduser("~/Library/Logs")


def build_plist(day, hour, minute):
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, os.path.join(ROOT, "update.py")],
        "WorkingDirectory": ROOT,
        "StartCalendarInterval": {"Weekday": day, "Hour": hour, "Minute": minute},
        # Run at the next opportunity if the machine was asleep at the scheduled
        # time. Without this a laptop that is shut on Mondays never updates.
        "RunAtLoad": False,
        "StandardOutPath": os.path.join(LOG_DIR, f"{LABEL}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"{LABEL}.err"),
        # launchd starts jobs with a minimal PATH; git and curl both need to be
        # findable, and Homebrew's prefixes are not on the default PATH.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def cron_equivalent(day, hour, minute):
    return (f"{minute} {hour} * * {day}  cd {ROOT} && {sys.executable} update.py "
            f">> ~/publications-update.log 2>&1")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", type=int, default=1, metavar="0-6",
                        help="Day of week, 0=Sunday (default: 1, Monday)")
    parser.add_argument("--hour", type=int, default=8)
    parser.add_argument("--minute", type=int, default=37,
                        help="Default 37 rather than 0, to avoid the on-the-hour "
                             "crowd hitting Scholar at once")
    parser.add_argument("--show", action="store_true", help="Print, do not install")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    if args.uninstall:
        if not os.path.exists(PLIST_PATH):
            print(f"Nothing installed at {PLIST_PATH}")
            return 0
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
        os.unlink(PLIST_PATH)
        print(f"Removed {PLIST_PATH}")
        return 0

    plist = build_plist(args.day, args.hour, args.minute)

    if args.show or sys.platform != "darwin":
        if sys.platform != "darwin":
            print(f"Not macOS ({sys.platform}); showing the equivalents instead.\n")
        print(plistlib.dumps(plist).decode())
        print("cron equivalent:\n  " + cron_equivalent(args.day, args.hour, args.minute))
        return 0

    os.makedirs(AGENTS_DIR, exist_ok=True)
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    load = subprocess.run(["launchctl", "load", PLIST_PATH],
                          capture_output=True, text=True)
    if load.returncode != 0:
        print(f"launchctl load failed: {load.stderr.strip()}", file=sys.stderr)
        return 1

    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    print(f"Installed {PLIST_PATH}")
    print(f"  Runs every {days[args.day % 7]} at {args.hour:02d}:{args.minute:02d}")
    print(f"  Log:  {plist['StandardOutPath']}")
    print(f"  Test it now:  launchctl start {LABEL}")
    print("  Remove:       python scripts/install_schedule.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
