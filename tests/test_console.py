"""Output survives characters the platform's default codepage cannot encode."""

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import console


def test_a_cp1252_stream_is_switched_to_utf8(monkeypatch):
    """What a redirected stdout is on Windows: arrows raised UnicodeEncodeError."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    console.use_utf8_stdio()
    print("→ ✓ Gürel")
    stream.flush()
    assert raw.getvalue().decode("utf-8").rstrip("\r\n") == "→ ✓ Gürel"
    assert os.environ["PYTHONUTF8"] == "1"


def test_a_stream_without_reconfigure_is_left_alone(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())
    console.use_utf8_stdio()


def test_an_entry_point_prints_utf8_into_a_redirected_stream():
    """The real failure: a script's output going to a file, under a legacy codepage."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
    env["PYTHONIOENCODING"] = "cp1252"
    code = "import console; console.use_utf8_stdio(); print('\u2192 \u2713')"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, check=True).stdout
    assert out.decode("utf-8").strip() == "→ ✓"
