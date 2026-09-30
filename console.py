"""Make every script's output UTF-8, whatever the platform's default is.

The pipeline prints arrows, check marks and non-English titles. On Windows, a
stream that is redirected to a file or a pipe -- the scheduled run's log, a
subprocess, CI -- is encoded with the locale's codepage (cp1252), so the first
such character raised UnicodeEncodeError and killed the run at step 6. A real
console is unaffected, which is why this only ever failed unattended.
"""

import os
import sys


def use_utf8_stdio() -> None:
    """Re-encode stdout and stderr as UTF-8, and have child Pythons do the same."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    # update.py runs fetch_citations.py as a subprocess writing to the same log.
    os.environ.setdefault("PYTHONUTF8", "1")
