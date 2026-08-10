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

import resolve_arxiv
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
    def __init__(self, calls, state_path, notified, inputs, outputs):
        self.calls = calls
        self.state_path = state_path
        self.notified = notified
        self.inputs = inputs
        self.outputs = outputs

    def state(self):
        return PipelineState.load(self.state_path)

    def ran(self, step):
        return step in self.calls

    def completed(self, step):
        """Record `step` as having run, the way a real run would."""
        state = self.state()
        state.mark_done(step, self.inputs[step], self.outputs[step])
        state.save()


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

    # Each step's declared inputs and outputs, redirected into the tmp dir so
    # marking a step done and then re-running is a real content comparison, not a
    # stub. Redirecting by basename keeps the real relationships: orig.bib is both
    # step 3's output and step 4's input, and both must land on one file.
    def _redirect(declared):
        table = {}
        for step, paths in declared.items():
            redirected = []
            for path in paths:
                p = tmp_path / os.path.basename(path)
                if not p.exists():
                    p.write_text(f"contents of {p.name}\n")
                redirected.append(str(p))
            table[step] = redirected
        return table

    inputs = _redirect(update.STEP_INPUTS)
    outputs = _redirect(update.STEP_OUTPUTS)
    monkeypatch.setattr(update, "STEP_INPUTS", inputs)
    monkeypatch.setattr(update, "STEP_OUTPUTS", outputs)

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

    return Harness(calls, state_path, notified, inputs, outputs)


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
    h.completed("resolve")
    update.main([])
    assert not h.ran("step3_resolve")


def test_force_overrides_an_auto_skip(h):
    h.completed("resolve")
    update.main(["--force"])
    assert h.ran("step3_resolve")


def test_a_changed_input_un_skips_the_step(h):
    h.completed("resolve")
    with open(h.inputs["resolve"][0], "a") as f:
        f.write("a new paper\n")
    update.main([])
    assert h.ran("step3_resolve")


# --- output that went away ----------------------------------------------
#
# The inputs alone cannot see this: reverting the citation totals in main.tex by
# hand left step 5's inputs untouched, so it auto-skipped on every later run and
# the CV stayed wrong until CI compared against Overleaf. Locally, nothing said so.

@pytest.mark.parametrize("step, func", [
    ("resolve",     "step3_resolve"),
    ("build_bib",   "step4_build_bib"),
    ("rebuild_tex", "step5_rebuild_tex"),
])
def test_an_edited_output_un_skips_the_step(h, step, func):
    h.completed(step)
    with open(h.outputs[step][0], "w") as f:
        f.write("someone reverted this by hand\n")
    update.main([])
    assert h.ran(func)


@pytest.mark.parametrize("step, func", [
    ("resolve",     "step3_resolve"),
    ("build_bib",   "step4_build_bib"),
    ("rebuild_tex", "step5_rebuild_tex"),
])
def test_a_deleted_output_un_skips_the_step(h, step, func):
    h.completed(step)
    os.unlink(h.outputs[step][0])
    update.main([])
    assert h.ran(func)


# --- a run that could not finish asking ---------------------------------

def test_a_run_where_a_source_went_silent_does_not_record_step_3_as_done(h, monkeypatch):
    """Otherwise the next run auto-skips, and a paper published last week stays
    cited as a preprint until some unrelated input happens to change."""
    def silent(dry_run):
        resolve_arxiv._note_unanswered("dblp.org")
        return 0, 0, 0, []
    monkeypatch.setattr(update, "step3_resolve", silent)
    update.main([])
    assert "resolve" not in h.state().steps
    # The rest of the run must still happen: the data on disk is unchanged, not
    # broken, and refusing to build the CV over it would be a worse failure.
    assert h.ran("step5_rebuild_tex")
    assert "rebuild_tex" in h.state().steps


def test_a_run_where_every_source_answered_records_step_3_as_done(h):
    update.main([])
    assert "resolve" in h.state().steps


def test_a_step_recorded_before_outputs_were_tracked_runs_once(h):
    """An old .pipeline_state.json has no outputs; it must not be trusted blindly.

    The graceful path for the schema change: the step re-runs once and records
    its outputs, rather than the whole state file being discarded.
    """
    state = h.state()
    state.mark_done("resolve", h.inputs["resolve"])   # no outputs, as before
    state.save()
    update.main([])
    assert h.ran("step3_resolve")
    assert h.state().steps["resolve"]["outputs"], "outputs still not recorded"


