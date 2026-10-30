"""Opt-in: the adapter against a real Serena (``KE_SERENA_LIVE=1`` and ``KE_SERENA_COMMAND``).

Two temporary git projects both define ``class Shape``; each project's
session must answer from its own repository only. Serena needs ``uv`` on its
PATH (it starts pyright through ``uvx``) and may download the language server
on first use, so this test is never part of CI.

    KE_SERENA_LIVE=1 KE_SERENA_COMMAND=/path/to/serena pytest tests/serena/test_serena_live.py
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_engine.serena.config import SerenaConfig, SerenaProject
from knowledge_engine.serena.manager import SerenaManager, run_tool

# Read at import: the suite's autouse fixture clears every KE_* variable per test.
LIVE = os.environ.get("KE_SERENA_LIVE") == "1"
COMMAND = os.environ.get("KE_SERENA_COMMAND", "")

pytestmark = pytest.mark.skipif(
    not LIVE or not COMMAND or shutil.which("git") is None,
    reason="live Serena test: set KE_SERENA_LIVE=1 and KE_SERENA_COMMAND (and install git)",
)


@pytest.fixture
def live(tmp_path: Path, kit: SimpleNamespace) -> Iterator[SerenaManager]:
    a = kit.make_repo(tmp_path / "live-a", {"pkg/shapes.py": kit.SHAPES_A})
    b = kit.make_repo(tmp_path / "live-b", {"pkg/shapes.py": kit.SHAPES_B})
    config = SerenaConfig(
        enabled=True,
        command=tuple(shlex.split(COMMAND)),
        expected_version="2.1.0",
        projects_file=tmp_path / "unused.toml",
        state_dir=tmp_path / "serena-state",
        request_timeout_s=180.0,
        startup_timeout_s=600.0,
        idle_shutdown_s=0,
        projects={
            "live-a": SerenaProject("live-a", a.resolve(), "example.org/live-a"),
            "live-b": SerenaProject("live-b", b.resolve(), "example.org/live-b"),
        },
    )
    manager = SerenaManager(config, start_reaper=False)
    yield manager
    manager.close()


def test_live_serena_answers_per_project(live: SerenaManager) -> None:
    report = live.check()
    assert report["ok"], report
    for project_id, method in (("live-a", "area"), ("live-b", "perimeter")):
        found = run_tool(
            live,
            "serena.find_symbol",
            {
                "project_id": project_id,
                "name_path_pattern": f"Shape/{method}",
                "include_body": True,
            },
        )
        (entry,) = found["results"]
        assert entry["path"] == "pkg/shapes.py"
        assert entry["locator"]["repository_id"] == f"example.org/{project_id}"
        assert entry["locator"]["commit"] is not None
        assert (entry["locator"]["start_line"], entry["locator"]["end_line"]) == (2, 4)
        assert f"def {method}" in entry["body"]
    overview = run_tool(
        live,
        "serena.get_symbols_overview",
        {"project_id": "live-b", "relative_path": "pkg/shapes.py"},
    )
    assert overview["results"]["Class"] == ["Shape"]
    assert "make_a" not in str(overview["results"])
    refs = run_tool(
        live,
        "serena.find_referencing_symbols",
        {"project_id": "live-a", "name_path": "Shape", "relative_path": "pkg/shapes.py"},
    )
    assert all(r["locator"]["repository_id"] == "example.org/live-a" for r in refs["results"])
