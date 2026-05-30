"""Tests for the MCP tool-discovery seam and server merge.

Verifies the existing 4 base tools are preserved and the project-docs capability
tools are merged in and answer without error when features are off.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.mcp_tools import collect_tools
from knowledge_engine.project_docs.mcp_tools.base import ToolContext


def _ctx(tmp_path: Path, cfg: ProjectDocsConfig) -> ToolContext:
    return ToolContext(cfg=cfg, root=tmp_path)


def test_collect_tools_disabled_when_mcp_off() -> None:
    cfg = ProjectDocsConfig(mcp=ProjectDocsConfig().mcp.__class__(enabled=False))
    defs, dispatch = collect_tools(cfg)
    assert defs == []
    assert dispatch == {}


def test_capability_tools_present_and_safe(tmp_path: Path) -> None:
    cfg = ProjectDocsConfig()  # mcp.enabled defaults True
    defs, dispatch = collect_tools(cfg)
    names = {d["name"] for d in defs}
    assert "project_docs.capabilities" in names
    assert "project_docs.config_status" in names
    assert "project_docs.healthcheck" in names

    res = dispatch["project_docs.capabilities"]("project_docs.capabilities", {}, _ctx(tmp_path, cfg))
    payload = json.loads(res["content"][0]["text"])
    assert payload["scanner_enabled"] is False
    assert payload["embeddings_enabled"] is False
    assert payload["source_mutation_allowed"] is False


def test_healthcheck_never_raises(tmp_path: Path) -> None:
    cfg = ProjectDocsConfig()
    _, dispatch = collect_tools(cfg)
    res = dispatch["project_docs.healthcheck"]("project_docs.healthcheck", {}, _ctx(tmp_path, cfg))
    payload = json.loads(res["content"][0]["text"])
    assert payload["registry"] == "ok"
    assert payload["project_count"] == 0


def test_server_merges_without_breaking_base(tmp_path: Path, monkeypatch) -> None:
    # Point the base engine at temp dirs so Server() constructs cleanly.
    monkeypatch.setenv("KE_CORPUS_ROOT", str(tmp_path / "corpus"))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(tmp_path / "corpus" / "registry.json"))
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    (tmp_path / "corpus").mkdir(parents=True, exist_ok=True)

    from knowledge_engine.mcp_server import Server

    server = Server()
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    # base tools preserved
    assert {"search", "registry_list", "registry_toggle", "registry_get"} <= names
    # project-docs capability tools merged in
    assert "project_docs.capabilities" in names
