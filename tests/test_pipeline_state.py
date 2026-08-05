"""Step skipping, and the mtime failure modes it replaces."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline_state import PipelineState, hash_file


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
