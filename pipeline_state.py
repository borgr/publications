"""Which steps still need to run, decided by input content rather than mtimes.

Why not mtimes
--------------
The pipeline used to compare `os.path.getmtime` between outputs and inputs. That
has three failure modes, all of which this repo hit:

  * A fresh clone gives every file the checkout time, so the ordering is
    meaningless and steps skip or re-run essentially at random.
  * `touch`-like side effects leak into the logic. Step 3 rewrote orig.bib
    even when nothing changed, purely so its mtime would advance and mark the
    step done -- an output write performed for the benefit of the scheduler.
  * Rewriting a file with identical content still advances its mtime, so a
    no-op run cascades into re-running everything downstream.

Content hashing has none of those. A step is stale when the *bytes* of an input
differ from the bytes present the last time that step completed, which is both
clone-stable and immune to no-op rewrites.

Recorded per step, so adding an input to a step's dependency list correctly
invalidates just that step.
"""

import hashlib
import json
import os
import time

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(FILE_DIR, ".pipeline_state.json")

_CHUNK = 1 << 16


def hash_file(path):
    """Return a short content hash, or "" when the file does not exist.

    "" is a real value here: it means "absent", and an input appearing or
    disappearing is a change like any other.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()[:16]
    except OSError:
        return ""


class PipelineState:
    """Per-step record of the input hashes present when the step last succeeded."""

    VERSION = 1

    def __init__(self, steps=None, path=STATE_PATH):
        self.path = path
        self.steps = steps or {}

    @classmethod
    def load(cls, path=STATE_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        if data.get("version") != cls.VERSION:
            # An unrecognised schema means run everything rather than guess.
            return cls(path=path)
        return cls(steps=data.get("steps", {}), path=path)

    def save(self):
        tmp = self.path + ".tmp"
        payload = {
            "_comment": "Input content hashes from the last successful run of "
                        "each step, used to skip work whose inputs have not "
                        "changed. Safe to delete: the next run redoes everything.",
            "version": self.VERSION,
            "steps": {k: self.steps[k] for k in sorted(self.steps)},
        }
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)

    # ── staleness ────────────────────────────────────────────────────────────

    def changed_inputs(self, step, inputs):
        """Return the input paths whose contents differ from the recorded run.

        A step that has never run reports all of its inputs as changed.
        """
        recorded = (self.steps.get(step) or {}).get("inputs")
        if recorded is None:
            return list(inputs)
        return [p for p in inputs
                if recorded.get(os.path.basename(p)) != hash_file(p)]

    def is_stale(self, step, inputs):
        return bool(self.changed_inputs(step, inputs))

    def mark_done(self, step, inputs):
        """Record the current input hashes as this step's completed state."""
        self.steps[step] = {
            "inputs": {os.path.basename(p): hash_file(p) for p in inputs},
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "completed_epoch": int(time.time()),
        }

    # ── age, for the time-based fetch check ──────────────────────────────────

    def age_hours(self, step):
        """Hours since the step last completed, or inf if it never has.

        Recorded rather than read off a file mtime, so a fresh clone does not
        look like a fresh fetch.
        """
        epoch = (self.steps.get(step) or {}).get("completed_epoch")
        if not epoch:
            return float("inf")
        return max(0.0, (time.time() - epoch) / 3600)
