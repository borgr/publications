"""Surface a pipeline failure to a human who is not watching the terminal.

An unattended run (launchd, cron, a scheduled Action) that fails silently is
worse than one that never ran, because the CV keeps looking current while it
quietly goes stale. Every channel here is best-effort and never raises: a
broken notifier must not be the reason a run reports failure.
"""

import os
import shutil
import subprocess
import sys

_MAX_BODY = 400


def _macos_notification(title, message):
    """Post to Notification Center. No-op off macOS or without osascript."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    # AppleScript string literals take backslash and double-quote escapes.
    def esc(text):
        return text.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{esc(message[:_MAX_BODY])}" '
              f'with title "{esc(title)}"')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _github_actions_annotation(message):
    """Emit a GitHub Actions error annotation, so it shows on the run summary."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return False
    flat = message.replace("\n", " ").replace("%", "%25")
    print(f"::error title=publications pipeline::{flat[:_MAX_BODY]}", flush=True)
    return True


def failure(summary, detail="", *, enabled=True):
    """Report a failure on every channel available. Always returns None."""
    text = summary if not detail else f"{summary}\n{detail}"
    print(f"\nFAILED: {text}", file=sys.stderr, flush=True)
    if not enabled:
        return
    _github_actions_annotation(text)
    _macos_notification("Publications pipeline failed", text)
