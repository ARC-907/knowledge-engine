"""Tests for the log/test MCP tool module.

These tests run fully offline. They build a temporary project DB with the
migration runner, seed a test run via
:func:`knowledge_engine.project_docs.logs.record_test_run`, and assert that:

* ``project_docs.get_test_history`` returns the seeded run;
* ``project_docs.search_runtime_logs`` returns ``status == "disabled"`` under
  the default (runtime logs off) configuration;
* raw-log fields are never returned under the default config.

A lightweight stand-in context object is used so the tests exercise the
documented ``ToolContext`` surface (``cfg``, ``root``, ``registry_conn``,
``project_conn``) without coupling to its constructor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.project_docs import db, fingerprints, logs
from knowledge_engine.project_docs.config import load_config
from knowledge_engine.project_docs.mcp_tools import log_tools

_REGISTRY_PREFIXES = ("001_",)
_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")
_MIGRATIONS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "knowledge_engine"
    / "project_docs"
    / "migrations"
)


@dataclass
class _Ctx:
    """Minimal ToolContext stand-in exposing only the dispatch surface."""

    cfg: Any
    root: Path
    registry: Any
    project: Any

    def registry_conn(self):
        return self.registry

    def project_conn(self, project_fp: str | None = None):
        return self.project


@pytest.fixture()
def cfg():
    """Default config (runtime logs off, raw logs off)."""

    return load_config()


@pytest.fixture()
def conns():
    """Build an in-memory registry DB and project DB with migrations applied."""

    registry_conn = db.connect(":memory:")
    db.apply_migrations(registry_conn, _MIGRATIONS, only_prefixes=_REGISTRY_PREFIXES)

    project_conn = db.connect(":memory:")
    db.apply_migrations(project_conn, _MIGRATIONS, only_prefixes=_PROJECT_PREFIXES)

    return registry_conn, project_conn


@pytest.fixture()
def context(cfg, conns, tmp_path):
    registry_conn, project_conn = conns
    return _Ctx(cfg=cfg, root=tmp_path, registry=registry_conn, project=project_conn)


def _seed_test_run(registry_conn, project_conn, cfg):
    """Allocate fingerprints and record one failing test run."""

    proj = fingerprints.ensure_project(registry_conn, "/sample/proj", "sample")
    branch = fingerprints.ensure_branch(registry_conn, proj, "main")
    run = logs.record_test_run(
        project_conn,
        registry_conn,
        cfg,
        command="pytest -q",
        exit_code=1,
        started_at="2026-05-30T00:00:00Z",
        duration_ms=1234,
        framework="pytest",
        target="tests/",
        output="1 failed, 2 passed\nAssertionError: boom",
        project_fp=proj,
        branch_fp=branch,
    )
    return proj, branch, run


def test_tools_advertised(cfg):
    """All six log tools are advertised with the project_docs.* namespace."""

    names = {t["name"] for t in log_tools.tools(cfg)}
    assert names == {
        "project_docs.search_test_logs",
        "project_docs.search_build_logs",
        "project_docs.search_runtime_logs",
        "project_docs.get_test_history",
        "project_docs.get_failure_context",
        "project_docs.get_latest_test_summary",
    }
    assert log_tools.GROUP == "log"


def test_get_test_history_returns_seeded_run(context, conns, cfg):
    """A seeded test run is returned by get_test_history."""

    registry_conn, project_conn = conns
    _seed_test_run(registry_conn, project_conn, cfg)

    result = log_tools.dispatch("project_docs.get_test_history", {}, context)
    payload = _payload(result)
    rows = payload["results"]
    assert len(rows) == 1
    assert rows[0]["command"] == "pytest -q"
    assert rows[0]["classification"] == "fail"


def test_search_runtime_logs_disabled_by_default(context):
    """Runtime-log search is dark unless include_runtime_logs is enabled."""

    result = log_tools.dispatch(
        "project_docs.search_runtime_logs", {"query": "anything"}, context
    )
    assert _payload(result)["status"] == "disabled"


def test_raw_logs_stripped_by_default(context, conns, cfg):
    """Raw-log keys never leave the process unless allow_raw_logs is set."""

    registry_conn, project_conn = conns
    _seed_test_run(registry_conn, project_conn, cfg)

    result = log_tools.dispatch("project_docs.get_test_history", {}, context)
    for row in _payload(result)["results"]:
        assert "raw_log" not in row
        assert "raw_output" not in row


def test_latest_test_summary_returns_seeded_run(context, conns, cfg):
    """get_latest_test_summary returns the most recent run."""

    registry_conn, project_conn = conns
    _seed_test_run(registry_conn, project_conn, cfg)

    result = log_tools.dispatch("project_docs.get_latest_test_summary", {}, context)
    payload = _payload(result)
    assert payload["classification"] == "fail"


def test_get_failure_context_requires_id(context, conns, cfg):
    """get_failure_context returns invalid_args when no test_run_id is given."""

    registry_conn, project_conn = conns
    _seed_test_run(registry_conn, project_conn, cfg)

    result = log_tools.dispatch("project_docs.get_failure_context", {}, context)
    assert _payload(result)["status"] == "invalid_args"


def test_get_failure_context_returns_run(context, conns, cfg):
    """get_failure_context returns the run detail for a known id."""

    registry_conn, project_conn = conns
    _proj, _branch, run = _seed_test_run(registry_conn, project_conn, cfg)

    result = log_tools.dispatch(
        "project_docs.get_failure_context", {"test_run_id": run.id}, context
    )
    payload = _payload(result)
    assert payload["id"] == run.id
    assert payload["classification"] == "fail"
    assert "raw_log" not in payload


def test_unknown_project_returns_not_configured(cfg, conns, tmp_path):
    """A context whose project_conn resolves to None returns not_configured."""

    registry_conn, _project_conn = conns
    ctx = _Ctx(cfg=cfg, root=tmp_path, registry=registry_conn, project=None)
    result = log_tools.dispatch("project_docs.get_test_history", {}, ctx)
    assert _payload(result)["status"] == "not_configured"


def test_unknown_tool_returns_status(context):
    """An unrecognized tool name returns a structured unknown_tool status."""

    result = log_tools.dispatch("project_docs.not_a_tool", {}, context)
    assert _payload(result)["status"] == "unknown_tool"


def _payload(result: dict) -> dict:
    """Extract the JSON object carried by a text/status tool result."""

    assert isinstance(result, dict)
    content = result.get("content")
    if isinstance(content, list) and content:
        return json.loads(content[0]["text"])
    if isinstance(result.get("text"), str):
        return json.loads(result["text"])
    return result
