"""Tests for update.py's orchestration: what runs, what is skipped, what is recorded.

The individual steps have their own tests. What this file covers is main()'s
decision-making, which is what runs unattended every week and which had almost no
coverage -- and where the failures are the expensive kind, because they are quiet.
A step wrongly auto-skipped, or a dry run that records success, does not raise
anything: the CV simply stops tracking reality while every run reports success.

Every step is replaced by a recorder, so nothing here touches the network, git,
or the real data files.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update
from pipeline_state import PipelineState

_STEPS = {
    "step1_fetch":          None,
    "step2_add_new_papers": 0,
    "step2b_enrich":        0,
    "step3_resolve":        (0, 0, 0, []),
    "step4_build_bib":      "cats-sentinel",
    "step5_rebuild_tex":    None,
    "step6_worklist":       None,
    "step7_push":           True,
}


class Harness:
    def __init__(self, calls, state_path, notified, inputs):
        self.calls = calls
        self.state_path = state_path
        self.notified = notified
        self.inputs = inputs

    def state(self):
        return PipelineState.load(self.state_path)

    def ran(self, step):
        return step in self.calls


@pytest.fixture
def h(tmp_path, monkeypatch):
    calls, notified = [], []

    monkeypatch.setattr(update, "preflight", lambda: [])
    # The real one reaches Overleaf over the network. Two tests below override it.
    monkeypatch.setattr(update, "check_push_credential", lambda enabled: None)

    for name, result in _STEPS.items():
        def recorder(*args, _n=name, _r=result, **kwargs):
            calls.append(_n)
            return _r
        monkeypatch.setattr(update, name, recorder)

    # Each step's declared inputs, redirected into the tmp dir so marking a step
    # done and then re-running is a real content comparison, not a stub.
    inputs = {}
    for step, paths in update.STEP_INPUTS.items():
        redirected = []
        for path in paths:
            p = tmp_path / os.path.basename(path)
            p.write_text(f"contents of {p.name}\n")
            redirected.append(str(p))
        inputs[step] = redirected
    monkeypatch.setattr(update, "STEP_INPUTS", inputs)

    state_path = str(tmp_path / "state.json")
    # Delegates to the real load, only with a redirected path: constructing a
    # PipelineState directly would skip reading the file, and every test about
    # skipping and recording depends on the round trip through disk.
    real_load = PipelineState.load.__func__
    monkeypatch.setattr(update.PipelineState, "load",
                        classmethod(lambda cls, path=state_path: real_load(cls, path)))

    def fake_failure(*args, **kwargs):
        # `enabled` is how --no-notify is plumbed through; ignoring it here made
        # the test assert the opposite of what the flag does.
        if kwargs.get("enabled", True):
            notified.append(args[0] if args else "")
    monkeypatch.setattr(update.notify, "failure", fake_failure)
    monkeypatch.setattr(update, "CITATIONS_CSV", str(tmp_path / "citations.csv"))
    (tmp_path / "citations.csv").write_text("title,citations\n")

    return Harness(calls, state_path, notified, inputs)


# --- what a dry run must not do -----------------------------------------

def test_a_dry_run_records_no_progress(h):
    """Otherwise the next real run auto-skips work that never happened."""
    update.main(["--dry-run"])
    assert h.ran("step4_build_bib")
    assert h.state().steps == {}, "a dry run marked steps as completed"


def test_a_dry_run_writes_no_state_file(h):
    update.main(["--dry-run"])
    assert not os.path.exists(h.state_path)


def test_a_real_run_records_every_step_it_ran(h):
    update.main([])
    recorded = set(h.state().steps)
    assert {"fetch", "resolve", "build_bib", "rebuild_tex"} <= recorded


# --- auto-skipping ------------------------------------------------------

def test_unchanged_inputs_auto_skip_the_step(h):
    state = h.state()
    state.mark_done("resolve", h.inputs["resolve"])
    state.save()
    update.main([])
    assert not h.ran("step3_resolve")


def test_force_overrides_an_auto_skip(h):
    state = h.state()
    state.mark_done("resolve", h.inputs["resolve"])
    state.save()
    update.main(["--force"])
    assert h.ran("step3_resolve")


def test_a_changed_input_un_skips_the_step(h):
    state = h.state()
    state.mark_done("resolve", h.inputs["resolve"])
    state.save()
    with open(h.inputs["resolve"][0], "a") as f:
        f.write("a new paper\n")
    update.main([])
    assert h.ran("step3_resolve")


@pytest.mark.parametrize("flag, step", [
    ("--skip-fetch",        "step1_fetch"),
    ("--skip-new",          "step2_add_new_papers"),
    ("--skip-resolve",      "step3_resolve"),
    ("--skip-publications", "step4_build_bib"),
    ("--skip-tex",          "step5_rebuild_tex"),
    ("--no-push",           "step7_push"),
])
def test_each_skip_flag_skips_its_own_step(h, flag, step):
    update.main([flag])
    assert not h.ran(step)


def test_a_skip_flag_skips_nothing_else(h):
    update.main(["--skip-resolve"])
    assert h.ran("step4_build_bib") and h.ran("step5_rebuild_tex")


# --- ordering ----------------------------------------------------------

def test_the_steps_run_in_pipeline_order(h):
    """Building before resolving would publish the previous run's bibliography."""
    update.main([])
    order = [c for c in h.calls if c in
             ("step1_fetch", "step3_resolve", "step4_build_bib",
              "step5_rebuild_tex", "step6_worklist", "step7_push")]
    assert order == sorted(order, key=lambda s: [
        "step1_fetch", "step3_resolve", "step4_build_bib",
        "step5_rebuild_tex", "step6_worklist", "step7_push"].index(s))


