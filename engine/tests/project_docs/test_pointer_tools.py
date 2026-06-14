"""Tests for the pointer MCP tool module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_engine.project_docs import db as pddb
from knowledge_engine.project_docs import pointers
from knowledge_engine.project_docs.hashing import content_hash
from knowledge_engine.project_docs.mcp_tools import base, pointer_tools

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")
PROJECT_FP = "proj_aaaaaaaaaaaaaaaa"
BRANCH_FP = "br_bbbbbbbbbbbbbbbb"
RECORD_ID = "rec_0001"
BODY_TEXT = "Example documentation body for pointer resolution."


# ── Lightweight config doubles for the full-content gate ──────────────


@dataclass
class _Mcp:
    allow_full_content: bool = False
    default_result_mode: str = "summary"


@dataclass
class _Cfg:
    mcp: _Mcp


def _payload(result: dict) -> object:
    """Decode the JSON text payload from an MCP content envelope."""
    return json.loads(result["content"][0]["text"])


def _seed_project_db(path: Path) -> tuple[object, str]:
    """Create a project DB, seed one doc + body + pointer, return (conn, uri)."""
    conn = pddb.connect(path)
    pddb.apply_migrations(conn, only_prefixes=PROJECT_PREFIXES)

    chash = content_hash(BODY_TEXT)
    conn.execute(
        """
        INSERT INTO project_docs (
            record_id, project_fp, branch_fp, source_path, category,
            content_hash, sanitization_status, ingestion_status,
            created_at, updated_at, summary
        ) VALUES (?, ?, ?, ?, 'doc', ?, 'sanitized', 'ingested',
                  datetime('now'), datetime('now'), ?)
        """,
        (
            RECORD_ID,
            PROJECT_FP,
            BRANCH_FP,
            "docs/example.md",
            chash,
            "A short summary of the example doc.",
        ),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body, raw_body) "
        "VALUES (?, ?, NULL)",
        (RECORD_ID, BODY_TEXT),
    )
    conn.commit()

    uri = pointers.allocate(
        conn,
        RECORD_ID,
        "doc",
        PROJECT_FP,
        BRANCH_FP,
        "docs/example.md",
        {"start": 1, "end": 10},
        chash,
    )
    return conn, uri


class _FixedContext(base.ToolContext):
    """ToolContext returning one pre-built connection for the seeded project."""

    def __init__(self, cfg, root, conn):
        super().__init__(cfg=cfg, root=root)
        self._fixed = conn

    def project_conn(self, project_fp=None):  # type: ignore[override]
        if project_fp is not None and project_fp != PROJECT_FP:
            return None
        return self._fixed


def _make_ctx(tmp_path: Path, conn, *, allow_full: bool = False) -> _FixedContext:
    cfg = _Cfg(_Mcp(allow_full_content=allow_full))
    return _FixedContext(cfg, tmp_path, conn)


@pytest.fixture()
def seeded(tmp_path):
    """A migrated, seeded project connection plus its allocated pointer URI."""
    conn, uri = _seed_project_db(tmp_path / "project.sqlite")
    try:
        yield tmp_path, conn, uri
    finally:
        conn.close()


def test_group_constant() -> None:
    assert pointer_tools.GROUP == "pointer"


def test_tool_defs_shape() -> None:
    defs = pointer_tools.tools(_Cfg(_Mcp()))
    names = {d["name"] for d in defs}
    assert names == {
        "project_docs.resolve_pointer",
        "project_docs.list_pointers",
        "project_docs.validate_pointer",
        "project_docs.pointer_backrefs",
    }
    for d in defs:
        assert d["inputSchema"]["type"] == "object"
        assert "properties" in d["inputSchema"]


def test_resolve_pointer_returns_summary(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.resolve_pointer", {"uri": uri}, ctx
    )
    payload = _payload(result)
    assert payload["record_id"] == RECORD_ID
    assert payload["pointer_id"] == uri
    assert payload["summary"] == "A short summary of the example doc."
    # Summary mode must NOT leak the full body.
    assert "content" not in payload
    assert "raw_content" not in payload


def test_resolve_pointer_full_blocked_when_gate_off(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn, allow_full=False)
    result = pointer_tools.dispatch(
        "project_docs.resolve_pointer", {"uri": uri, "mode": "full"}, ctx
    )
    payload = _payload(result)
    assert payload["status"] == "not_permitted"


def test_resolve_pointer_full_allowed_when_gate_on(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn, allow_full=True)
    result = pointer_tools.dispatch(
        "project_docs.resolve_pointer", {"uri": uri, "mode": "full"}, ctx
    )
    payload = _payload(result)
    assert payload["record_id"] == RECORD_ID
    assert "status" not in payload
    assert payload["content"] == BODY_TEXT


def test_resolve_pointer_not_found(seeded) -> None:
    tmp_path, conn, _uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    missing = pointers.format_pointer("doc", PROJECT_FP, BRANCH_FP, "rec_missing")
    result = pointer_tools.dispatch(
        "project_docs.resolve_pointer", {"uri": missing}, ctx
    )
    payload = _payload(result)
    assert payload["status"] == "not_found"


def test_resolve_pointer_invalid_uri(seeded) -> None:
    tmp_path, conn, _uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.resolve_pointer", {"uri": "not-a-pointer"}, ctx
    )
    payload = _payload(result)
    assert payload["status"] == "invalid_uri"


def test_validate_pointer_bogus_uri(seeded) -> None:
    tmp_path, conn, _uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.validate_pointer", {"uri": "not-a-pointer"}, ctx
    )
    payload = _payload(result)
    assert payload["valid"] is False
    assert payload["exists"] is False
    assert payload["content_hash_match"] is False


def test_validate_pointer_valid_grammar_missing_record(seeded) -> None:
    tmp_path, conn, _uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    missing = pointers.format_pointer("doc", PROJECT_FP, BRANCH_FP, "rec_missing")
    result = pointer_tools.dispatch(
        "project_docs.validate_pointer", {"uri": missing}, ctx
    )
    payload = _payload(result)
    assert payload["valid"] is True
    assert payload["exists"] is False


def test_validate_pointer_existing(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.validate_pointer", {"uri": uri}, ctx
    )
    payload = _payload(result)
    assert payload["valid"] is True
    assert payload["exists"] is True
    assert payload["content_hash_match"] is True


def test_list_pointers(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch("project_docs.list_pointers", {}, ctx)
    payload = _payload(result)
    assert isinstance(payload, list)
    assert any(p["pointer_id"] == uri for p in payload)


def test_pointer_backrefs_empty(seeded) -> None:
    tmp_path, conn, uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.pointer_backrefs", {"pointer_id": uri}, ctx
    )
    payload = _payload(result)
    assert payload == []


def test_unknown_project_returns_status(seeded) -> None:
    tmp_path, conn, _uri = seeded
    ctx = _make_ctx(tmp_path, conn)
    result = pointer_tools.dispatch(
        "project_docs.list_pointers", {"project_fp": "proj_does_not_exist"}, ctx
    )
    payload = _payload(result)
    assert payload["status"] == "unknown_project"
