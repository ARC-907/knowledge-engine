"""Tests for the project-docs full-text search module.

A fresh project DB (migrations 002-007) is created per test. Rows are inserted
directly into ``project_docs`` / ``project_doc_bodies`` and the contentless
``project_docs_fts`` index, mirroring the FTS contract used by the ingestion
pipeline, so the search layer can be exercised in isolation.
"""

from __future__ import annotations

import sqlite3

import pytest

from knowledge_engine.project_docs import search
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.db import apply_migrations, connect
from knowledge_engine.project_docs.schema import RESULT_FULL

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


@pytest.fixture()
def project_conn(tmp_path) -> sqlite3.Connection:
    """A project DB connection with the project-side migrations applied."""
    conn = connect(tmp_path / "proj.sqlite")
    apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    try:
        yield conn
    finally:
        conn.close()


def _insert(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    branch_fp: str = "br_main0000000000",
    category: str = "doc",
    source_path: str = "docs/readme.md",
    summary: str = "a summary",
    body: str = "hello world body",
    git_commit: str | None = None,
    created_at: str = "2026-05-30T00:00:00Z",
) -> None:
    """Insert one record across the three storage tables per the FTS contract."""
    conn.execute(
        "INSERT INTO project_docs "
        "(record_id, project_fp, branch_fp, category, content_hash, "
        " created_at, updated_at, source_path, summary, git_commit) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            record_id,
            "proj_test00000000",
            branch_fp,
            category,
            "deadbeef",
            created_at,
            created_at,
            source_path,
            summary,
            git_commit,
        ),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body) VALUES (?,?)",
        (record_id, body),
    )
    rowid = conn.execute(
        "SELECT rowid FROM project_docs WHERE record_id = ?", (record_id,)
    ).fetchone()["rowid"]
    conn.execute(
        "INSERT INTO project_docs_fts(rowid, searchable_body, summary) VALUES(?,?,?)",
        (rowid, body, summary),
    )
    conn.commit()


def _full_cfg() -> ProjectDocsConfig:
    cfg = ProjectDocsConfig()
    object.__setattr__(cfg.mcp, "allow_full_content", True)
    return cfg


def _gated_cfg() -> ProjectDocsConfig:
    cfg = ProjectDocsConfig()
    object.__setattr__(cfg.mcp, "allow_full_content", False)
    return cfg


