"""Tests for the embedding MCP tool group.

Fully offline: in-memory project + registry DBs and the deterministic
``stub`` provider (resolved through the real ``embeddings.providers.get_provider``
factory by setting ``embeddings.provider = "stub"``). No network or external
model is touched.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db, paths
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.embeddings import index as emb_index
from knowledge_engine.project_docs.embeddings import providers as emb_providers
from knowledge_engine.project_docs.mcp_tools import base, embedding_tools

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
def _payload(result: dict) -> dict:
    """Parse the JSON payload from an MCP text result envelope."""
    return json.loads(result["content"][0]["text"])


def _project_db():
    """In-memory project DB with project-scoped migrations applied."""
    conn = db.connect(Path(":memory:"))
    db.apply_migrations(conn, only_prefixes=PROJECT_PREFIXES)
    return conn


def _registry_db():
    """In-memory registry DB with the registry migration applied."""
    conn = db.connect(Path(":memory:"))
    db.apply_migrations(conn, only_prefixes=("001_",))
    return conn


def _insert_doc(conn, record_id: str, body: str) -> None:
    """Insert a minimal project_docs row plus its body and FTS shadow rows."""
    conn.execute(
        """
        INSERT INTO project_docs
            (record_id, project_fp, branch_fp, category, content_hash,
             created_at, updated_at, summary)
        VALUES (?, 'proj_x', 'br_x', 'doc', ?, 'now', 'now', ?)
        """,
        (record_id, "hash_" + record_id, body[:40]),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body) VALUES (?, ?)",
        (record_id, body),
    )
    rowid = conn.execute(
        "SELECT rowid FROM project_docs WHERE record_id = ?", (record_id,)
    ).fetchone()["rowid"]
    conn.execute(
        "INSERT INTO project_docs_fts(rowid, searchable_body, summary) VALUES(?,?,?)",
        (rowid, body, body[:40]),
    )
    conn.commit()


def _ctx(cfg: ProjectDocsConfig, conn=None, tmp_path: Path | None = None) -> base.ToolContext:
    """Build a ToolContext whose default project resolves to ``conn``.

    The default project slug is derived from the root directory name (see
    ``ToolContext.project_slug_for(None)``), so we pre-seed ``_projects`` under
    that slug to inject the in-memory DB without touching disk.
    """
    root = tmp_path or Path.cwd()
    ctx = base.ToolContext(cfg=cfg, root=root)
    if conn is not None:
        slug = paths.slugify(root.name)
        ctx._projects[slug] = conn
    return ctx


def _enabled_cfg(allow_search: bool = True) -> ProjectDocsConfig:
    """Config with embeddings on (stub provider) and optional search permission."""
    cfg = ProjectDocsConfig()
    object.__setattr__(cfg.embeddings, "enabled", True)
    object.__setattr__(cfg.embeddings, "provider", "stub")
    object.__setattr__(cfg.mcp, "allow_embedding_search", allow_search)
    return cfg


# --------------------------------------------------------------------------- #
# Sanity: the stub provider really resolves through the frozen factory.
# --------------------------------------------------------------------------- #
def test_stub_provider_resolves_via_factory():
    cfg = _enabled_cfg()
    provider = emb_providers.get_provider(cfg)
    assert provider is not None
    assert provider.name == "stub"
    assert isinstance(provider.embed(["x"]), list)


# --------------------------------------------------------------------------- #
# embedding_status — always safe
# --------------------------------------------------------------------------- #
def test_embedding_status_disabled_by_default():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch("project_docs.embedding_status", {}, _ctx(cfg))
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["enabled"] is False
    assert "provider" in payload
    assert "model" in payload


def test_embedding_status_reports_indexed_count(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    provider = emb_providers.get_provider(cfg)
    emb_index.generate(conn, provider, record_ids=["d1"])
    result = embedding_tools.dispatch(
        "project_docs.embedding_status", {}, _ctx(cfg, conn, tmp_path)
    )
    payload = _payload(result)
    assert payload["enabled"] is True
    assert payload["indexed"] == 1
    assert payload["search_permitted"] is True


# --------------------------------------------------------------------------- #
# Gates: disabled / not_permitted
# --------------------------------------------------------------------------- #
def test_generate_embeddings_disabled_by_default():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch("project_docs.generate_embeddings", {}, _ctx(cfg))
    assert _payload(result)["status"] == "disabled"


def test_refresh_embeddings_disabled_by_default():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch("project_docs.refresh_embeddings", {}, _ctx(cfg))
    assert _payload(result)["status"] == "disabled"


def test_cluster_records_disabled_by_default():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch("project_docs.cluster_records", {}, _ctx(cfg))
    assert _payload(result)["status"] == "disabled"


def test_semantic_search_disabled_when_embeddings_off():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch(
        "project_docs.semantic_search", {"query": "alpha"}, _ctx(cfg)
    )
    assert _payload(result)["status"] == "disabled"


def test_semantic_search_not_permitted_when_search_disallowed(tmp_path):
    cfg = _enabled_cfg(allow_search=False)
    conn = _project_db()
    result = embedding_tools.dispatch(
        "project_docs.semantic_search", {"query": "alpha"}, _ctx(cfg, conn, tmp_path)
    )
    assert _payload(result)["status"] == "not_permitted"


def test_similar_records_not_permitted_when_search_disallowed(tmp_path):
    cfg = _enabled_cfg(allow_search=False)
    conn = _project_db()
    result = embedding_tools.dispatch(
        "project_docs.similar_records", {"record_id": "d1"}, _ctx(cfg, conn, tmp_path)
    )
    assert _payload(result)["status"] == "not_permitted"


# --------------------------------------------------------------------------- #
# Unknown project resolution
# --------------------------------------------------------------------------- #
def test_generate_embeddings_unknown_project(tmp_path):
    cfg = _enabled_cfg()
    # No conn seeded and a project_fp that the (empty) registry cannot resolve.
    ctx = base.ToolContext(cfg=cfg, root=tmp_path, _registry=_registry_db())
    result = embedding_tools.dispatch(
        "project_docs.generate_embeddings", {"project_fp": "proj_unknown"}, ctx
    )
    assert _payload(result)["status"] == "unknown_project"


# --------------------------------------------------------------------------- #
# Generate / refresh
# --------------------------------------------------------------------------- #
def test_generate_embeddings_embeds_docs(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    _insert_doc(conn, "d2", "beta document")
    result = embedding_tools.dispatch(
        "project_docs.generate_embeddings", {}, _ctx(cfg, conn, tmp_path)
    )
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["embedded"] == 2
    assert payload["total"] == 2


def test_refresh_embeddings_targets_subset(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    _insert_doc(conn, "d2", "beta document")
    result = embedding_tools.dispatch(
        "project_docs.refresh_embeddings",
        {"record_ids": ["d1"]},
        _ctx(cfg, conn, tmp_path),
    )
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["refreshed"] == 1


# --------------------------------------------------------------------------- #
# Semantic search / similar (happy path with a stub provider)
# --------------------------------------------------------------------------- #
def test_semantic_search_returns_results(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    _insert_doc(conn, "d2", "beta document")
    embedding_tools.dispatch(
        "project_docs.generate_embeddings", {}, _ctx(cfg, conn, tmp_path)
    )
    result = embedding_tools.dispatch(
        "project_docs.semantic_search",
        {"query": "alpha document"},
        _ctx(cfg, conn, tmp_path),
    )
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["count"] >= 1
    # The stub provider is deterministic: the exact-text match ranks first.
    assert payload["results"][0]["record_id"] == "d1"


def test_similar_records_returns_neighbours(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    _insert_doc(conn, "d2", "beta document")
    embedding_tools.dispatch(
        "project_docs.generate_embeddings", {}, _ctx(cfg, conn, tmp_path)
    )
    result = embedding_tools.dispatch(
        "project_docs.similar_records", {"record_id": "d1"}, _ctx(cfg, conn, tmp_path)
    )
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["record_id"] == "d1"
    # d1 itself is excluded from its own neighbour list.
    assert all(r["record_id"] != "d1" for r in payload["results"])


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #
def test_cluster_records_returns_clusters(tmp_path):
    cfg = _enabled_cfg()
    conn = _project_db()
    _insert_doc(conn, "d1", "alpha document")
    _insert_doc(conn, "d2", "beta document")
    _insert_doc(conn, "d3", "gamma notes")
    embedding_tools.dispatch(
        "project_docs.generate_embeddings", {}, _ctx(cfg, conn, tmp_path)
    )
    result = embedding_tools.dispatch(
        "project_docs.cluster_records", {"k": 3}, _ctx(cfg, conn, tmp_path)
    )
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["count"] >= 1
    members = [rid for c in payload["clusters"] for rid in c["record_ids"]]
    assert sorted(members) == ["d1", "d2", "d3"]


# --------------------------------------------------------------------------- #
# Tool definitions + dispatch surface
# --------------------------------------------------------------------------- #
def test_tools_advertises_all_six():
    cfg = ProjectDocsConfig()
    names = {t["name"] for t in embedding_tools.tools(cfg)}
    assert names == {
        "project_docs.embedding_status",
        "project_docs.generate_embeddings",
        "project_docs.refresh_embeddings",
        "project_docs.semantic_search",
        "project_docs.similar_records",
        "project_docs.cluster_records",
    }


def test_tool_defs_have_object_input_schema():
    cfg = ProjectDocsConfig()
    for tool in embedding_tools.tools(cfg):
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]


def test_group_constant():
    assert embedding_tools.GROUP == "embedding"


def test_unknown_tool_returns_status():
    cfg = ProjectDocsConfig()
    result = embedding_tools.dispatch("project_docs.bogus", {}, _ctx(cfg))
    assert _payload(result)["status"] == "unknown_tool"


# --------------------------------------------------------------------------- #
# Guard: confirm EmbeddingsCfg/McpCfg fields we rely on exist.
# --------------------------------------------------------------------------- #
def test_config_fields_present():
    cfg = ProjectDocsConfig()
    emb_fields = {f.name for f in dataclasses.fields(cfg.embeddings)}
    mcp_fields = {f.name for f in dataclasses.fields(cfg.mcp)}
    assert {"enabled", "provider", "model"} <= emb_fields
    assert "allow_embedding_search" in mcp_fields


# Quiet a possibly-unused import on some Python configs.
_ = pytest
