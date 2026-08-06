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

import errno
import hashlib
import json
import os
import time

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(FILE_DIR, ".pipeline_state.json")
LOCK_PATH = os.path.join(FILE_DIR, ".pipeline.lock")


class AlreadyRunning(Exception):
    """Another run holds the lock."""


class RunLock:
    """Stop two runs from interleaving their writes.

    The scheduled weekly run and a manual one can overlap. Individual files are
    written atomically, so neither can be truncated, but that does not prevent a
    lost update: both processes read the table, both write it, and the first
    one's new paper quietly disappears.

    A PID file rather than fcntl, because the useful message is "run 12345 is
    already going" and because a stale lock from a killed process must not wedge
    the pipeline forever -- if the recorded PID is gone, the lock is taken over.

    Used as a context manager; releasing is idempotent.
    """

    def __init__(self, path=LOCK_PATH):
        self.path = path
        self.acquired = False

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return exc.errno == errno.EPERM   # exists but owned by someone else
        return True

    def _read_pid(self):
        try:
            with open(self.path) as f:
                return int((f.read().split("\n")[0] or "0").strip())
        except (OSError, ValueError):
            return 0

    def acquire(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self._read_pid()
                # Any live holder blocks, including this process. Exempting our
                # own PID would let a run silently steal its own lock, which
                # defeats the point and hides a re-entrancy bug.
                if pid and self._alive(pid):
                    raise AlreadyRunning(
                        f"another run is in progress (pid {pid}). If it is not, "
                        f"delete {os.path.basename(self.path)}.")
                # Stale: the recorded process is gone. Take it over.
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            self.acquired = True
            return self
        raise AlreadyRunning(f"could not acquire {self.path}")

    def release(self):
        if not self.acquired:
            return
        # Only remove our own lock, so taking over a stale one cannot delete the
        # lock of whoever took it over from us.
        if self._read_pid() == os.getpid():
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()
        return False

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