def test_renaming_the_author_rebuilds_the_cv(h):
    """Step 5 is where config.AUTHOR_NAME reaches the CV: it writes the name line
    in main.tex and points the bibliography styles' bolding at it. Nothing else
    reads that value, so a rebuild this step skips is a rename that did not
    happen -- and for somebody forking this pipeline, the name on the CV they
    compile is still mine until a paper happens to resolve."""
    h.completed("rebuild_tex")
    config_input = [p for p in h.inputs["rebuild_tex"]
                    if os.path.basename(p) == "config.py"]
    assert config_input, "config.py is not an input to step 5"
    with open(config_input[0], "w") as f:
        f.write('AUTHOR_NAME = "Someone Else"\n')
    update.main(["--no-notify"])
    assert h.ran("step5_rebuild_tex")


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


# ── the Semantic Scholar key, reported rather than assumed ───────────────────
#
# An absent key does not fail, it throttles, and a throttled S2 takes the ACL
# Anthology and OpenReview with it -- both are reached through its externalIds. So
# a run with no key looks like a run with nothing left to resolve, which is a thing
# this pipeline is supposed to be able to say the difference between.

def test_the_run_says_which_key_it_is_using(h, monkeypatch, capsys):
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "s2_api_key_source",
                        lambda: ("k", "the S2_API_KEY environment variable"))
    update.main(["--no-notify"])
    assert "the S2_API_KEY environment variable" in capsys.readouterr().out


def test_a_missing_key_is_reported_with_where_to_put_one(h, monkeypatch, capsys):
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "s2_api_key_source", lambda: ("", ""))
    update.main(["--no-notify"])
    out = capsys.readouterr().out
    assert "no API key" in out and resolve_arxiv.KEY_FILE in out


def test_a_missing_key_does_not_stop_the_run(h, monkeypatch):
    """It is a notice, not a preflight problem: the pipeline works without one."""
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "s2_api_key_source", lambda: ("", ""))
    update.main(["--no-notify"])
    assert h.ran("step3_resolve") and h.ran("step4_build_bib")


def test_the_key_is_never_printed(h, monkeypatch, capsys):
    """The source is the useful half. Printing the key would put a credential in
    ~/Library/Logs on every weekly run, and in the CI log if CI ever runs this."""
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "s2_api_key_source",
                        lambda: ("s3cr3t-key-value", "config.py"))
    update.main(["--no-notify"])
    assert "s3cr3t-key-value" not in capsys.readouterr().out


def test_no_key_notice_when_the_resolve_step_is_skipped(h, monkeypatch, capsys):
    """Nothing asks S2 anything, so the key is irrelevant and saying so is noise."""
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "s2_api_key_source", lambda: ("", ""))
    update.main(["--skip-resolve", "--no-notify"])
    assert "API key" not in capsys.readouterr().out


# ── a fetch that failed is not a run that failed ─────────────────────────────
#
# Scholar is the one source in this pipeline that answers with a CAPTCHA when it
# feels crawled -- it is why CI does not fetch at all -- and fetch_citations.py
# deliberately refuses to overwrite a good citations.csv with a short scrape. Both
# used to end the run at step 1, so the week's resolving, rebuilding and pushing
# were lost to a problem that says nothing about the papers already on disk.

@pytest.fixture
def broken_fetch(h, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Scholar returned a CAPTCHA / unusual-traffic page")
    monkeypatch.setattr(update, "step1_fetch", boom)
    return h


def test_a_failed_fetch_still_runs_the_rest_of_the_pipeline(broken_fetch):
    with pytest.raises(SystemExit):
        update.main(["--no-notify"])
    assert broken_fetch.ran("step3_resolve"), "a CAPTCHA cost the week's resolving"
    assert broken_fetch.ran("step5_rebuild_tex")
    assert broken_fetch.ran("step7_push"), "a CAPTCHA cost the week's publishing"


def test_a_failed_fetch_exits_nonzero(broken_fetch):
    """Working around it is only safe because the run still reports it."""
    with pytest.raises(SystemExit) as exc:
        update.main(["--no-notify"])
    assert exc.value.code == 1


def test_a_failed_fetch_notifies(broken_fetch):
    with pytest.raises(SystemExit):
        update.main([])
    assert broken_fetch.notified, "a failed fetch produced no notification"


def test_a_failed_fetch_is_not_recorded_as_done(broken_fetch):
    """Step 1 is age-based, so recording it would wait out --fetch-age before
    trying again -- a day of not asking, for a CAPTCHA that clears in minutes."""
    with pytest.raises(SystemExit):
        update.main(["--no-notify"])
    assert "fetch" not in broken_fetch.state().steps


def test_a_failed_fetch_with_nothing_on_disk_is_fatal(broken_fetch):
    """There is no previous scrape to fall back on, so there is no run to save:
    every later step would work from an empty table and publish an empty CV."""
    os.unlink(update.CITATIONS_CSV)
    with pytest.raises(RuntimeError):
        update.main(["--no-notify"])
    assert not broken_fetch.ran("step3_resolve")


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
