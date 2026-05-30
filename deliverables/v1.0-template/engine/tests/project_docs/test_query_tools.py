"""Tests for the project-docs query MCP tools.

These exercise the full read seam: a project DB is seeded through the real
ingestion pipeline (under the slug that ``ToolContext.project_conn(None)``
resolves to), then the query tools are dispatched against it. They verify that
search finds the seeded row, that bodies stay hidden by default, and that
``get_full_content`` is gated by ``mcp.allow_full_content``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db as pddb
from knowledge_engine.project_docs import fingerprints, ingest, paths, schema
from knowledge_engine.project_docs.config import McpCfg, ProjectDocsConfig
from knowledge_engine.project_docs.mcp_tools import query_tools
from knowledge_engine.project_docs.mcp_tools.base import ToolContext

_BRANCH = "main"
_BODY = "alpha beta gamma searchable widget documentation body"


def _cfg(*, allow_full_content: bool = False) -> ProjectDocsConfig:
    """Build a frozen config with MCP enabled and the full-content gate set.

    ``ProjectDocsConfig`` and its nested sections are ``frozen=True`` dataclasses,
    so tests construct rather than mutate them.
    """
    return ProjectDocsConfig(
        enabled=True,
        mcp=McpCfg(enabled=True, allow_full_content=allow_full_content),
    )


def _payload(result: dict) -> dict:
    """Decode the JSON text envelope returned by a tool dispatch."""
    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    """A ToolContext whose current project DB has one ingested doc record."""
    cfg = _cfg()
    root = tmp_path

    # The context opens DBs but does not create their parent dirs; the registry
    # DB and the per-project content DBs live under different parents, so create
    # both before any connection is opened.
    paths.project_db_path(root, cfg, "x").parent.mkdir(parents=True, exist_ok=True)
    paths.fingerprint_db_path(root, cfg).parent.mkdir(parents=True, exist_ok=True)

    context = ToolContext(cfg=cfg, root=root)

    # Resolve the slug/fp the way the context will, so seeding matches lookup.
    slug = paths.slugify(root.name)
    project_fp = fingerprints.project_fp(paths.canonical_root(root))
    branch_fp = fingerprints.branch_fp(project_fp, _BRANCH)

    # Register identity in the registry DB so validate_context passes, and name
    # the project after the slug so project_slug_for(fp) round-trips too.
    registry = context.registry_conn()
    fingerprints.ensure_project(registry, paths.canonical_root(root), slug)
    fingerprints.ensure_branch(registry, project_fp, _BRANCH)

    # Build the project DB at exactly the path the context will open.
    project = pddb.connect(paths.project_db_path(root, cfg, slug))
    pddb.apply_migrations(
        project,
        only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_"),
    )
    run_id = ingest.begin_run(project, registry, project_fp, branch_fp, schema.MODE_INGEST)
    record = ingest.ingest_record(
        project,
        registry,
        project_fp=project_fp,
        branch_fp=branch_fp,
        source_path="docs/overview.md",
        category=schema.CATEGORY_DOC,
        subtype="markdown",
        text=_BODY,
        cfg=cfg,
        run_id=run_id,
    )
    ingest.finish_run(project, run_id, {"docs_written": 1})
    project.close()

    context.record_id = record.record_id  # type: ignore[attr-defined]
    context.branch_fp = branch_fp  # type: ignore[attr-defined]
    return context


def test_tools_listed() -> None:
    cfg = ProjectDocsConfig()
    names = {t["name"] for t in query_tools.tools(cfg)}
    assert "project_docs.search" in names
    assert "project_docs.get_full_content" in names
    assert len(names) == 8


def test_search_finds_seeded_row(ctx: ToolContext) -> None:
    out = query_tools.dispatch("project_docs.search", {"query": "alpha"}, ctx)
    payload = _payload(out)
    assert payload["count"] == 1
    assert payload["results"][0]["record_id"] == ctx.record_id  # type: ignore[attr-defined]
    # Compact by default: summary mode never includes a body.
    assert "body" not in payload["results"][0]


def test_get_record_returns_summary(ctx: ToolContext) -> None:
    out = query_tools.dispatch(
        "project_docs.get_record", {"record_id": ctx.record_id}, ctx  # type: ignore[attr-defined]
    )
    payload = _payload(out)
    assert payload["record_id"] == ctx.record_id  # type: ignore[attr-defined]
    assert "body" not in payload


def test_get_record_not_found(ctx: ToolContext) -> None:
    out = query_tools.dispatch("project_docs.get_record", {"record_id": "nope"}, ctx)
    assert _payload(out)["status"] == "not_found"


def test_get_full_content_gated(ctx: ToolContext) -> None:
    out = query_tools.dispatch(
        "project_docs.get_full_content", {"record_id": ctx.record_id}, ctx  # type: ignore[attr-defined]
    )
    assert _payload(out)["status"] == "not_permitted"


def test_get_full_content_allowed(ctx: ToolContext) -> None:
    ctx.cfg = replace(ctx.cfg, mcp=McpCfg(enabled=True, allow_full_content=True))
    out = query_tools.dispatch(
        "project_docs.get_full_content", {"record_id": ctx.record_id}, ctx  # type: ignore[attr-defined]
    )
    payload = _payload(out)
    assert payload["body"] == _BODY


def test_search_by_type_and_branch(ctx: ToolContext) -> None:
    by_type = _payload(
        query_tools.dispatch(
            "project_docs.search_by_type", {"category": schema.CATEGORY_DOC}, ctx
        )
    )
    assert by_type["count"] == 1

    by_branch = _payload(
        query_tools.dispatch(
            "project_docs.search_by_branch",
            {"branch_fp": ctx.branch_fp},  # type: ignore[attr-defined]
            ctx,
        )
    )
    assert by_branch["count"] == 1


def test_search_recent_and_by_path(ctx: ToolContext) -> None:
    recent = _payload(query_tools.dispatch("project_docs.search_recent", {}, ctx))
    assert recent["count"] == 1

    by_path = _payload(
        query_tools.dispatch(
            "project_docs.search_by_path", {"source_path": "docs/overview.md"}, ctx
        )
    )
    assert by_path["count"] == 1


def test_unknown_project_returns_status(ctx: ToolContext) -> None:
    out = query_tools.dispatch(
        "project_docs.search", {"query": "alpha", "project_fp": "proj_doesnotexist"}, ctx
    )
    assert _payload(out)["status"] == "unknown_project"
