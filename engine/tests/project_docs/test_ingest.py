"""Tests for the project-docs direct ingestion pipeline."""

from __future__ import annotations

import sqlite3

import pytest

from knowledge_engine.project_docs import config, db, fingerprints, ingest, schema


@pytest.fixture()
def registry_conn(tmp_path) -> sqlite3.Connection:
    """A fresh registry DB with only the 001_ migration applied."""
    conn = db.connect(tmp_path / "registry.sqlite")
    db.apply_migrations(conn, only_prefixes=("001_",))
    return conn


@pytest.fixture()
def project_conn(tmp_path) -> sqlite3.Connection:
    """A fresh project content DB with the 002_–007_ migrations applied."""
    conn = db.connect(tmp_path / "project.sqlite")
    db.apply_migrations(
        conn, only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_")
    )
    return conn


@pytest.fixture()
def context(registry_conn: sqlite3.Connection) -> tuple[str, str]:
    """An ensured (project_fp, branch_fp) pair in the registry."""
    pfp = fingerprints.ensure_project(registry_conn, "/home/user/proj", "proj")
    bfp = fingerprints.ensure_branch(registry_conn, pfp, "main")
    return pfp, bfp


def test_ingest_one_doc_writes_row_and_is_searchable(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context
    cfg = config.load_config()

    record = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=pfp,
        branch_fp=bfp,
        source_path="docs/widget.md",
        category=schema.CATEGORY_DOC,
        subtype=None,
        text="The widget module handles the frobnication of gadgets.",
        cfg=cfg,
    )

    assert record.ingestion_status == schema.INGESTED
    assert record.pointer_id.startswith("ke-doc://doc/")

    rows = project_conn.execute("SELECT * FROM project_docs").fetchall()
    assert len(rows) == 1
    assert rows[0]["record_id"] == record.record_id

    body = project_conn.execute(
        "SELECT searchable_body, raw_body FROM project_doc_bodies WHERE record_id = ?",
        (record.record_id,),
    ).fetchone()
    assert "frobnication" in body["searchable_body"]
    assert body["raw_body"] is None  # retain_raw_content defaults False

    # FTS finds it through the contentless JOIN MATCH.
    hits = project_conn.execute(
        "SELECT pd.* FROM project_docs_fts f "
        "JOIN project_docs pd ON pd.rowid = f.rowid "
        "WHERE project_docs_fts MATCH ? ORDER BY bm25(project_docs_fts) LIMIT ?",
        ("frobnication", 10),
    ).fetchall()
    assert len(hits) == 1
    assert hits[0]["record_id"] == record.record_id


def test_reingest_identical_skips_dedupe(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context
    cfg = config.load_config()
    text = "Identical content for dedupe."

    first = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=pfp,
        branch_fp=bfp,
        source_path="docs/dup.md",
        category=schema.CATEGORY_DOC,
        subtype=None,
        text=text,
        cfg=cfg,
    )
    assert first.ingestion_status == schema.INGESTED

    second = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=pfp,
        branch_fp=bfp,
        source_path="docs/dup.md",
        category=schema.CATEGORY_DOC,
        subtype=None,
        text=text,
        cfg=cfg,
    )
    assert second.ingestion_status == schema.SKIPPED_DEDUPE
    assert second.record_id == first.record_id

    count = project_conn.execute("SELECT COUNT(*) AS c FROM project_docs").fetchone()["c"]
    assert count == 1
    fts_count = project_conn.execute(
        "SELECT COUNT(*) AS c FROM project_docs_fts"
    ).fetchone()["c"]
    assert fts_count == 1


def test_validate_context_mismatch_raises(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, _ = context
    cfg = config.load_config()
    bogus_branch_fp = "br_neverallocated00"

    with pytest.raises(fingerprints.ContextError):
        ingest.ingest_record(
            project_conn,
            registry_conn,
            project_fp=pfp,
            branch_fp=bogus_branch_fp,
            source_path="docs/x.md",
            category=schema.CATEGORY_DOC,
            subtype=None,
            text="some text",
            cfg=cfg,
        )
    # Nothing was written.
    count = project_conn.execute("SELECT COUNT(*) AS c FROM project_docs").fetchone()["c"]
    assert count == 0


def test_docstring_category_uses_docstring_pointer(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context
    cfg = config.load_config()

    record = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=pfp,
        branch_fp=bfp,
        source_path="src/mod.py:func",
        category=schema.CATEGORY_DOCSTRING,
        subtype="function",
        text="Return the sum of two numbers.",
        cfg=cfg,
    )
    assert record.pointer_id.startswith("ke-doc://docstring/")


def test_rejected_secret_stores_no_body_no_fts(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context
    cfg = config.load_config()
    # An oversize document is rejected by the sanitizer.
    big_text = "x" * (cfg.ingestion.max_document_bytes + 1)

    record = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=pfp,
        branch_fp=bfp,
        source_path="big.txt",
        category=schema.CATEGORY_DOC,
        subtype=None,
        text=big_text,
        cfg=cfg,
    )
    assert record.ingestion_status == schema.REJECTED
    assert record.sanitization_status in schema.REJECTED_STATES

    # Metadata row exists, but no body and no FTS entry.
    docs = project_conn.execute("SELECT COUNT(*) AS c FROM project_docs").fetchone()["c"]
    assert docs == 1
    bodies = project_conn.execute(
        "SELECT COUNT(*) AS c FROM project_doc_bodies"
    ).fetchone()["c"]
    assert bodies == 0
    fts = project_conn.execute(
        "SELECT COUNT(*) AS c FROM project_docs_fts"
    ).fetchone()["c"]
    assert fts == 0


def test_begin_and_finish_run(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context

    run_id = ingest.begin_run(
        project_conn, registry_conn, pfp, bfp, schema.MODE_INGEST
    )
    assert run_id.startswith("run_")

    row = project_conn.execute(
        "SELECT * FROM project_doc_ingestion_runs WHERE ingestion_run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["status"] == schema.PENDING
    assert row["mode"] == schema.MODE_INGEST

    ingest.finish_run(
        project_conn, run_id, {"docs_seen": 3, "docs_written": 2}, status="completed"
    )
    row = project_conn.execute(
        "SELECT * FROM project_doc_ingestion_runs WHERE ingestion_run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert '"docs_seen": 3' in row["stats_json"]


def test_begin_run_rejects_unknown_mode(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    context: tuple[str, str],
) -> None:
    pfp, bfp = context
    with pytest.raises(ValueError):
        ingest.begin_run(project_conn, registry_conn, pfp, bfp, "bogus_mode")
