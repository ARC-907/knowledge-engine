"""Tests for the pointer URI grammar, allocation, and resolution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db, pointers
from knowledge_engine.project_docs.schema import (
    POINTER_DOC,
    POINTER_DOCSTRING,
)

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")
PROJECT_FP = "proj_abc123def4560000"
BRANCH_FP = "br_0011223344556677"
RECORD_ID = "rec_0001"
CONTENT_HASH = "a" * 64


# ── Minimal config doubles for the full-content gate ──────────────────


@dataclass
class _Mcp:
    allow_full_content: bool


@dataclass
class _Cfg:
    mcp: _Mcp


@pytest.fixture()
def project_conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated project DB with one stored doc record + searchable body."""
    conn = db.connect(tmp_path / "project.sqlite")
    db.apply_migrations(conn, only_prefixes=PROJECT_PREFIXES)
    conn.execute(
        """
        INSERT INTO project_docs (
            record_id, project_fp, branch_fp, category, content_hash,
            created_at, updated_at, summary, sanitization_status,
            ingestion_run_id, git_commit, source_path
        ) VALUES (?, ?, ?, 'doc', ?, datetime('now'), datetime('now'),
                  'A short summary.', 'sanitized', 'run_1', 'deadbeef',
                  'src/example.py')
        """,
        (RECORD_ID, PROJECT_FP, BRANCH_FP, CONTENT_HASH),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body, raw_body) "
        "VALUES (?, ?, NULL)",
        (RECORD_ID, "The full searchable body text."),
    )
    conn.commit()
    return conn


# ── Grammar ───────────────────────────────────────────────────────────


def test_format_parse_round_trip() -> None:
    uri = pointers.format_pointer(POINTER_DOC, PROJECT_FP, BRANCH_FP, RECORD_ID)
    assert uri.startswith("ke-doc://doc/project/")
    parsed = pointers.parse_pointer(uri)
    assert parsed == {
        "scheme": "ke-doc",
        "ptype": POINTER_DOC,
        "project_fp": PROJECT_FP,
        "branch_fp": BRANCH_FP,
        "kind": "doc",
        "record_id": RECORD_ID,
    }


def test_docstring_type_round_trips_through_canonical_form() -> None:
    uri = pointers.format_pointer(POINTER_DOCSTRING, PROJECT_FP, BRANCH_FP, RECORD_ID)
    parsed = pointers.parse_pointer(uri)
    assert parsed["ptype"] == POINTER_DOCSTRING
    # docstring profile uses the "doc" kind segment.
    assert parsed["kind"] == POINTER_DOC


def test_ke_docstring_alias_parses_to_docstring() -> None:
    alias = (
        f"KE-DOCSTRING://project/{PROJECT_FP}/branch/{BRANCH_FP}/doc/{RECORD_ID}"
    )
    parsed = pointers.parse_pointer(alias)
    assert parsed["scheme"] == "ke-doc"
    assert parsed["ptype"] == POINTER_DOCSTRING
    assert parsed["project_fp"] == PROJECT_FP
    assert parsed["branch_fp"] == BRANCH_FP
    assert parsed["record_id"] == RECORD_ID


def test_scheme_is_case_insensitive() -> None:
    uri = pointers.format_pointer(POINTER_DOC, PROJECT_FP, BRANCH_FP, RECORD_ID)
    upper = uri.replace("ke-doc://", "KE-DOC://")
    assert pointers.parse_pointer(upper)["ptype"] == POINTER_DOC


def test_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        pointers.parse_pointer("not-a-pointer")
    with pytest.raises(ValueError):
        pointers.parse_pointer("ke-doc://doc/wrong/shape")


# ── Allocate + resolve ────────────────────────────────────────────────


def test_allocate_then_resolve_returns_summary(project_conn: sqlite3.Connection) -> None:
    pid = pointers.allocate(
        project_conn,
        RECORD_ID,
        POINTER_DOC,
        PROJECT_FP,
        BRANCH_FP,
        "src/example.py",
        [10, 25],
        CONTENT_HASH,
    )
    assert pid == pointers.format_pointer(POINTER_DOC, PROJECT_FP, BRANCH_FP, RECORD_ID)

    resolved = pointers.resolve(project_conn, pid)
    assert resolved is not None
    assert resolved["summary"] == "A short summary."
    assert resolved["source_path"] == "src/example.py"
    assert resolved["source_span"] == [10, 25]
    assert resolved["project_fp"] == PROJECT_FP
    assert resolved["branch_fp"] == BRANCH_FP
    assert resolved["git_commit"] == "deadbeef"
    assert resolved["ingestion_run_id"] == "run_1"
    assert resolved["sanitization_status"] == "sanitized"
    assert resolved["related"] == []
    # Summary mode never leaks the full body.
    assert "content" not in resolved


