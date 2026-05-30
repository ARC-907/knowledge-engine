"""Tests for schema constants, models, and hashing."""

from __future__ import annotations

from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.hashing import content_hash, short_hash
from knowledge_engine.project_docs.models import DocRecord, Pointer, TestRun


def test_schema_membership() -> None:
    assert "test_log" in schema.CATEGORIES
    assert "docstring" in schema.CATEGORIES
    assert schema.REJECTED_OVERSIZE in schema.REJECTED_STATES
    assert "pass" in schema.TEST_CLASSIFICATIONS
    assert "docstring" in schema.POINTER_TYPES
    assert "report" in schema.SCAN_MODES


def test_hashing_deterministic() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(short_hash("abc")) == 8
    assert len(short_hash("abc", 12)) == 12


def test_docrecord_roundtrip() -> None:
    rec = DocRecord(
        record_id="r1", project_fp="proj_x", branch_fp="br_y", category="doc",
        content_hash="h", created_at="t", updated_at="t", summary="s", source_path="a.md",
    )
    rebuilt = DocRecord.from_row(rec.to_row())
    assert rebuilt == rec


def test_from_row_ignores_extra_keys() -> None:
    row = {"id": "t1", "project_fp": "p", "branch_fp": "b", "started_at": "now",
           "joined_extra": "ignored"}
    tr = TestRun.from_row(row)
    assert tr.id == "t1"
    assert tr.classification == "unknown"


def test_pointer_roundtrip() -> None:
    p = Pointer(pointer_id="ptr", record_id="r1", content_hash="h", created_at="t",
                project_fp="proj_x", branch_fp="br_y", ptype="docstring")
    assert Pointer.from_row(p.to_row()) == p
