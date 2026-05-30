"""Tests for the DB factory and migration runner."""

from __future__ import annotations

from pathlib import Path

from knowledge_engine.project_docs.db import apply_migrations, connect, schema_version


def test_registry_only_migration(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fp.sqlite")
    n = apply_migrations(conn, only_prefixes=("001_",))
    assert n == 1
    assert schema_version(conn) == 1
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "projects" in tables
    assert "branches" in tables
    # Project tables must NOT be present in the registry DB.
    assert "project_docs" not in tables


def test_project_migrations(tmp_path: Path) -> None:
    conn = connect(tmp_path / "proj.sqlite")
    n = apply_migrations(conn, only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_"))
    assert n == 6
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )}
    for expected in ("project_docs", "project_docs_fts", "test_runs",
                     "doc_pointers", "git_context", "doc_embeddings"):
        assert expected in names


def test_migrations_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "proj.sqlite")
    first = apply_migrations(conn, only_prefixes=("002_", "003_"))
    second = apply_migrations(conn, only_prefixes=("002_", "003_"))
    assert first == 2
    assert second == 0
