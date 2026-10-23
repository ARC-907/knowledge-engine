"""Importing the engine's entry modules has no side effects.

Each module is imported in a fresh interpreter whose working directory holds a
``pipeline/.env`` (the file the pipeline config used to load at import) with
outbound sockets disabled. The import must not mutate ``os.environ``, write to
stdout or open a network connection, and the lean entry points must not
eagerly import the pipeline submodules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

PROBE = """
import io, os, socket, sys
env = dict(os.environ)

def _no_network(*args, **kwargs):
    raise AssertionError(f"network access at import: {args!r}")

socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network
socket.create_connection = _no_network
socket.getaddrinfo = _no_network

out, sys.stdout = sys.stdout, io.StringIO()
import importlib
importlib.import_module(sys.argv[1])
written, sys.stdout = sys.stdout.getvalue(), out
assert written == "", repr(written)
assert dict(os.environ) == env, sorted(set(os.environ) ^ set(env))
eager = sorted(
    name for name in sys.modules
    if name.startswith("knowledge_engine.runtime.pipeline.")
)
print(",".join(eager))
"""

#: Modules that must not pull in any pipeline submodule (queue, message board, ...).
LEAN = {
    "knowledge_engine.cli",
    "knowledge_engine.runtime.pipeline",
    "knowledge_engine.runtime.foundation.config",
    "knowledge_engine.runtime.foundation.db",
}

MODULES = [
    "knowledge_engine.cli",
    "knowledge_engine.runtime.pipeline",
    "knowledge_engine.runtime.foundation.config",
    "knowledge_engine.runtime.foundation.db",
    "knowledge_engine.agent_board.cli",
    "knowledge_engine.tool_host.host",
    "knowledge_engine.core.util.sqlite",
    "knowledge_engine.core.ollama",
]


@pytest.mark.parametrize("module", MODULES)
def test_import_has_no_side_effects(module: str, tmp_path: Path) -> None:
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / ".env").write_text("KE_IMPORT_SIDE_EFFECT=1\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KE_") or key == "KE_UI_TESTS"
    }
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, module],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    eager = [name for name in proc.stdout.strip().split(",") if name]
    if module in LEAN:
        assert eager == [], f"{module} eagerly imported {eager}"
