"""Tests for the git/lineage MCP tool module.

These tests are offline: they never invoke real git over the network and rely on
config gates rather than the presence of a repository. The key behaviors:

* ``project_docs.git_context`` returns ``disabled`` when ``git.enabled`` is False.
* ``project_docs.search_by_diff`` is disabled by default (diff summaries off).
* ``project_docs.search_by_commit`` returns a clean empty list against an empty
  project DB.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db as pddb
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.mcp_tools import git_tools
from knowledge_engine.project_docs.mcp_tools.base import ToolContext

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


def _payload(result: dict) -> object:
    """Decode the JSON object embedded in an MCP text-content envelope."""
    return json.loads(result["content"][0]["text"])


def _enabled_cfg() -> ProjectDocsConfig:
    """A config with git enabled but diff summaries still off (the default)."""
    base = ProjectDocsConfig()
    return replace(base, enabled=True, git=replace(base.git, enabled=True))


def _ctx(cfg: ProjectDocsConfig, tmp_path: Path, project_conn=None) -> ToolContext:
    """Build a ToolContext, optionally pre-wiring a project connection so the
    tools never touch the real filesystem registry."""
    ctx = ToolContext(cfg=cfg, root=tmp_path)
    if project_conn is not None:
        ctx._projects[ctx.project_slug_for(None)] = project_conn
    return ctx


def _project_conn(tmp_path: Path):
    """Create an in-memory-style project DB on disk with migrations applied."""
    conn = pddb.connect(tmp_path / "proj.sqlite")
    pddb.apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    return conn


# ── group contract ───────────────────────────────────────────────────


def test_group_name() -> None:
    assert git_tools.GROUP == "git"


def test_tools_listed() -> None:
    names = {t["name"] for t in git_tools.tools(_enabled_cfg())}
    assert names == {
        "project_docs.git_context",
        "project_docs.search_by_commit",
        "project_docs.get_branch_lineage",
        "project_docs.get_change_context",
        "project_docs.explain_file_history",
        "project_docs.search_by_diff",
    }


# ── gating ───────────────────────────────────────────────────────────


def test_git_context_disabled_when_git_off(tmp_path: Path) -> None:
    cfg = ProjectDocsConfig(enabled=True)  # git.enabled defaults True...
    cfg = replace(cfg, git=replace(cfg.git, enabled=False))
    ctx = _ctx(cfg, tmp_path)
    result = git_tools.dispatch("project_docs.git_context", {}, ctx)
    assert _payload(result)["status"] == "disabled"


def test_search_by_diff_disabled_by_default(tmp_path: Path) -> None:
    # git enabled, but include_diff_summaries is off (the conservative default).
    cfg = _enabled_cfg()
    assert cfg.git.include_diff_summaries is False
    ctx = _ctx(cfg, tmp_path)
    result = git_tools.dispatch(
        "project_docs.search_by_diff", {"from_ref": "HEAD~1", "to_ref": "HEAD"}, ctx
    )
    assert _payload(result)["status"] == "disabled"


def test_diff_gated_tools_disabled_when_git_off(tmp_path: Path) -> None:
    cfg = ProjectDocsConfig(enabled=True)
    cfg = replace(cfg, git=replace(cfg.git, enabled=False))
    ctx = _ctx(cfg, tmp_path)
    for tool in (
        "project_docs.get_branch_lineage",
        "project_docs.get_change_context",
        "project_docs.explain_file_history",
        "project_docs.search_by_diff",
    ):
        result = git_tools.dispatch(tool, {"from_ref": "a", "path": "x", "commit": "y"}, ctx)
        assert _payload(result)["status"] == "disabled", tool


# ── search_by_commit on an empty project DB ──────────────────────────


def test_search_by_commit_empty_project(tmp_path: Path) -> None:
    conn = _project_conn(tmp_path)
    ctx = _ctx(_enabled_cfg(), tmp_path, project_conn=conn)
    result = git_tools.dispatch(
        "project_docs.search_by_commit", {"commit": "deadbeef"}, ctx
    )
    payload = _payload(result)
    assert payload == []


def test_search_by_commit_unknown_project_returns_empty(tmp_path: Path) -> None:
    # An unknown project_fp -> project_conn returns None -> clean empty list.
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch(
        "project_docs.search_by_commit",
        {"commit": "abc123", "project_fp": "proj_doesnotexist0"},
        ctx,
    )
    assert _payload(result) == []


def test_search_by_commit_requires_commit(tmp_path: Path) -> None:
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_docs.search_by_commit", {}, ctx)
    assert _payload(result)["status"] == "invalid_args"


# ── unknown tool ─────────────────────────────────────────────────────


def test_unknown_tool(tmp_path: Path) -> None:
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_docs.nope", {}, ctx)
    assert _payload(result)["status"] == "unknown_tool"


# ── git-absent degradation (mocked, offline) ─────────────────────────


def test_git_context_not_configured_when_not_repo(tmp_path: Path, monkeypatch) -> None:
    # Force "not a repo" without touching real git.
    monkeypatch.setattr(git_tools.gitctx, "_is_repo", lambda root: False)
    monkeypatch.setattr(git_tools.gitctx, "collect", lambda root, cfg: None)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_docs.git_context", {}, ctx)
    assert _payload(result)["status"] == "not_configured"


def test_branch_lineage_not_configured_when_not_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(git_tools.gitctx, "_is_repo", lambda root: False)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_docs.get_branch_lineage", {"base": "main"}, ctx)
    assert _payload(result)["status"] == "not_configured"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
