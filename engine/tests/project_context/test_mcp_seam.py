"""Tests for the MCP tool-discovery seam and server merge.

Verifies the existing 4 base tools are preserved and the project-context capability
tools are merged in and answer without error when features are off.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge_engine.project_context.config import ProjectContextConfig
from knowledge_engine.project_context.mcp_tools import collect_tools
from knowledge_engine.project_context.mcp_tools.base import ToolContext


def _ctx(tmp_path: Path, cfg: ProjectContextConfig) -> ToolContext:
    return ToolContext(cfg=cfg, root=tmp_path)


def test_collect_tools_disabled_when_mcp_off() -> None:
    cfg = ProjectContextConfig(mcp=ProjectContextConfig().mcp.__class__(enabled=False))
    defs, dispatch = collect_tools(cfg)
    assert defs == []
    assert dispatch == {}


def test_capability_tools_present_and_safe(tmp_path: Path) -> None:
    cfg = ProjectContextConfig()  # mcp.enabled defaults True
    defs, dispatch = collect_tools(cfg)
    names = {d["name"] for d in defs}
    assert "project_context.capabilities" in names
    assert "project_context.config_status" in names
    assert "project_context.healthcheck" in names

    res = dispatch["project_context.capabilities"](
        "project_context.capabilities", {}, _ctx(tmp_path, cfg)
    )
    payload = json.loads(res["content"][0]["text"])
    assert payload["scanner_enabled"] is False
    assert payload["embeddings_enabled"] is False
    assert payload["source_mutation_allowed"] is False


def test_healthcheck_never_raises(tmp_path: Path) -> None:
    cfg = ProjectContextConfig()
    _, dispatch = collect_tools(cfg)
    res = dispatch["project_context.healthcheck"](
        "project_context.healthcheck", {}, _ctx(tmp_path, cfg)
    )
    payload = json.loads(res["content"][0]["text"])
    assert payload["registry"] == "ok"
    assert payload["project_count"] == 0


def test_server_merges_without_breaking_base(tmp_path: Path, monkeypatch) -> None:
    # Point the base engine at temp dirs so the MCP server builds cleanly.
    monkeypatch.setenv("KE_CORPUS_ROOT", str(tmp_path / "corpus"))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(tmp_path / "corpus" / "registry.json"))
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    (tmp_path / "corpus").mkdir(parents=True, exist_ok=True)

    import anyio

    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.mcp.server import build_server
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()  # Config.from_env() never touches the filesystem.
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    server = build_server(config, registry, Indexer(config, registry))
    names = {t.name for t in anyio.run(server.list_tools)}
    # base tools preserved
    assert {"search", "registry_list", "registry_toggle", "registry_get"} <= names
    # project-context capability tools merged in
    assert "project_context.capabilities" in names


def test_tool_context_opens_one_registry_connection_under_concurrency(tmp_path: Path) -> None:
    import threading

    from knowledge_engine.project_context import db as pddb

    cfg = ProjectContextConfig()
    ctx = _ctx(tmp_path, cfg)
    opened: list[object] = []
    real_connect = pddb.connect
    barrier = threading.Barrier(8)

    def counting_connect(path: object, **kwargs: object):  # type: ignore[no-untyped-def]
        conn = real_connect(path, **kwargs)  # type: ignore[arg-type]
        opened.append(conn)
        return conn

    pddb.connect = counting_connect  # type: ignore[assignment]
    try:
        seen: list[object] = []

        def worker() -> None:
            barrier.wait()
            seen.append(ctx.registry_conn())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        pddb.connect = real_connect  # type: ignore[assignment]
        ctx.close()
    assert len(opened) == 1
    assert len({id(conn) for conn in seen}) == 1


def test_tool_groups_share_the_mcp_envelopes() -> None:
    from knowledge_engine.agent_board.mcp_tools import base as board_base
    from knowledge_engine.mcp import tool_groups, toolkit
    from knowledge_engine.project_context.mcp_tools import base as pc_base

    assert pc_base.text_result is tool_groups.text_result is board_base.text_result
    assert pc_base.status_result is board_base.status_result is toolkit.status_result
    assert board_base.error_result is toolkit.error_result
    assert json.loads(tool_groups.status_result("off", why="x")["content"][0]["text"]) == {
        "status": "off",
        "why": "x",
    }
