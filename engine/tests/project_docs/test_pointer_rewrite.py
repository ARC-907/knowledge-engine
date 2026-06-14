"""Tests for the pointer plan/apply/rollback source-rewrite flow.

These tests are fully offline. They build in-memory project and registry DBs via
the frozen migrations, write a real source file into a tmp dir, and exercise the
plan (read-only), apply (gated), and rollback (byte-for-byte restore) paths. No
git or network access is used.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db, fingerprints, ingest, pointers
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.scanner import pointer_apply, pointer_plan
from knowledge_engine.project_docs.schema import CATEGORY_DOCSTRING

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")

SOURCE = '''def greet(name):
    """This is a long docstring.

    It spans several lines and is a good candidate for replacement with a
    short pointer back to the canonical knowledge base.
    """
    return f"hello {name}"
'''

# The docstring literal spans source lines 2-6 (1-based inclusive).
DOC_SPAN = (2, 6)


def _project_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.apply_migrations(conn, only_prefixes=PROJECT_PREFIXES)
    return conn


def _registry_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.apply_migrations(conn, only_prefixes=("001_",))
    return conn


def _context(registry: sqlite3.Connection, root: Path) -> tuple[str, str]:
    """Register a project + branch and return ``(project_fp, branch_fp)``."""
    project_fp = fingerprints.ensure_project(registry, str(root), "sample")
    branch_fp = fingerprints.ensure_branch(registry, project_fp, "main")
    return project_fp, branch_fp


def _write_source(tmp_path: Path) -> Path:
    path = tmp_path / "mod.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def _docstring_body() -> str:
    return "\n".join(SOURCE.splitlines()[DOC_SPAN[0] - 1 : DOC_SPAN[1]])


def _ingest_docstring(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    *,
    project_fp: str,
    branch_fp: str,
) -> str:
    """Ingest the sample docstring as a record; return its record id."""
    cfg = ProjectDocsConfig()
    rec = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=project_fp,
        branch_fp=branch_fp,
        source_path="mod.py",
        category=CATEGORY_DOCSTRING,
        subtype="docstring",
        text=_docstring_body(),
        cfg=cfg,
    )
    # Record the span in provenance (project_docs has no span column); the plan
    # reads the span from project_doc_provenance.source_span_json.
    project_conn.execute(
        "INSERT INTO project_doc_provenance "
        "(record_id, ingestion_run_id, detector, source_path, source_span_json, notes) "
        "VALUES (?, NULL, 'docstring', 'mod.py', ?, NULL)",
        (rec.record_id, json.dumps([DOC_SPAN[0], DOC_SPAN[1]])),
    )
    project_conn.commit()
    return rec.record_id


def _all_gates_on() -> ProjectDocsConfig:
    """Return a config with every pointer-apply gate open.

    The config dataclasses are frozen, so the open-gate variant is built with
    :func:`dataclasses.replace` rather than in-place mutation.
    """
    base = ProjectDocsConfig()
    pointer = replace(
        base.scanner.pointer_replacement,
        enabled=True,
        allow_source_mutation=True,
        write_backups=True,
    )
    scanner = replace(
        base.scanner, enabled=True, dry_run=False, pointer_replacement=pointer
    )
    return replace(base, scanner=scanner)


def test_plan_makes_no_source_changes(tmp_path):
    """Planning must not touch the source file (bytes before == after)."""
    project = _project_db()
    registry = _registry_db()
    project_fp, branch_fp = _context(registry, tmp_path)
    src = _write_source(tmp_path)
    before = src.read_bytes()
    _ingest_docstring(project, registry, project_fp=project_fp, branch_fp=branch_fp)

    cfg = ProjectDocsConfig()
    result = pointer_plan.run(
        str(tmp_path), cfg, project, project_fp=project_fp, branch_fp=branch_fp
    )

    assert result["plan_id"]
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["target_file"] == "mod.py"
    assert item["span"] == [2, 6]
    assert item["proposed_pointer"].startswith("ke-doc://docstring/")
    assert item["replacement_preview"].startswith('"""See ke-doc://docstring/')
    assert item["replacement_preview"].endswith('."""')
    assert item["reversible"] is True
    # Source untouched.
    assert src.read_bytes() == before
    # Plan row persisted as a dry run.
    row = project.execute(
        "SELECT dry_run FROM pointer_rewrite_plans WHERE plan_id = ?",
        (result["plan_id"],),
    ).fetchone()
    assert row["dry_run"] == 1


