"""Ingestion pipeline for the project-context subsystem.

This module is the single write path into a project content DB. The public
entry points are:

* :func:`begin_run` / :func:`finish_run` — bracket a batch of ingestions with a
  ``project_doc_ingestion_runs`` row.
* :func:`ingest_record` — run one piece of text through the full pipeline
  (validate context → sanitize → hash → dedupe → write rows → update FTS) and
  return the resulting :class:`DocRecord`.

The pipeline is conservative by config: sanitization can be toggled
(``ingestion.sanitize_before_write``), raw bodies are only retained when
explicitly enabled (``ingestion.retain_raw_content``), and a record whose text
is rejected by the sanitizer is recorded with no body and no full-text index
entry. See section 7 of the design spec for the canonical pipeline.

The FTS contract is shared verbatim with the search module:
``project_docs_fts`` is a contentless ``fts5(searchable_body, summary)`` whose
``rowid`` mirrors ``project_context.rowid``; rows are inserted explicitly after the
``project_docs`` row is written.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from ..core.util.time import utc_now_iso
from . import fingerprints, hashing, pointers, sanitize, schema
from .models import DocRecord

#: Default summary length when no summarizer is supplied.
_DEFAULT_SUMMARY_CHARS = 200

#: Columns written to ``project_docs`` from a :class:`DocRecord`.
_DOC_COLUMNS = (
    "record_id",
    "pointer_id",
    "project_fp",
    "branch_fp",
    "project_name",
    "branch_name",
    "source_path",
    "source_uri",
    "category",
    "subtype",
    "content_hash",
    "sanitized_content_hash",
    "raw_retained",
    "sanitization_status",
    "ingestion_status",
    "created_at",
    "updated_at",
    "source_modified_at",
    "git_commit",
    "git_branch",
    "git_dirty_json",
    "summary",
    "ingestion_run_id",
)


def _pointer_type(category: str) -> str:
    """Map a document category to its pointer type.

    Docstring records use the ``docstring`` pointer profile; everything else
    uses the generic ``doc`` profile.
    """
    return schema.POINTER_DOCSTRING if category == schema.CATEGORY_DOCSTRING else schema.POINTER_DOC


def begin_run(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    project_fp: str,
    branch_fp: str,
    mode: str,
) -> str:
    """Open an ingestion run and return its id.

    Validates the project/branch context against the registry first, then writes
    a ``project_doc_ingestion_runs`` row in the ``pending`` state. ``mode`` must
    be one of :data:`schema.SCAN_MODES`.
    """
    fingerprints.validate_context(registry_conn, project_fp, branch_fp)
    if mode not in schema.SCAN_MODES:
        raise ValueError(f"unknown scan mode: {mode!r}")

    run_id = "run_" + uuid.uuid4().hex
    project_conn.execute(
        "INSERT INTO project_doc_ingestion_runs "
        "(ingestion_run_id, project_fp, branch_fp, mode, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, project_fp, branch_fp, mode, utc_now_iso(), schema.PENDING),
    )
    project_conn.commit()
    return run_id


def finish_run(
    project_conn: sqlite3.Connection,
    run_id: str,
    stats: dict,
    status: str = "completed",
) -> None:
    """Close an ingestion run, recording stats and a finish timestamp.

    ``stats`` is stored as JSON in ``stats_json``; callers typically pass
    counters such as ``docs_seen`` / ``docs_written`` / ``docs_skipped`` /
    ``docs_rejected``.
    """
    project_conn.execute(
        "UPDATE project_doc_ingestion_runs SET "
        "finished_at = ?, status = ?, stats_json = ? WHERE ingestion_run_id = ?",
        (utc_now_iso(), status, json.dumps(stats or {}), run_id),
    )
    project_conn.commit()


def _find_duplicate(
    project_conn: sqlite3.Connection,
    content_hash: str,
    source_path: str,
    branch_fp: str,
) -> DocRecord | None:
    """Return an existing record matching the dedupe key, or ``None``.

    The dedupe key is ``(content_hash, source_path, branch_fp)`` per the design
    spec, section 7.
    """
    row = project_conn.execute(
        "SELECT * FROM project_docs "
        "WHERE content_hash = ? AND source_path = ? AND branch_fp = ? LIMIT 1",
        (content_hash, source_path, branch_fp),
    ).fetchone()
    return DocRecord.from_row(row) if row is not None else None


def _insert_doc(project_conn: sqlite3.Connection, record: DocRecord) -> None:
    """Insert a ``project_docs`` row from a record."""
    row = record.to_row()
    columns = ", ".join(_DOC_COLUMNS)
    placeholders = ", ".join("?" for _ in _DOC_COLUMNS)
    project_conn.execute(
        f"INSERT INTO project_docs ({columns}) VALUES ({placeholders})",
        tuple(row[col] for col in _DOC_COLUMNS),
    )


def _insert_fts(
    project_conn: sqlite3.Connection,
    record_id: str,
    searchable_body: str,
    summary: str,
) -> None:
    """Index a record in the contentless FTS table, mirroring its rowid."""
    rowid = project_conn.execute(
        "SELECT rowid FROM project_docs WHERE record_id = ?", (record_id,)
    ).fetchone()[0]
    project_conn.execute(
        "INSERT INTO project_docs_fts(rowid, searchable_body, summary) VALUES (?, ?, ?)",
        (rowid, searchable_body, summary),
    )


def _sanitized(text: str, cfg) -> tuple[str, str]:
    """``(clean_text, sanitization_status)`` per ``ingestion.sanitize_before_write``."""
    if cfg.ingestion.sanitize_before_write:
        result = sanitize.sanitize(text, cfg)
        return result.text, result.status
    return text, schema.SANITIZED


def _write_rejected(
    project_conn: sqlite3.Connection,
    *,
    text: str,
    sanitization_status: str,
    fields: dict,
) -> DocRecord:
    """Rejected text: a metadata-only row, no body, no FTS entry."""
    record = DocRecord(
        record_id=uuid.uuid4().hex,
        content_hash=hashing.content_hash(text),
        sanitization_status=sanitization_status,
        ingestion_status=schema.REJECTED,
        **fields,
    )
    record.pointer_id = pointers.format_pointer(
        _pointer_type(record.category), record.project_fp, record.branch_fp, record.record_id
    )
    _insert_doc(project_conn, record)
    project_conn.commit()
    return record


def _write_ingested(
    project_conn: sqlite3.Connection,
    *,
    text: str,
    clean_text: str,
    chash: str,
    summary: str,
    sanitization_status: str,
    retain_raw: bool,
    run_id: str | None,
    fields: dict,
) -> DocRecord:
    """Write the doc row, body, provenance and FTS entry in one commit."""
    record_id = uuid.uuid4().hex
    record = DocRecord(
        record_id=record_id,
        pointer_id=pointers.format_pointer(
            _pointer_type(fields["category"]), fields["project_fp"], fields["branch_fp"], record_id
        ),
        content_hash=chash,
        sanitized_content_hash=chash,
        raw_retained=1 if retain_raw else 0,
        sanitization_status=sanitization_status,
        ingestion_status=schema.INGESTED,
        summary=summary,
        **fields,
    )
    _insert_doc(project_conn, record)
    project_conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body, raw_body) VALUES (?, ?, ?)",
        (record_id, clean_text, text if retain_raw else None),
    )
    project_conn.execute(
        "INSERT INTO project_doc_provenance "
        "(record_id, ingestion_run_id, detector, source_path, source_span_json, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (record_id, run_id, "direct", fields["source_path"], None, None),
    )
    _insert_fts(project_conn, record_id, clean_text, summary)
    project_conn.commit()
    return record


def ingest_record(
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    *,
    project_fp: str,
    branch_fp: str,
    source_path: str,
    category: str,
    subtype: str | None,
    text: str,
    cfg,
    source_uri: str | None = None,
    source_modified_at: str | None = None,
    git=None,
    run_id: str | None = None,
    summarizer=None,
) -> DocRecord:
    """Ingest one document into the project content DB.

    Runs the pipeline from the design spec, section 7, and returns the resulting
    :class:`DocRecord`. Records may end in one of three terminal states:

    * ``ingested`` — new ``project_docs`` row + body + provenance + FTS entry.
    * ``skipped_dedupe`` — an identical record already exists; the existing
      record is returned with its ``ingestion_status`` set accordingly.
    * ``rejected`` — the sanitizer flagged the text (oversize/binary/unsafe); a
      metadata-only row is written with no body and no FTS entry.

    Steps: validate context -> sanitize -> (rejected: metadata row) -> hash +
    dedupe -> summarize -> write doc/body/provenance/FTS.
    """
    fingerprints.validate_context(registry_conn, project_fp, branch_fp)

    clean_text, sanitization_status = _sanitized(text, cfg)
    now = utc_now_iso()
    fields = {
        "project_fp": project_fp,
        "branch_fp": branch_fp,
        "category": category,
        "created_at": now,
        "updated_at": now,
        "subtype": subtype or "",
        "source_path": source_path,
        "source_uri": source_uri,
        "source_modified_at": source_modified_at,
        "git_commit": getattr(git, "commit_hash", None) if git is not None else None,
        "git_branch": getattr(git, "branch", None) if git is not None else None,
        "ingestion_run_id": run_id,
    }

    if sanitization_status in schema.REJECTED_STATES:
        return _write_rejected(
            project_conn, text=text, sanitization_status=sanitization_status, fields=fields
        )

    chash = hashing.content_hash(clean_text)
    existing = _find_duplicate(project_conn, chash, source_path, branch_fp)
    if existing is not None:
        existing.ingestion_status = schema.SKIPPED_DEDUPE
        return existing

    summary = (
        summarizer(clean_text) if summarizer is not None else clean_text[:_DEFAULT_SUMMARY_CHARS]
    )
    return _write_ingested(
        project_conn,
        text=text,
        clean_text=clean_text,
        chash=chash,
        summary=summary,
        sanitization_status=sanitization_status,
        retain_raw=bool(cfg.ingestion.retain_raw_content),
        run_id=run_id,
        fields=fields,
    )
