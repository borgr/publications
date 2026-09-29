"""The CI alert for a local weekly run that stopped happening."""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_last_run

from pipeline_state import PipelineState

DAY = 86400


def state_fetched(tmp_path, days_ago):
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("fetch", [])
    state.steps["fetch"]["completed_epoch"] = int(time.time() - days_ago * DAY)
    state.save()
    return str(tmp_path / "state.json")


def test_a_recent_fetch_passes(tmp_path):
    ok, message = check_last_run.check(PipelineState.load(state_fetched(tmp_path, 3)), 31)
    assert ok
    assert "3.0 days ago" in message


def test_a_fetch_older_than_the_limit_fails_and_says_what_to_do(tmp_path):
    ok, message = check_last_run.check(PipelineState.load(state_fetched(tmp_path, 40)), 31)
    assert not ok
    assert "40 days ago" in message
    assert "update.py" in message


def test_no_recorded_fetch_passes(tmp_path):
    """A fork, or state reset by a VERSION bump: nothing has stopped yet."""
    ok, _ = check_last_run.check(PipelineState.load(str(tmp_path / "missing.json")), 31)
    assert ok


def test_main_exit_code_follows_the_limit(tmp_path, capsys):
    path = state_fetched(tmp_path, 20)
    assert check_last_run.main(["--state", path]) == 0
    assert check_last_run.main(["--state", path, "--max-days", "14"]) == 1
    assert "::error::" in capsys.readouterr().out

