"""Tests for the DB factory and migration runner."""

from __future__ import annotations

from pathlib import Path

from knowledge_engine.project_context.db import apply_migrations, connect, schema_version


def test_registry_only_migration(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fp.sqlite")
    n = apply_migrations(conn, only_prefixes=("001_",))
    assert n == 1
    assert schema_version(conn) == 1
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "projects" in tables
    assert "branches" in tables
    # Project tables must NOT be present in the registry DB.
    assert "project_documents" not in tables


def test_project_migrations(tmp_path: Path) -> None:
    conn = connect(tmp_path / "proj.sqlite")
    n = apply_migrations(
        conn, only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_", "008_")
    )
    assert n == 7
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    for expected in (
        "project_documents",
        "project_documents_fts",
        "test_runs",
        "doc_pointers",
        "git_context",
        "doc_embeddings",
    ):
        assert expected in names


def test_migrations_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "proj.sqlite")
    first = apply_migrations(conn, only_prefixes=("002_", "003_"))
    second = apply_migrations(conn, only_prefixes=("002_", "003_"))
    assert first == 2
    assert second == 0


def test_migration_sets_are_discovered_by_directory(tmp_path: Path) -> None:
    from knowledge_engine.project_context import db

    registry = connect(tmp_path / "fp.sqlite")
    project = connect(tmp_path / "proj.sqlite")
    try:
        registry_applied = db.apply_registry_migrations(registry)
        project_applied = db.apply_project_migrations(project)
        reapplied = db.apply_project_migrations(project)
        assert (registry_applied, project_applied, reapplied) == (1, 7, 0)
        registry_tables = {
            r["name"] for r in registry.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "projects" in registry_tables and "project_documents" not in registry_tables
        versions = {r[0] for r in project.execute("SELECT version FROM schema_version")}
        assert versions == {2, 3, 4, 5, 6, 7, 8}
        # A new NNN_*.sql dropped next to the others is picked up with no code change.
        assert all(p.name[:3].isdigit() for p in db.MIGRATIONS_DIR.glob("*.sql"))
        assert [p.name for p in db.REGISTRY_MIGRATIONS_DIR.glob("*.sql")] == [
            "001_project_fingerprints.sql"
        ]
    finally:
        registry.close()
        project.close()


def test_existing_registry_and_project_dbs_are_not_reapplied(tmp_path: Path) -> None:
    """DBs migrated by the old flat-directory runner keep their versions."""
    from knowledge_engine.project_context import db

    conn = connect(tmp_path / "old.sqlite")
    try:
        apply_migrations(
            conn, only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_", "008_")
        )
        reapplied = db.apply_project_migrations(conn)
        assert reapplied == 0
    finally:
        conn.close()