class TestSearch:
    def test_query_matches(self, project_conn):
        _insert(project_conn, record_id="r1", body="the quick brown fox")
        results = search.search(project_conn, "quick")
        assert len(results) == 1
        result = results[0]
        assert result["record_id"] == "r1"
        assert result["category"] == "doc"
        # snippet key is always present; on a contentless FTS5 index the
        # original column text is not stored, so snippet() yields None.
        assert "snippet" in result
        assert "score" in result
        assert "body" not in result  # summary mode omits body

    def test_no_match_returns_empty(self, project_conn):
        _insert(project_conn, record_id="r1", body="alpha beta")
        assert search.search(project_conn, "gamma") == []

    def test_branch_filter_narrows(self, project_conn):
        _insert(project_conn, record_id="r1", branch_fp="br_aaaa", body="shared token")
        _insert(project_conn, record_id="r2", branch_fp="br_bbbb", body="shared token")
        results = search.search(project_conn, "shared", branch_fp="br_aaaa")
        assert [r["record_id"] for r in results] == ["r1"]

    def test_category_filter_narrows(self, project_conn):
        _insert(project_conn, record_id="r1", category="doc", body="shared token")
        _insert(project_conn, record_id="r2", category="devlog", body="shared token")
        results = search.search(project_conn, "shared", category="devlog")
        assert [r["record_id"] for r in results] == ["r2"]

    def test_since_filter_narrows(self, project_conn):
        _insert(
            project_conn,
            record_id="old",
            body="shared token",
            created_at="2026-01-01T00:00:00Z",
        )
        _insert(
            project_conn,
            record_id="new",
            body="shared token",
            created_at="2026-05-30T00:00:00Z",
        )
        results = search.search(project_conn, "shared", since="2026-05-01T00:00:00Z")
        assert [r["record_id"] for r in results] == ["new"]

    def test_git_commit_filter(self, project_conn):
        _insert(project_conn, record_id="r1", git_commit="abc123", body="shared token")
        _insert(project_conn, record_id="r2", git_commit="def456", body="shared token")
        results = search.search(project_conn, "shared", git_commit="abc123")
        assert [r["record_id"] for r in results] == ["r1"]

    def test_full_mode_not_permitted_without_cfg(self, project_conn):
        _insert(project_conn, record_id="r1", body="secret body text")
        results = search.search(project_conn, "secret", mode=RESULT_FULL, cfg=None)
        assert results[0]["body_status"] == "not_permitted"
        assert "body" not in results[0]

    def test_full_mode_gate_false(self, project_conn):
        _insert(project_conn, record_id="r1", body="secret body text")
        results = search.search(
            project_conn, "secret", mode=RESULT_FULL, cfg=_gated_cfg()
        )
        assert results[0]["body_status"] == "not_permitted"

    def test_full_mode_gate_true_returns_body(self, project_conn):
        _insert(project_conn, record_id="r1", body="secret body text")
        results = search.search(
            project_conn, "secret", mode=RESULT_FULL, cfg=_full_cfg()
        )
        assert results[0]["body"] == "secret body text"
        assert "body_status" not in results[0]

    def test_limit(self, project_conn):
        for i in range(5):
            _insert(project_conn, record_id=f"r{i}", body="shared token")
        results = search.search(project_conn, "shared", limit=2)
        assert len(results) == 2


class TestGetRecord:
    def test_returns_record(self, project_conn):
        _insert(project_conn, record_id="r1", summary="my summary")
        result = search.get_record(project_conn, "r1")
        assert result is not None
        assert result["record_id"] == "r1"
        assert result["summary"] == "my summary"
        assert "body" not in result

    def test_missing_returns_none(self, project_conn):
        assert search.get_record(project_conn, "nope") is None

    def test_full_mode_gated(self, project_conn):
        _insert(project_conn, record_id="r1", body="the body")
        gated = search.get_record(project_conn, "r1", mode=RESULT_FULL)
        assert gated is not None
        assert gated["body_status"] == "not_permitted"
        ok = search.get_record(project_conn, "r1", mode=RESULT_FULL, cfg=_full_cfg())
        assert ok is not None
        assert ok["body"] == "the body"


class TestConvenienceWrappers:
    def test_search_by_branch(self, project_conn):
        _insert(project_conn, record_id="r1", branch_fp="br_aaaa")
        _insert(project_conn, record_id="r2", branch_fp="br_bbbb")
        results = search.search_by_branch(project_conn, "br_aaaa")
        assert [r["record_id"] for r in results] == ["r1"]

    def test_search_by_type(self, project_conn):
        _insert(project_conn, record_id="r1", category="doc")
        _insert(project_conn, record_id="r2", category="devlog")
        results = search.search_by_type(project_conn, "devlog")
        assert [r["record_id"] for r in results] == ["r2"]

    def test_search_by_path(self, project_conn):
        _insert(project_conn, record_id="r1", source_path="a.md")
        _insert(project_conn, record_id="r2", source_path="b.md")
        results = search.search_by_path(project_conn, "b.md")
        assert [r["record_id"] for r in results] == ["r2"]

    def test_search_recent_orders_newest_first(self, project_conn):
        _insert(project_conn, record_id="old", created_at="2026-01-01T00:00:00Z")
        _insert(project_conn, record_id="new", created_at="2026-05-30T00:00:00Z")
        results = search.search_recent(project_conn)
        assert [r["record_id"] for r in results] == ["new", "old"]
