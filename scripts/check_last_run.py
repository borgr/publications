#!/usr/bin/env python3
"""Fail when the local weekly run has not fetched Scholar for too long.

The run that fetches Scholar and pushes lives on your own machine (see
scripts/install_schedule.py), so every alert it raises -- a notification, a log
line, a non-zero exit -- only exists on that machine. When the machine itself is
gone (powered off, replaced, a laptop left in a drawer), nothing runs, so nothing
fails: the CV just quietly stops being current. That is the one failure the
local run cannot report, and it went unnoticed for two weeks in September 2026.

CI can see it, because the local run commits .pipeline_state.json on every
successful fetch. This reads the fetch step's timestamp from the committed copy
and exits 1 when it is older than --max-days, so the monthly CI job goes red and
GitHub emails whoever owns the schedule.

    python scripts/check_last_run.py               # default: 31 days
    python scripts/check_last_run.py --max-days 14

The fetch step, not the last commit, because a run that reaches Scholar and gets
a CAPTCHA every week still commits and still leaves the table unable to learn of
new papers. A fork, or a clone whose state was reset by a VERSION bump, has no
recorded fetch at all; that passes with a notice, since the alert means "this
used to work and stopped", and there is nothing yet that stopped.
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline_state import STATE_PATH, PipelineState

STEP = "fetch"
DEFAULT_MAX_DAYS = 31


def check(state: PipelineState, max_days: float, now: float | None = None) -> tuple[bool, str]:
    """Return (ok, message) for the age of the last successful fetch."""
    if not state.ever_completed(STEP):
        return True, ("No successful Scholar fetch is recorded in .pipeline_state.json, "
                      "so there is no run to be overdue. Nothing to check.")
    record = state.steps[STEP]
    now = time.time() if now is None else now
    days = (now - record["completed_epoch"]) / 86400
    when = record.get("completed_at", "unknown time")
    if days > max_days:
        return False, (
            f"The local pipeline last fetched Scholar {days:.0f} days ago ({when}), "
            f"more than the {max_days:g}-day limit. The machine running the weekly "
            "job (scripts/install_schedule.py) is off, gone, or failing: run "
            "`python update.py` there, or install the schedule on a machine you use.")
    return True, f"Last Scholar fetch {days:.1f} days ago ({when}); limit is {max_days:g}."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-days", type=float, default=DEFAULT_MAX_DAYS,
                        help=f"Fail when the last fetch is older than this (default {DEFAULT_MAX_DAYS})")
    parser.add_argument("--state", default=STATE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    ok, message = check(PipelineState.load(args.state), args.max_days)
    print(message if ok else f"::error::{message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
