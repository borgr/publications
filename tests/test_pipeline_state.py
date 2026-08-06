"""Step skipping, and the mtime failure modes it replaces."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline_state import AlreadyRunning, PipelineState, RunLock, hash_file


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_step_that_never_ran_is_stale(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    state = PipelineState(path=str(tmp_path / "state.json"))
    assert state.is_stale("build", [a])


def test_unchanged_inputs_are_not_stale(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [a])
    assert not state.is_stale("build", [a])


def test_changed_content_is_stale_and_is_named(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    b = write(tmp_path / "b.txt", "two")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [a, b])
    write(tmp_path / "b.txt", "changed")
    assert state.changed_inputs("build", [a, b]) == [b]


def test_rewriting_identical_content_is_not_a_change(tmp_path):
    """The mtime bug: an unchanged rewrite used to cascade a full re-run."""
    a = write(tmp_path / "a.txt", "one")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [a])
    os.utime(a, (0, 0))          # mtime moves backwards
    write(tmp_path / "a.txt", "one")  # and forwards again, same bytes
    assert not state.is_stale("build", [a])


def test_a_new_input_added_to_a_step_makes_it_stale(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    b = write(tmp_path / "b.txt", "two")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [a])
    assert state.changed_inputs("build", [a, b]) == [b]


def test_a_missing_input_is_a_recorded_state_not_a_crash(tmp_path):
    missing = str(tmp_path / "gone.txt")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [missing])
    assert not state.is_stale("build", [missing])
    write(tmp_path / "gone.txt", "now exists")
    assert state.is_stale("build", [missing])


def test_steps_are_tracked_independently(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("build", [a])
    assert not state.is_stale("build", [a])
    assert state.is_stale("tex", [a])


def test_state_round_trips(tmp_path):
    a = write(tmp_path / "a.txt", "one")
    path = str(tmp_path / "state.json")
    state = PipelineState(path=path)
    state.mark_done("build", [a])
    state.save()
    assert not PipelineState.load(path).is_stale("build", [a])


def test_corrupt_state_file_means_run_everything(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    a = write(tmp_path / "a.txt", "one")
    assert PipelineState.load(str(path)).is_stale("build", [a])


def test_unknown_schema_version_means_run_everything(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 999, "steps": {"build": {"inputs": {}}}}',
                    encoding="utf-8")
    a = write(tmp_path / "a.txt", "one")
    assert PipelineState.load(str(path)).is_stale("build", [a])


def test_age_is_infinite_before_the_first_run(tmp_path):
    state = PipelineState(path=str(tmp_path / "state.json"))
    assert state.age_hours("fetch") == float("inf")


def test_age_is_recorded_not_read_from_a_file_mtime(tmp_path):
    """A fresh clone must not look like a fresh fetch."""
    a = write(tmp_path / "citations.csv", "data")
    state = PipelineState(path=str(tmp_path / "state.json"))
    state.mark_done("fetch", [a])
    assert state.age_hours("fetch") < 1
    os.utime(a, (0, 0))
    assert state.age_hours("fetch") < 1


def test_hash_of_a_missing_file_is_empty():
    assert hash_file("/definitely/not/here") == ""


def test_hash_is_content_addressed(tmp_path):
    a = write(tmp_path / "a.txt", "same")
    b = write(tmp_path / "b.txt", "same")
    assert hash_file(a) == hash_file(b)
    write(tmp_path / "b.txt", "different")
    assert hash_file(a) != hash_file(b)


# ── one run at a time ────────────────────────────────────────────────────────
#
# The weekly scheduled run and a manual one can overlap. Atomic writes stop a
# file being truncated but not a lost update: both read the table, both write it,
# and the first one's new paper quietly disappears.

_os = os


def test_a_second_run_is_refused(tmp_path):
    path = str(tmp_path / "run.lock")
    with RunLock(path):
        with pytest.raises(AlreadyRunning) as excinfo:
            RunLock(path).acquire()
        assert str(_os.getpid()) in str(excinfo.value)


def test_the_lock_is_released_on_exit(tmp_path):
    path = str(tmp_path / "run.lock")
    with RunLock(path):
        assert _os.path.exists(path)
    assert not _os.path.exists(path)


def test_the_lock_is_released_even_when_the_run_raises(tmp_path):
    path = str(tmp_path / "run.lock")
    with pytest.raises(ValueError):
        with RunLock(path):
            raise ValueError("boom")
    assert not _os.path.exists(path)


def test_a_stale_lock_from_a_dead_process_is_taken_over(tmp_path):
    """A killed run must not wedge the pipeline forever."""
    path = tmp_path / "run.lock"
    # PID 1 exists but a plausibly-dead high PID does not; use an unused one.
    dead = 2 ** 22
    path.write_text(f"{dead}\n2026-01-01T00:00:00\n", encoding="utf-8")
    with RunLock(str(path)) as lock:
        assert lock.acquired
        assert path.read_text(encoding="utf-8").startswith(str(_os.getpid()))
    assert not path.exists()


def test_a_corrupt_lock_file_is_taken_over(tmp_path):
    path = tmp_path / "run.lock"
    path.write_text("not a pid", encoding="utf-8")
    with RunLock(str(path)) as lock:
        assert lock.acquired


def test_releasing_twice_is_harmless(tmp_path):
    lock = RunLock(str(tmp_path / "run.lock")).acquire()
    lock.release()
    lock.release()


def test_releasing_does_not_delete_someone_elses_lock(tmp_path):
    """After a takeover, the original owner must not remove the new lock."""
    path = tmp_path / "run.lock"
    ours = RunLock(str(path)).acquire()
    path.write_text("999999\n", encoding="utf-8")   # someone else took over
    ours.release()
    assert path.exists(), "must only remove a lock we still own"
