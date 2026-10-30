"""The tool host's bounded argv runner and argv validation (the module execution substrate)."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from knowledge_engine.tool_host import host


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "s.py"
    path.write_text(body)
    return path


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "")}


def test_runs_with_stdin_and_only_the_given_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST_ONLY", "x")
    script = _script(
        tmp_path,
        "import os, sys\nsys.stdout.write(sys.stdin.read() + '|' + str('HOST_ONLY' in os.environ))\n",
    )
    out = host.run_bounded(
        [sys.executable, str(script)],
        cwd=tmp_path,
        stdin=b"hi",
        env=_env(),
        timeout_s=10,
        max_stdout_bytes=100,
    )
    assert out.returncode == 0 and out.stdout == b"hi|False"
    assert not (out.timed_out or out.cancelled or out.output_exceeded)


def test_timeout_and_output_cap(tmp_path: Path) -> None:
    slow = _script(tmp_path, "import time\ntime.sleep(30)\n")
    out = host.run_bounded(
        [sys.executable, str(slow)],
        cwd=tmp_path,
        stdin=b"",
        env=_env(),
        timeout_s=0.3,
        max_stdout_bytes=10,
    )
    assert out.timed_out and out.returncode is None and out.elapsed_ms < 8000
    loud = tmp_path / "loud.py"
    loud.write_text(
        "import sys, time\nsys.stdout.write('x' * 100000)\nsys.stdout.flush()\ntime.sleep(30)\n"
    )
    out = host.run_bounded(
        [sys.executable, str(loud)],
        cwd=tmp_path,
        stdin=b"",
        env=_env(),
        timeout_s=20,
        max_stdout_bytes=1000,
    )
    assert out.output_exceeded and len(out.stdout) == 1000 and out.elapsed_ms < 8000


def test_cancel_hook(tmp_path: Path) -> None:
    slow = _script(tmp_path, "import time\ntime.sleep(30)\n")
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    out = host.run_bounded(
        [sys.executable, str(slow)],
        cwd=tmp_path,
        stdin=b"",
        env=_env(),
        timeout_s=20,
        max_stdout_bytes=10,
        should_cancel=flag.is_set,
    )
    assert out.cancelled and not out.timed_out


def test_resolve_script_argv_uses_the_tool_host_rules(tmp_path: Path) -> None:
    script = _script(tmp_path, "")
    assert host.resolve_script_argv(["python", "s.py", "--x"], tmp_path) == [
        "python",
        str(script.resolve()),
        "--x",
    ]
    for argv, needle in (
        (["python", "-c", "print(1)"], "Inline code flag"),
        (["python", "-X", "s.py"], "option"),
        (["perl", "s.py"], "not in allowed list"),
        (["python", "../outside.py"], "outside"),
        (["python", "missing.py"], "Script not found"),
        ([], "empty"),
    ):
        with pytest.raises(ValueError, match=needle):
            host.resolve_script_argv(argv, tmp_path)
