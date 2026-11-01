"""Migration 008: ``project_docs`` / ``project_docs_fts`` -> ``project_documents*``.

A 3.0/3.1 store is built by running the migrations up to 007 and writing
documents the way the pre-rename pipeline did (old table names). Migration 008
must keep every search result, leave a schema identical to a fresh database,
and keep the explicitly maintained FTS index usable.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from knowledge_engine.core.util import sqlite as ke_sqlite
from knowledge_engine.project_context import db, fingerprints, ingest, schema, search
from knowledge_engine.project_context.config import ProjectContextConfig

LEGACY_VERSION = 7
DOCS = (
    ("rec_alpha", "docs/alpha.md", "The harbour deployment runbook covers rollbacks."),
    ("rec_beta", "docs/beta.md", "Owl migration notes for the harbour service."),
    ("rec_gamma", "src/gamma.py", "Gamma module parses frobnication configs."),
)
_OLD_NAME_RE = re.compile(r"\bproject_docs(_fts)?\b")


def _migrations_through(version: int) -> list[ke_sqlite.Migration]:
    found = ke_sqlite.discover_migrations(db.MIGRATIONS_DIR)
    return [m for m in found if m.version <= version]


@contextlib.contextmanager
def _open(path: Path) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(db.connect(path)) as conn:
        yield conn


def _insert_legacy(conn: sqlite3.Connection, record_id: str, path: str, body: str) -> None:
    """Write one document under the pre-008 names, as the 3.1 pipeline did."""
    now = "2026-10-01T00:00:00Z"
    conn.execute(
        "INSERT INTO project_docs (record_id, project_fp, branch_fp, category,"
        " content_hash, created_at, updated_at, source_path, summary)"
        " VALUES (?, 'proj_x', 'br_main', 'doc', ?, ?, ?, ?, ?)",
        (record_id, f"h-{record_id}", now, now, path, body[:24]),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body) VALUES (?, ?)",
        (record_id, body),
    )
    conn.execute(
        "INSERT INTO doc_pointers (pointer_id, record_id, project_fp, branch_fp,"
        " content_hash, created_at) VALUES (?, ?, 'proj_x', 'br_main', 'h', ?)",
        (f"ptr-{record_id}", record_id, now),
    )
    rowid = conn.execute(
        "SELECT rowid FROM project_docs WHERE record_id = ?", (record_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO project_docs_fts (rowid, searchable_body, summary) VALUES (?, ?, ?)",
        (rowid, body, body[:24]),
    )


def _legacy_store(path: Path) -> None:
    with _open(path) as conn:
        ke_sqlite.apply_migrations(conn, _migrations_through(LEGACY_VERSION))
        for record_id, source_path, body in DOCS:
            _insert_legacy(conn, record_id, source_path, body)
        conn.commit()


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
        " WHERE name != 'sqlite_sequence' ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _hits(conn: sqlite3.Connection, query: str) -> list[tuple[str, str, float]]:
    return [
        (r["record_id"], r["snippet"], round(r["score"], 9))
        for r in search.search(conn, query, limit=10)
    ]


QUERIES = ("harbour", "frobnication", "owl OR rollbacks", "migration")


@pytest.fixture()
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "content.sqlite"
    _legacy_store(path)
    return path


def test_upgrade_keeps_results_and_matches_a_fresh_schema(legacy: Path, tmp_path: Path) -> None:
    with _open(legacy) as conn:
        assert db.document_tables(conn) == ("project_docs", "project_docs_fts")
        before = {q: _hits(conn, q) for q in QUERIES}
        applied = db.apply_project_migrations(conn)
        after = {q: _hits(conn, q) for q in QUERIES}
        upgraded = _schema(conn)
        version = db.schema_version(conn)
        tables = db.document_tables(conn)
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert applied == 1 and version == 8
    assert tables == ("project_documents", "project_documents_fts")
    assert all(before[q] for q in QUERIES)
    assert after == before
    assert fk_violations == [] and integrity == "ok"

    with _open(tmp_path / "fresh.sqlite") as fresh:
        db.apply_project_migrations(fresh)
        assert _schema(fresh) == upgraded

    names = {name for _, name, _, _ in upgraded}
    assert {
        "project_documents_fts_data",
        "project_documents_fts_idx",
        "project_documents_fts_docsize",
        "project_documents_fts_config",
    } <= names
    assert not any(name.startswith("project_docs") for name in names)
    # Child foreign keys, indexes and any trigger/view text follow the rename.
    assert [name for _, name, _, sql in upgraded if sql and _OLD_NAME_RE.search(sql)] == []


def test_fts_and_foreign_keys_stay_in_sync_after_upgrade(legacy: Path, tmp_path: Path) -> None:
    cfg = ProjectContextConfig()
    with _open(legacy) as conn, _open(tmp_path / "registry.sqlite") as registry:
        db.apply_project_migrations(conn)
        db.apply_registry_migrations(registry)
        pfp = fingerprints.ensure_project(registry, "/home/user/proj", "proj")
        bfp = fingerprints.ensure_branch(registry, pfp, "main")
        fields = {
            "project_fp": pfp,
            "branch_fp": bfp,
            "source_path": "docs/new.md",
            "category": schema.CATEGORY_DOC,
            "subtype": None,
            "text": "Lighthouse keepers rotate the harbour lamps nightly.",
            "cfg": cfg,
        }
        # Insert: the pipeline indexes a new document next to the migrated ones.
        record = ingest.ingest_record(conn, registry, **fields)
        assert record.ingestion_status == schema.INGESTED
        assert [h[0] for h in _hits(conn, "lighthouse")] == [record.record_id]
        assert len(_hits(conn, "harbour")) == 3
        # Unchanged content dedupes: no second row, no second FTS entry.
        again = ingest.ingest_record(conn, registry, **fields)
        assert again.ingestion_status == schema.SKIPPED_DEDUPE
        assert len(_hits(conn, "lighthouse")) == 1

        # Update: the contentless index is rewritten by rowid ('delete' + insert).
        rowid, body = conn.execute(
            "SELECT d.rowid, b.searchable_body FROM project_documents d"
            " JOIN project_doc_bodies b USING (record_id) WHERE d.record_id = 'rec_gamma'"
        ).fetchone()
        conn.execute(
            "INSERT INTO project_documents_fts (project_documents_fts, rowid, searchable_body,"
            " summary) VALUES ('delete', ?, ?, ?)",
            (rowid, body, body[:24]),
        )
        conn.execute(
            "INSERT INTO project_documents_fts (rowid, searchable_body, summary)"
            " VALUES (?, 'Gamma module parses widget configs.', '')",
            (rowid,),
        )
        assert _hits(conn, "frobnication") == []
        assert [h[0] for h in _hits(conn, "widget")] == ["rec_gamma"]

        # Delete: rewritten foreign keys still cascade to the child tables.
        conn.execute("DELETE FROM project_documents WHERE record_id = 'rec_beta'")
        conn.commit()
        orphans = [
            conn.execute(f"SELECT count(*) FROM {table} WHERE record_id = 'rec_beta'").fetchone()[0]
            for table in ("project_doc_bodies", "doc_pointers")
        ]
        indexed = {h[0] for h in _hits(conn, "harbour")}
    assert orphans == [0, 0]
    # A deleted row's stale FTS entry has nothing to join to, so it never surfaces.
    assert indexed == {"rec_alpha", record.record_id}


def test_rerun_is_idempotent(legacy: Path) -> None:
    with _open(legacy) as conn:
        first = db.apply_project_migrations(conn)
        snapshot = _schema(conn)
        second = db.apply_project_migrations(conn)
        third = ke_sqlite.apply_migrations(conn, _migrations_through(8))
        unchanged = _schema(conn)
        hits = _hits(conn, "harbour")
    assert (first, second, third) == (1, 0, 0)
    assert unchanged == snapshot
    assert {h[0] for h in hits} == {"rec_alpha", "rec_beta"}


def test_failed_rename_rolls_back_to_version_7(legacy: Path) -> None:
    with _open(legacy) as conn:
        conn.execute("DROP TABLE project_docs_fts")
        conn.commit()
        with pytest.raises(ke_sqlite.MigrationError):
            db.apply_project_migrations(conn)
        version = db.schema_version(conn)
        tables = db.document_tables(conn)
    # The first ALTER ran but was rolled back with the failing second one.
    assert version == LEGACY_VERSION
    assert tables[0] == "project_docs"
