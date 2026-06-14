"""Dataclasses for project-docs records.

Field names match the SQLite columns in the migrations so ``to_row()`` /
``from_row()`` round-trip cleanly. ``from_row`` tolerates extra keys (e.g. a
joined query) and missing optional keys, so the same helper works for full-row
reads and partial projections.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping


def _from_mapping(cls: type, row: Mapping[str, Any]) -> Any:
    """Build a dataclass from a mapping, ignoring unknown keys and defaulting
    anything absent."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


@dataclass
class DocRecord:
    record_id: str
    project_fp: str
    branch_fp: str
    category: str
    content_hash: str
    created_at: str
    updated_at: str
    pointer_id: str | None = None
    project_name: str = ""
    branch_name: str = ""
    source_path: str = ""
    source_uri: str | None = None
    subtype: str = ""
    sanitized_content_hash: str | None = None
    raw_retained: int = 0
    sanitization_status: str = "sanitized"
    ingestion_status: str = "ingested"
    source_modified_at: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty_json: str | None = None
    summary: str = ""
    ingestion_run_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DocRecord":
        return _from_mapping(cls, row)


@dataclass
class IngestionRun:
    ingestion_run_id: str
    project_fp: str
    branch_fp: str
    mode: str
    started_at: str
    finished_at: str | None = None
    stats_json: str = "{}"
    status: str = "running"

    def to_row(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "IngestionRun":
        return _from_mapping(cls, row)


@dataclass
class TestRun:
    id: str
    project_fp: str
    branch_fp: str
    started_at: str
    command: str = ""
    framework: str | None = None
    target: str | None = None
    exit_code: int | None = None
    classification: str = "unknown"
    duration_ms: int | None = None
    git_commit: str | None = None
    git_dirty_json: str | None = None
    summary: str = ""
    failure_summary: str = ""
    raw_retained: int = 0

    def to_row(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TestRun":
        return _from_mapping(cls, row)


# Prevent pytest from collecting the TestRun *dataclass* as a test class.
TestRun.__test__ = False


@dataclass
class LogRecord:
    """Build/runtime log record (shared shape for build_log_records and
    runtime_log_records)."""

    id: str
    project_fp: str
    branch_fp: str
    started_at: str
    command: str = ""
    exit_code: int | None = None
    classification: str = "unknown"
    duration_ms: int | None = None
    git_commit: str | None = None
    summary: str = ""
    sanitized_log: str = ""
    raw_log: str | None = None
    raw_retained: int = 0

    def to_row(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LogRecord":
        return _from_mapping(cls, row)


@dataclass
class Pointer:
    pointer_id: str
    record_id: str
    content_hash: str
    created_at: str
    project_fp: str = ""
    branch_fp: str = ""
    scheme: str = "ke-doc"
    ptype: str = "doc"
    source_path: str | None = None
    source_span_json: str | None = None
    status: str = "active"

    def to_row(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Pointer":
        return _from_mapping(cls, row)


@dataclass
class Candidate:
    """A scanner-discovered ingestion candidate (in-memory only; not stored)."""

    source_path: str
    category: str
    subtype: str = ""
    est_bytes: int = 0
    risk_flags: tuple[str, ...] = ()
    span: tuple[int, int] | None = None
    preview: str = ""
    detector: str = ""


@dataclass
class ScanReport:
    """Result of a report-only scan (in-memory only; not stored)."""

    root: str
    mode: str
    candidates: list[Candidate]
    git_available: bool = False
    recommended_actions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def total_bytes(self) -> int:
        return sum(c.est_bytes for c in self.candidates)


@dataclass
class GitContext:
    branch: str | None
    commit_hash: str | None
    dirty: bool
    remote_hash: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class DiffSummary:
    from_ref: str | None
    to_ref: str | None
    files_changed: int
    insertions: int
    deletions: int
    summary: str = ""


@dataclass
class EmbeddingMeta:
    record_id: str
    provider: str
    model: str
    dim: int
    created_at: str