# --- failure must not be quiet ------------------------------------------

def test_a_failed_push_exits_nonzero(h, monkeypatch):
    monkeypatch.setattr(update, "step7_push", lambda dry: False)
    with pytest.raises(SystemExit) as exc:
        update.main([])
    assert exc.value.code == 1


def test_a_failed_push_notifies(h, monkeypatch):
    monkeypatch.setattr(update, "step7_push", lambda dry: False)
    with pytest.raises(SystemExit):
        update.main([])
    assert h.notified, "a push failure produced no notification"


def test_no_notify_silences_the_notification_but_not_the_exit_code(h, monkeypatch):
    monkeypatch.setattr(update, "step7_push", lambda dry: False)
    with pytest.raises(SystemExit) as exc:
        update.main(["--no-notify"])
    assert exc.value.code == 1 and not h.notified


def test_a_preflight_problem_stops_before_any_step(h, monkeypatch):
    monkeypatch.setattr(update, "preflight",
                        lambda: ["orig.bib is missing"])
    with pytest.raises(SystemExit) as exc:
        update.main([])
    assert exc.value.code == 1
    assert h.calls == [], f"steps ran despite a preflight problem: {h.calls}"
    assert h.notified


@pytest.mark.parametrize("argv, wants_credential", [
    ([], True),
    (["--no-push"], False),
    (["--dry-run"], False),
    (["--dry-run", "--no-push"], False),
])
def test_the_push_credential_is_checked_only_when_there_will_be_a_push(
        h, monkeypatch, argv, wants_credential):
    """The check costs a network round trip and reports a problem that does not
    apply to a run which never pushes."""
    asked = []
    monkeypatch.setattr(update, "check_push_credential",
                        lambda enabled: asked.append(enabled) or None)
    update.main(argv + ["--no-notify"])
    assert asked == [wants_credential]


def test_a_credential_problem_warns_but_still_runs_every_step(h, monkeypatch):
    """The reason the check moved out of preflight: a rotated token used to stop
    the fetch, the resolve and the rebuild as well as the push."""
    monkeypatch.setattr(update, "check_push_credential",
                        lambda enabled: "Cannot authenticate to Overleaf")
    monkeypatch.setattr(update, "step7_push", lambda dry: False)
    with pytest.raises(SystemExit) as exc:
        update.main(["--no-notify"])
    assert exc.value.code == 1, "a run that could not publish must not exit 0"
    assert h.ran("step1_fetch") and h.ran("step4_build_bib")


def test_a_step_that_raises_leaves_the_step_unrecorded(h, monkeypatch):
    """So the next run retries it instead of auto-skipping a step that failed."""
    def boom(*a, **k):
        raise RuntimeError("DBLP is down")
    monkeypatch.setattr(update, "step3_resolve", boom)
    with pytest.raises(RuntimeError):
        update.main([])
    assert "resolve" not in h.state().steps


def test_skipping_the_push_still_exits_zero(h):
    update.main(["--no-push"])
    assert not h.notified


# --- state file integrity ----------------------------------------------

def test_the_state_file_stays_valid_json(h):
    update.main([])
    with open(h.state_path) as f:
        json.load(f)
