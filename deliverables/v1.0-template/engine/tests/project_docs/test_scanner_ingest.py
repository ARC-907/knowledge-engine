"""Tests for scanner Mode 2 (ingest)."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db, fingerprints
from knowledge_engine.project_docs.config import ProjectDocsConfig, ScannerCfg
from knowledge_engine.project_docs.scanner import ingest as scanner_ingest
from knowledge_engine.project_docs.scanner.validators import GateError

MIGRATIONS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "knowledge_engine"
    / "project_docs"
    / "migrations"
)

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


def _registry(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "registry.sqlite")
    db.apply_migrations(conn, MIGRATIONS, only_prefixes=("001_",))
    return conn


def _project(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "project.sqlite")
    db.apply_migrations(conn, MIGRATIONS, only_prefixes=PROJECT_PREFIXES)
    return conn


def _ctx(registry: sqlite3.Connection, root: Path):
    canonical = str(root).replace("\\", "/").lower()
    pfp = fingerprints.ensure_project(registry, canonical, "demo")
    bfp = fingerprints.ensure_branch(registry, pfp, "main")
    return pfp, bfp


def _enabled_cfg() -> ProjectDocsConfig:
    """Return a config with the scanner gate flipped on."""
    return dataclasses.replace(ProjectDocsConfig(), scanner=ScannerCfg(enabled=True))


def test_ingest_disabled_raises_gate_error(tmp_path: Path) -> None:
    """The default config keeps the scanner off; ingest must refuse."""
    registry = _registry(tmp_path)
    project = _project(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    pfp, bfp = _ctx(registry, root)
    cfg = ProjectDocsConfig()  # scanner.enabled is False by default.

    with pytest.raises(GateError):
        scanner_ingest.run(
            root,
            cfg,
            project,
            registry,
            project_fp=pfp,
            branch_fp=bfp,
        )


def test_ingest_markdown_writes_row_and_fts(tmp_path: Path) -> None:
    """With the scanner enabled, a markdown file lands in the DB + FTS."""
    registry = _registry(tmp_path)
    project = _project(tmp_path)

    root = tmp_path / "proj"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "intro.md").write_text(
        "# Intro\n\nThe quick brown fox jumps over the lazy dog.\n",
        encoding="utf-8",
    )

    pfp, bfp = _ctx(registry, root)
    cfg = _enabled_cfg()

    stats = scanner_ingest.run(
        root,
        cfg,
        project,
        registry,
        project_fp=pfp,
        branch_fp=bfp,
    )

    assert stats["candidates"] >= 1
    assert stats["ingested"] >= 1
    assert stats["run_id"]

    # A project_docs row exists for the markdown file.
    row = project.execute(
        "SELECT record_id FROM project_docs WHERE source_path = ?",
        ("docs/intro.md",),
    ).fetchone()
    assert row is not None

    # FTS finds the body using the shared join contract.
    hit = project.execute(
        "SELECT pd.record_id FROM project_docs_fts f "
        "JOIN project_docs pd ON pd.rowid = f.rowid "
        "WHERE project_docs_fts MATCH ? "
        "ORDER BY bm25(project_docs_fts) LIMIT ?",
        ("brown", 5),
    ).fetchone()
    assert hit is not None
    assert hit[0] == row[0]

    # The run was recorded and closed.
    run_row = project.execute(
        "SELECT status FROM project_doc_ingestion_runs WHERE ingestion_run_id = ?",
        (stats["run_id"],),
    ).fetchone()
    assert run_row is not None
    assert run_row[0] == "completed"
