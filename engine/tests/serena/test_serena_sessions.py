"""Serena sessions against the fake stdio MCP server: verification, failures, isolation."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge_engine.knowledge.errors import (
    IncompatibleError,
    InvalidInputError,
    KnowledgeTimeoutError,
    UnavailableError,
)
from knowledge_engine.serena.manager import SerenaManager, run_tool
from knowledge_engine.serena.session import SessionState

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_session_starts_verifies_and_answers(
    make_manager: Callable[..., SerenaManager],
    call_log: Path,
    kit: SimpleNamespace,
    tmp_path: Path,
) -> None:
    manager = make_manager()
    result = run_tool(
        manager,
        "serena.find_symbol",
        {"project_id": "proj-a", "name_path_pattern": "Shape/area", "include_body": True},
    )
    (entry,) = result["results"]
    assert entry["path"] == "pkg/shapes.py"
    assert entry["locator"]["start_line"] == 2 and entry["locator"]["end_line"] == 4
    assert entry["locator"]["commit"] == kit.git(tmp_path / "repo-a", "rev-parse", "HEAD")
    assert result["repository"]["dirty"] is False
    assert "def area" in entry["body"]
    status = manager.status()["projects"][0]
    assert status["state"] == "ready" and status["server"] == "Serena 2.1.0"
    (call,) = kit.read_log(call_log)
    # The child gets an isolated HOME under the engine state dir and reporting off.
    assert Path(call["home"]).is_relative_to(tmp_path / "serena-state" / "home")
    assert call["reporting"] == "false"


@pytest.mark.parametrize("mode", ["wrong_version", "schema_drift"])
def test_mismatch_is_incompatible_and_no_tool_is_called(
    mode: str, make_manager: Callable[..., SerenaManager], call_log: Path, kit: SimpleNamespace
) -> None:
    manager = make_manager(mode)
    with pytest.raises(IncompatibleError) as caught:
        manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
    assert caught.value.details["problems"]
    with pytest.raises(IncompatibleError):  # sticky: never retried silently
        manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
    assert kit.read_log(call_log) == []
    assert manager.status()["projects"][0]["state"] == "incompatible"


def test_new_upstream_tools_are_reported_but_never_forwarded(
    make_manager: Callable[..., SerenaManager],
) -> None:
    manager = make_manager("extra_tool")
    manager.call("proj-a", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    assert manager.status()["projects"][0]["extra_upstream_tools"] == ["brand_new_tool"]
    with pytest.raises(InvalidInputError, match="not allowed"):
        manager.call("proj-a", "brand_new_tool", {})
    with pytest.raises(InvalidInputError, match="not allowed"):
        run_tool(manager, "serena.execute_shell_command", {"project_id": "proj-a"})


def test_upstream_errors_are_typed(make_manager: Callable[..., SerenaManager]) -> None:
    manager = make_manager()
    with pytest.raises(InvalidInputError, match="does not exist"):
        manager.call("proj-a", "get_symbols_overview", {"relative_path": "missing.py"})
    # The session survives a tool error.
    overview = manager.call("proj-a", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    assert overview == {"Class": ["Shape"], "Function": ["make_a"]}
    with pytest.raises(InvalidInputError, match="no Serena repository"):
        run_tool(manager, "serena.find_symbol", {"project_id": "proj-x", "name_path_pattern": "A"})


def test_timeout_stops_the_session_and_backs_off(
    make_manager: Callable[..., SerenaManager], fake_env: Callable[..., dict[str, str]]
) -> None:
    manager = make_manager(env=fake_env("slow", FAKE_SERENA_SLEEP_S="30"), request_timeout_s=1.0)
    with pytest.raises(KnowledgeTimeoutError):
        manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
    session = manager.session("proj-a")
    assert not session.running
    status = session.snapshot()
    assert status.state == SessionState.BACKOFF
    assert status.consecutive_failures == 1 and "timed out" in (status.last_error or "")


def test_a_killed_process_is_restarted(
    make_manager: Callable[..., SerenaManager], call_log: Path, kit: SimpleNamespace
) -> None:
    manager = make_manager("crash_once")
    with pytest.raises(UnavailableError):
        manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
    # The first restart has no delay; the fake does not crash a second time.
    result = manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
    assert [r["relative_path"] for r in result] == ["pkg/shapes.py"]
    status = manager.session("proj-a").snapshot()
    assert status.restarts == 1 and status.consecutive_failures == 0
    pids = {call["pid"] for call in kit.read_log(call_log)}
    assert len(pids) == 2  # a new process after the crash


def test_calls_during_backoff_fail_retryable(
    make_manager: Callable[..., SerenaManager],
) -> None:
    manager = make_manager("crash_once")
    session = manager.session("proj-a")
    with pytest.raises(UnavailableError):
        session.call("find_symbol", {"name_path_pattern": "Shape"})
    session._retry_at = session._clock() + 30  # simulate a later failure's backoff
    with pytest.raises(UnavailableError) as caught:
        session.call("find_symbol", {"name_path_pattern": "Shape"})
    assert caught.value.retryable and caught.value.details["retry_in_s"] > 0


def test_two_projects_in_parallel_never_cross(
    make_manager: Callable[..., SerenaManager], call_log: Path, kit: SimpleNamespace
) -> None:
    manager = make_manager()
    results: dict[str, list[dict[str, Any]]] = {}
    errors: list[BaseException] = []

    def search(project_id: str) -> None:
        try:
            for _ in range(3):
                found = run_tool(
                    manager,
                    "serena.find_symbol",
                    {"project_id": project_id, "name_path_pattern": "Shape", "depth": 1},
                )
                results.setdefault(project_id, []).extend(found["results"])
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=search, args=(pid,)) for pid in ("proj-a", "proj-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors
    assert {r["locator"]["repository_id"] for r in results["proj-a"]} == {"example.org/a"}
    assert {r["locator"]["repository_id"] for r in results["proj-b"]} == {"example.org/b"}
    by_project: dict[str, set[int]] = {}
    for call in kit.read_log(call_log):
        by_project.setdefault(Path(call["project"]).name, set()).add(call["pid"])
    assert set(by_project) == {"repo-a", "repo-b"}
    assert by_project["repo-a"].isdisjoint(by_project["repo-b"])
    refs = run_tool(
        manager,
        "serena.find_referencing_symbols",
        {"project_id": "proj-b", "name_path": "Shape", "relative_path": "pkg/use.py"},
    )
    assert all(r["locator"]["repository_id"] == "example.org/b" for r in refs["results"])


def test_paths_outside_the_project_are_dropped(
    make_manager: Callable[..., SerenaManager],
) -> None:
    manager = make_manager("escape")
    result = run_tool(
        manager, "serena.find_symbol", {"project_id": "proj-a", "name_path_pattern": "Shape"}
    )
    assert result["results"] == []
    assert result["rejected_paths"] == ["../outside.py"]


def test_session_cap_stops_the_least_recently_used(
    make_manager: Callable[..., SerenaManager],
) -> None:
    manager = make_manager(max_sessions=1)
    manager.call("proj-a", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    first = manager.session("proj-a")
    assert first.running
    manager.call("proj-b", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    assert not first.running and manager.session("proj-b").running


def test_idle_sessions_are_reaped(make_manager: Callable[..., SerenaManager]) -> None:
    manager = make_manager(idle_shutdown_s=60)
    manager.call("proj-a", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    session = manager.session("proj-a")
    assert manager.reap_idle() == []
    session.last_used -= 120
    assert manager.reap_idle() == ["proj-a"]
    assert not session.running
    # The next call starts it again.
    manager.call("proj-a", "get_symbols_overview", {"relative_path": "pkg/shapes.py"})
    assert session.running


def test_check_reports_each_project(make_manager: Callable[..., SerenaManager]) -> None:
    report = make_manager().check()
    assert report["ok"] is True
    assert [p["session"]["state"] for p in report["projects"]] == ["ready", "ready"]
    bad = make_manager("wrong_version").check("proj-a")
    assert bad["ok"] is False and bad["projects"][0]["error"]["code"] == "incompatible"


def test_closed_manager_refuses_calls(make_manager: Callable[..., SerenaManager]) -> None:
    manager = make_manager()
    manager.close()
    with pytest.raises(UnavailableError, match="shut down"):
        manager.call("proj-a", "find_symbol", {"name_path_pattern": "Shape"})