def test_resolve_full_content_is_gated(project_conn: sqlite3.Connection) -> None:
    pid = pointers.allocate(
        project_conn, RECORD_ID, POINTER_DOC, PROJECT_FP, BRANCH_FP, None, None, CONTENT_HASH
    )

    # Gate off -> no body even in full mode.
    denied = pointers.resolve(project_conn, pid, mode="full", cfg=_Cfg(_Mcp(False)))
    assert denied is not None
    assert "content" not in denied

    # No cfg at all -> still no body.
    no_cfg = pointers.resolve(project_conn, pid, mode="full")
    assert "content" not in no_cfg

    # Gate on + full mode -> body included.
    allowed = pointers.resolve(project_conn, pid, mode="full", cfg=_Cfg(_Mcp(True)))
    assert allowed["content"] == "The full searchable body text."


def test_resolve_missing_pointer_returns_none(project_conn: sqlite3.Connection) -> None:
    uri = pointers.format_pointer(POINTER_DOC, PROJECT_FP, BRANCH_FP, "rec_missing")
    assert pointers.resolve(project_conn, uri) is None


# ── list / validate / backrefs ────────────────────────────────────────


def test_list_pointers(project_conn: sqlite3.Connection) -> None:
    pointers.allocate(
        project_conn, RECORD_ID, POINTER_DOC, PROJECT_FP, BRANCH_FP, None, None, CONTENT_HASH
    )
    all_rows = pointers.list_pointers(project_conn)
    assert len(all_rows) == 1
    assert all_rows[0]["record_id"] == RECORD_ID

    filtered = pointers.list_pointers(project_conn, record_id=RECORD_ID)
    assert len(filtered) == 1
    assert pointers.list_pointers(project_conn, record_id="rec_other") == []


def test_validate_pointer_missing_is_valid_grammar_but_not_exists(
    project_conn: sqlite3.Connection,
) -> None:
    uri = pointers.format_pointer(POINTER_DOC, PROJECT_FP, BRANCH_FP, "rec_never")
    result = pointers.validate_pointer(project_conn, uri)
    assert result == {"valid": True, "exists": False, "content_hash_match": False}


def test_validate_pointer_existing_matches_hash(project_conn: sqlite3.Connection) -> None:
    pid = pointers.allocate(
        project_conn, RECORD_ID, POINTER_DOC, PROJECT_FP, BRANCH_FP, None, None, CONTENT_HASH
    )
    result = pointers.validate_pointer(project_conn, pid)
    assert result == {"valid": True, "exists": True, "content_hash_match": True}


def test_validate_pointer_bad_grammar() -> None:
    # A connectionless grammar check: malformed URI -> all False.
    result = pointers.validate_pointer(_NullConn(), "garbage")
    assert result == {"valid": False, "exists": False, "content_hash_match": False}


def test_pointer_backrefs(project_conn: sqlite3.Connection) -> None:
    pid = pointers.allocate(
        project_conn, RECORD_ID, POINTER_DOC, PROJECT_FP, BRANCH_FP, None, None, CONTENT_HASH
    )
    assert pointers.pointer_backrefs(project_conn, pid) == []
    project_conn.execute(
        "INSERT INTO pointer_backrefs (pointer_id, ref_source_path, ref_span_json, created_at) "
        "VALUES (?, 'src/other.py', '[1, 2]', datetime('now'))",
        (pid,),
    )
    project_conn.commit()
    refs = pointers.pointer_backrefs(project_conn, pid)
    assert len(refs) == 1
    assert refs[0]["ref_source_path"] == "src/other.py"


class _NullConn:
    """A connection stand-in that should never be queried (grammar fails first)."""

    def execute(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("validate_pointer queried the DB on bad grammar")
