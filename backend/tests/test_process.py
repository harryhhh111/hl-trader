from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hl_app.services import process


def run_child(script: str, timeout: float):
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    started = time.monotonic()
    try:
        out, err = process.communicate_bounded(proc, timeout)
        return proc, out, err, time.monotonic() - started
    except Exception:
        proc.wait()
        raise


def test_both_pipes_drained_under_pressure() -> None:
    script = (
        "import sys\n"
        "for i in range(200):\n"
        "    sys.stdout.write('o'*2048); sys.stdout.flush()\n"
        "    sys.stderr.write('e'*2048); sys.stderr.flush()\n"
    )
    proc, out, err, elapsed = run_child(script, 15)
    assert proc.returncode == 0
    assert len(out) == 200 * 2048 and len(err) == 200 * 2048
    assert elapsed < 10


def test_multibyte_chars_split_across_chunks() -> None:
    script = (
        "import sys,time\n"
        "data='比价中文字符'*2000\n"
        "for i in range(0,len(data),7):\n"
        "    sys.stdout.write(data[i:i+7]); sys.stdout.flush(); time.sleep(0.001)\n"
    )
    proc, out, _err, _elapsed = run_child(script, 20)
    assert proc.returncode == 0
    assert "比价中文字符" in out and len(out) == len("比价中文字符" * 2000)


def test_no_output_child() -> None:
    proc, out, err, _elapsed = run_child("pass", 10)
    assert proc.returncode == 0 and out == "" and err == ""


def test_oversized_output_kills_child(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "print('x' * (2 * 1024 * 1024))\n"
    )
    with pytest.raises(process.HyperliquidUnavailable, match="1 MiB"):
        run_child(script, 15)
    with pytest.raises(OSError):
        os.kill(int(pid_file.read_text()), 0)


def test_child_closing_output_but_not_exiting_is_killed(tmp_path) -> None:
    pid_file = tmp_path / "child2.pid"
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "os.close(1); os.close(2)\n"
        "import time; time.sleep(60)\n"
    )
    with pytest.raises(process.HyperliquidUnavailable, match="超时"):
        run_child(script, 1)
    with pytest.raises(OSError):
        os.kill(int(pid_file.read_text()), 0)


def test_silent_child_timeout_reaps(tmp_path) -> None:
    pid_file = tmp_path / "child3.pid"
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "import time; time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(process.HyperliquidUnavailable, match="超时"):
        run_child(script, 1)
    assert time.monotonic() - started < 5
    with pytest.raises(OSError):
        os.kill(int(pid_file.read_text()), 0)


def test_flushed_output_then_sleep_obeys_deadline(tmp_path) -> None:
    pid_file = tmp_path / "flushed-child.pid"
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "import time; print('partial-output', flush=True); time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(process.HyperliquidUnavailable, match="超时"):
        run_child(script, 0.3)
    assert time.monotonic() - started < 3
    with pytest.raises(OSError):
        os.kill(int(pid_file.read_text()), 0)
