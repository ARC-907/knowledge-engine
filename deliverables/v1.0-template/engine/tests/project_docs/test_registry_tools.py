"""Tests for the registry MCP tool group.

Offline: builds a temporary registry DB via the frozen P0 ``db`` factory and a
``ToolContext`` over a temp root, then exercises the dispatch surface end to end
(register -> list -> validate -> branches -> resolve -> current_context).
Git/network are not touched: ``current_context`` falls back to the configured
default branch when no git binary / repo is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_engine.project_docs import db as pddb
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.mcp_tools import registry_tools
from knowledge_engine.project_docs.mcp_tools.base import ToolContext


def _payload(result: dict) -> dict:
    """Decode the JSON text body out of an MCP text-result envelope."""
    import json

    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    """A ToolContext whose registry DB lives under a fresh temp root."""
    cfg = ProjectDocsConfig()
    context = ToolContext(cfg=cfg, root=tmp_path)
    # Force the registry connection open + migrated (001 only).
    conn = context.registry_conn()
    assert pddb.schema_version(conn) >= 1
    yield context
    context.close()


def test_group_constant() -> None:
    assert registry_tools.GROUP == "registry"


def test_tools_definitions_are_well_formed() -> None:
    defs = registry_tools.tools(ProjectDocsConfig())
    names = {t["name"] for t in defs}
    assert names == {
        "project_docs.list_projects",
        "project_docs.register_project",
        "project_docs.validate_project",
        "project_docs.list_branches",
        "project_docs.resolve_fingerprint",
        "project_docs.current_context",
    }
    for tool in defs:
        assert tool["name"].startswith("project_docs.")
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]


def test_register_then_list(ctx: ToolContext) -> None:
    reg = _payload(registry_tools.dispatch("project_docs.register_project", {}, ctx))
    assert reg["project_fp"].startswith("proj_")

    listed = _payload(registry_tools.dispatch("project_docs.list_projects", {}, ctx))
    assert len(listed["projects"]) >= 1
    assert any(p["project_fp"] == reg["project_fp"] for p in listed["projects"])


def test_register_is_idempotent(ctx: ToolContext) -> None:
    a = _payload(registry_tools.dispatch("project_docs.register_project", {}, ctx))
    b = _payload(registry_tools.dispatch("project_docs.register_project", {}, ctx))
    assert a["project_fp"] == b["project_fp"]
    listed = _payload(registry_tools.dispatch("project_docs.list_projects", {}, ctx))
    matches = [p for p in listed["projects"] if p["project_fp"] == a["project_fp"]]
    assert len(matches) == 1


def test_current_context_returns_project_fp(ctx: ToolContext) -> None:
    result = _payload(registry_tools.dispatch("project_docs.current_context", {}, ctx))
    assert result["project_fp"].startswith("proj_")
    assert result["branch_fp"].startswith("br_")
    assert result["branch"]
    assert result["git_available"] is False


def test_validate_project_roundtrip(ctx: ToolContext) -> None:
    reg = _payload(registry_tools.dispatch("project_docs.register_project", {}, ctx))
    ok = _payload(
        registry_tools.dispatch(
            "project_docs.validate_project", {"project_fp": reg["project_fp"]}, ctx
        )
    )
    assert ok["exists"] is True
    assert ok["name"]

    missing = _payload(
        registry_tools.dispatch(
            "project_docs.validate_project", {"project_fp": "proj_doesnotexist"}, ctx
        )
    )
    assert missing["exists"] is False


def test_validate_project_requires_arg(ctx: ToolContext) -> None:
    result = _payload(registry_tools.dispatch("project_docs.validate_project", {}, ctx))
    assert result["status"] == "invalid_arguments"


def test_list_branches_after_context(ctx: ToolContext) -> None:
    cc = _payload(registry_tools.dispatch("project_docs.current_context", {}, ctx))
    branches = _payload(
        registry_tools.dispatch(
            "project_docs.list_branches", {"project_fp": cc["project_fp"]}, ctx
        )
    )
    assert branches["project_fp"] == cc["project_fp"]
    assert any(b["branch_fp"] == cc["branch_fp"] for b in branches["branches"])


def test_resolve_fingerprint_is_deterministic_and_no_write(ctx: ToolContext) -> None:
    first = _payload(
        registry_tools.dispatch(
            "project_docs.resolve_fingerprint", {"branch": "main"}, ctx
        )
    )
    second = _payload(
        registry_tools.dispatch(
            "project_docs.resolve_fingerprint", {"branch": "main"}, ctx
        )
    )
    assert first == second
    assert first["project_fp"].startswith("proj_")
    assert first["branch_fp"].startswith("br_")

    # Deriving must not register anything.
    listed = _payload(registry_tools.dispatch("project_docs.list_projects", {}, ctx))
    assert listed["projects"] == []


def test_unknown_tool_returns_status(ctx: ToolContext) -> None:
    result = _payload(registry_tools.dispatch("project_docs.bogus", {}, ctx))
    assert result["status"] == "unknown_tool"
