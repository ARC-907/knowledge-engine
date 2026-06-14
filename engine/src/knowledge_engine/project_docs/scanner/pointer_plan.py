"""Mode 3 — dry-run pointer-rewrite planning (no source writes).

A *plan* is a structured, before/after preview of the docstring-to-pointer
replacements that :mod:`pointer_apply` would perform. Planning is strictly
read-only: it never opens a source file for writing. It persists one
``pointer_rewrite_plans`` row (``dry_run=1``) holding the plan items as JSON so
the plan can be reviewed and later applied by ``plan_id``.

Each item records the target file/span, the record's content hash, the proposed
pointer URI, the exact stub that would replace the span (the design-spec section
6 form), the backup strategy, risk flags, and whether the change is reversible.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import pointers
from ..schema import CATEGORY_DOCSTRING, POINTER_DOCSTRING

#: Suffix appended to a source file's path to name its rewrite backup. The
#: ``PointerReplacementCfg`` only carries a boolean ``write_backups`` gate (not a
#: suffix), so the convention is defined here and shared by plan + apply.
BACKUP_SUFFIX = ".ke-bak"


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def stub_for(pointer_uri: str, content_hash: str) -> str:
    """Return the Python docstring stub that replaces a span (spec section 6).

    Form: ``\"\"\"See <uri> (sha:<8>).\"\"\"`` — a single-line triple-quoted
    docstring carrying the pointer URI plus the first 8 chars of the content
    hash as a human hint.
    """
    return f'"""See {pointer_uri} (sha:{content_hash[:8]})."""'


def _parse_span(source_span: Any) -> tuple[int | None, int | None]:
    """Parse a span into 1-based inclusive ``(start, end)`` line numbers.

    Accepts either a ``"start-end"`` string or a 2-tuple/list. Returns
    ``(None, None)`` when the span is absent or malformed.
    """
    if source_span is None:
        return None, None
    if isinstance(source_span, (tuple, list)) and len(source_span) == 2:
        try:
            return int(source_span[0]), int(source_span[1])
        except (TypeError, ValueError):
            return None, None
    if isinstance(source_span, str):
        try:
            start_str, _, end_str = source_span.partition("-")
            return int(start_str), int(end_str)
        except (ValueError, AttributeError):
            return None, None
    return None, None


def _applied_record_ids(conn: sqlite3.Connection) -> set[str]:
    """Return record ids that already have a non-inactive pointer applied."""
    rows = conn.execute(
        "SELECT DISTINCT record_id FROM doc_pointers WHERE status = 'applied'"
    ).fetchall()
    return {row["record_id"] for row in rows}


def _provenance_span(conn: sqlite3.Connection, record_id: str) -> Any:
    """Return the source span recorded for ``record_id`` in provenance, if any.

    The span lives in ``project_doc_provenance.source_span_json`` (the
    ``project_docs`` table itself carries no span column). Returns the decoded
    JSON value, or ``None`` when no span was provenance-recorded.
    """
    row = conn.execute(
        "SELECT source_span_json FROM project_doc_provenance "
        "WHERE record_id = ? AND source_span_json IS NOT NULL "
        "ORDER BY id LIMIT 1",
        (record_id,),
    ).fetchone()
    if row is None or row["source_span_json"] is None:
        return None
    try:
        return json.loads(row["source_span_json"])
    except (TypeError, ValueError):
        return row["source_span_json"]


def _assess_risk(
    root: str, source_path: str | None, span: tuple[int | None, int | None]
) -> tuple[list[str], bool]:
    """Return ``(risk_flags, reversible)`` for a candidate, without writing.

    A missing file, a missing/unparseable span, or a span that does not line up
    with the file is risky and makes the item non-reversible.
    """
    flags: list[str] = []
    if not source_path:
        return ["missing_source_path"], False
    start, end = span
    if start is None or end is None:
        return ["unparseable_span"], False
    path = Path(root) / source_path
    if not path.is_file():
        return ["source_file_not_found"], False
    try:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return ["source_file_unreadable"], False
    if start < 1 or end > line_count or start > end:
        flags.append("span_out_of_range")
        return flags, False
    return flags, True


def run(
    root: str,
    cfg,
    project_conn: sqlite3.Connection,
    *,
    project_fp: str,
    branch_fp: str | None = None,
) -> dict:
    """Generate a dry-run pointer-rewrite plan and persist it.

    Reads docstring-category records that do not yet have an applied pointer,
    builds a replacement preview for each, assesses risk, and writes one
    ``pointer_rewrite_plans`` row (``dry_run=1``). Returns ``{plan_id, items}``.
    Never modifies a source file.
    """
    applied = _applied_record_ids(project_conn)
    rows = project_conn.execute(
        "SELECT * FROM project_docs WHERE category = ? ORDER BY created_at, record_id",
        (CATEGORY_DOCSTRING,),
    ).fetchall()

    items: list[dict] = []
    for row in rows:
        if row["record_id"] in applied:
            continue
        proposed_pointer = pointers.format_pointer(
            POINTER_DOCSTRING, project_fp, branch_fp or "", row["record_id"]
        )
        span = _parse_span(_provenance_span(project_conn, row["record_id"]))
        risk_flags, reversible = _assess_risk(root, row["source_path"], span)
        items.append(
            {
                "record_id": row["record_id"],
                "target_file": row["source_path"],
                "span": [span[0], span[1]] if span[0] is not None else None,
                "content_hash": row["content_hash"],
                "proposed_pointer": proposed_pointer,
                "replacement_preview": stub_for(proposed_pointer, row["content_hash"]),
                "backup_plan": (
                    f"write {BACKUP_SUFFIX} backup before replace"
                    if cfg.scanner.pointer_replacement.write_backups
                    else "no backup configured"
                ),
                "risk_flags": risk_flags,
                "reversible": reversible,
            }
        )

    plan_id = str(uuid.uuid4())
    project_conn.execute(
        """
        INSERT INTO pointer_rewrite_plans (plan_id, created_at, dry_run, items_json, status)
        VALUES (?, ?, 1, ?, 'planned')
        """,
        (plan_id, _utcnow(), json.dumps(items)),
    )
    project_conn.commit()
    return {"plan_id": plan_id, "items": items}
