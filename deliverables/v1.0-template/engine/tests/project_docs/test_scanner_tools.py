"""Tests for the scanner MCP tool module.

These exercise the tool-module contract for ``scanner_tools``: read-only
``scanner_report`` returns candidates without writing anything, and the gated
modes degrade to a structured ``not_permitted`` status instead of raising when
their config gate is off.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge_engine.project_docs.config import load_config
from knowledge_engine.project_docs.mcp_tools import scanner_tools
from knowledge_engine.project_docs.mcp_tools.base import ToolContext


def _payload(result: dict) -> dict:
    """Decode the JSON text payload from an MCP content envelope."""
    return json.loads(result["content"][0]["text"])


def _make_project(root: Path) -> None:
    """Create a small sanitized doc tree the markdown detector can discover."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "overview.md").write_text(
        "# Overview\n\nThis is a synthetic project document for testing.\n",
        encoding="utf-8",
    )
    (docs / "devlog.md").write_text(
        "# Devlog\n\n- did a thing\n- did another thing\n",
        encoding="utf-8",
    )


def test_group_constant() -> None:
    assert scanner_tools.GROUP == "scanner"


def test_tools_expose_six_scanner_verbs() -> None:
    cfg = load_config()
    names = {t["name"] for t in scanner_tools.tools(cfg)}
    assert names == {
        "project_docs.scanner_report",
        "project_docs.scanner_ingest",
        "project_docs.scanner_status",
        "project_docs.scanner_validate",
        "project_docs.scanner_plan_pointers",
        "project_docs.scanner_apply_pointers",
    }


def test_scanner_report_returns_candidates_and_writes_nothing(tmp_path: Path) -> None:
    _make_project(tmp_path)
    cfg = load_config()  # scanner disabled by default; report ignores gates
    ctx = ToolContext(cfg=cfg, root=tmp_path)

    result = scanner_tools.dispatch("project_docs.scanner_report", {}, ctx)
    payload = _payload(result)

    assert payload["mode"] == "report"
    assert payload["candidate_count"] >= 0
    assert isinstance(payload["by_category"], dict)
    assert isinstance(payload["recommended_actions"], list)
    assert payload["candidate_count"] == sum(payload["by_category"].values())

    # Report mode must never create a database or any other file under the root.
    sqlite_files = list(tmp_path.rglob("*.sqlite")) + list(tmp_path.rglob("*.sqlite3"))
    assert sqlite_files == []
    ctx.close()


def test_scanner_ingest_not_permitted_when_scanner_disabled(tmp_path: Path) -> None:
    _make_project(tmp_path)
    cfg = load_config()  # scanner.enabled is False by default
    assert cfg.scanner.enabled is False
    ctx = ToolContext(cfg=cfg, root=tmp_path)

    result = scanner_tools.dispatch(
        "project_docs.scanner_ingest",
        {"project_fp": None, "branch_fp": "br_test"},
        ctx,
    )
    payload = _payload(result)

    assert payload["status"] == "not_permitted"
    assert "reason" in payload
    ctx.close()


def test_scanner_apply_pointers_not_permitted_when_mutation_off(tmp_path: Path) -> None:
    cfg = load_config()  # mcp.allow_mutating_tools is False by default
    assert cfg.mcp.allow_mutating_tools is False
    ctx = ToolContext(cfg=cfg, root=tmp_path)

    result = scanner_tools.dispatch(
        "project_docs.scanner_apply_pointers",
        {"plan_id": 1, "project_fp": None, "branch_fp": "br_test", "dry_run": False},
        ctx,
    )
    payload = _payload(result)

    assert payload["status"] == "not_permitted"
    assert "mutating" in payload["reason"].lower()
    ctx.close()


def test_scanner_status_and_validate_are_read_only(tmp_path: Path) -> None:
    cfg = load_config()
    ctx = ToolContext(cfg=cfg, root=tmp_path)

    status = _payload(scanner_tools.dispatch("project_docs.scanner_status", {}, ctx))
    assert status["scanner_enabled"] is False
    assert "git_available" in status

    validate = _payload(scanner_tools.dispatch("project_docs.scanner_validate", {}, ctx))
    assert validate["permitted_modes"]["report"] is True
    assert validate["permitted_modes"]["ingest"] is False
    assert validate["permitted_modes"]["pointer_apply"] is False
    ctx.close()


def test_unknown_tool_returns_status(tmp_path: Path) -> None:
    cfg = load_config()
    ctx = ToolContext(cfg=cfg, root=tmp_path)
    payload = _payload(scanner_tools.dispatch("project_docs.nope", {}, ctx))
    assert payload["status"] == "unknown_tool"
    ctx.close()