def test_apply_blocked_without_gates(tmp_path):
    """Apply with default (closed) gates returns not_permitted and writes nothing."""
    project = _project_db()
    registry = _registry_db()
    project_fp, branch_fp = _context(registry, tmp_path)
    src = _write_source(tmp_path)
    before = src.read_bytes()
    _ingest_docstring(project, registry, project_fp=project_fp, branch_fp=branch_fp)

    cfg = ProjectDocsConfig()
    plan = pointer_plan.run(
        str(tmp_path), cfg, project, project_fp=project_fp, branch_fp=branch_fp
    )
    result = pointer_apply.run(
        plan["plan_id"],
        str(tmp_path),
        cfg,
        project,
        project_fp=project_fp,
        branch_fp=branch_fp,
        confirm=False,
        registry_conn=registry,
    )

    assert result["status"] == "not_permitted"
    assert src.read_bytes() == before
    count = project.execute(
        "SELECT COUNT(*) AS n FROM pointer_rewrite_events"
    ).fetchone()["n"]
    assert count == 0


def test_apply_missing_confirm_blocked(tmp_path):
    """All config gates on but confirm=False is still blocked."""
    project = _project_db()
    registry = _registry_db()
    project_fp, branch_fp = _context(registry, tmp_path)
    _write_source(tmp_path)
    _ingest_docstring(project, registry, project_fp=project_fp, branch_fp=branch_fp)

    cfg = _all_gates_on()
    plan = pointer_plan.run(
        str(tmp_path), cfg, project, project_fp=project_fp, branch_fp=branch_fp
    )
    result = pointer_apply.run(
        plan["plan_id"],
        str(tmp_path),
        cfg,
        project,
        project_fp=project_fp,
        branch_fp=branch_fp,
        confirm=False,
        registry_conn=registry,
    )
    assert result["status"] == "not_permitted"
    assert "confirm" in result["reason"]


def test_apply_replaces_span_and_rollback_restores(tmp_path):
    """With all gates + confirm, apply replaces the span, backs up, and rolls back."""
    project = _project_db()
    registry = _registry_db()
    project_fp, branch_fp = _context(registry, tmp_path)
    src = _write_source(tmp_path)
    original_bytes = src.read_bytes()
    _ingest_docstring(project, registry, project_fp=project_fp, branch_fp=branch_fp)

    cfg = _all_gates_on()
    plan = pointer_plan.run(
        str(tmp_path), cfg, project, project_fp=project_fp, branch_fp=branch_fp
    )
    result = pointer_apply.run(
        plan["plan_id"],
        str(tmp_path),
        cfg,
        project,
        project_fp=project_fp,
        branch_fp=branch_fp,
        confirm=True,
        registry_conn=registry,
    )

    assert result["status"] == "applied"
    assert len(result["applied"]) == 1, result
    applied = result["applied"][0]
    pointer_id = applied["pointer_id"]

    # Span replaced with the stub pointer.
    new_text = src.read_text(encoding="utf-8")
    assert "This is a long docstring" not in new_text
    assert '"""See ke-doc://docstring/' in new_text
    # 5 docstring lines collapsed to a single stub line.
    assert len(new_text.splitlines()) < len(SOURCE.splitlines())

    # Backup exists and holds the original content.
    backup_path = applied["backup_path"]
    assert backup_path is not None
    assert Path(backup_path).read_bytes() == original_bytes

    # Pointer resolves and is marked applied.
    resolved = pointers.resolve(project, pointer_id)
    assert resolved is not None
    prow = project.execute(
        "SELECT status FROM doc_pointers WHERE pointer_id = ?", (pointer_id,)
    ).fetchone()
    assert prow["status"] == "applied"

    # An apply audit event row exists.
    apply_events = project.execute(
        "SELECT COUNT(*) AS n FROM pointer_rewrite_events WHERE action = 'apply'"
    ).fetchone()["n"]
    assert apply_events == 1

    # Rollback restores the original bytes exactly.
    rb = pointer_apply.rollback(plan["plan_id"], project)
    assert rb["status"] == "rolled_back"
    assert len(rb["restored"]) == 1
    assert src.read_bytes() == original_bytes

    # Rollback audit event recorded and pointer marked rolled_back.
    rollback_events = project.execute(
        "SELECT COUNT(*) AS n FROM pointer_rewrite_events WHERE action = 'rollback'"
    ).fetchone()["n"]
    assert rollback_events == 1
    prow_after = project.execute(
        "SELECT status FROM doc_pointers WHERE pointer_id = ?", (pointer_id,)
    ).fetchone()
    assert prow_after["status"] == "rolled_back"


def test_apply_unknown_plan(tmp_path):
    """Applying an unknown plan id (gates open) returns unknown_plan."""
    project = _project_db()
    registry = _registry_db()
    project_fp, branch_fp = _context(registry, tmp_path)
    _write_source(tmp_path)
    cfg = _all_gates_on()
    result = pointer_apply.run(
        "no-such-plan",
        str(tmp_path),
        cfg,
        project,
        project_fp=project_fp,
        branch_fp=branch_fp,
        confirm=True,
        registry_conn=registry,
    )
    assert result["status"] == "unknown_plan"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
