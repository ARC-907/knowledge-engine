"""Mode 4 — guarded application and rollback of a pointer-rewrite plan.

Applying a plan is the most dangerous capability in the subsystem: it edits
source files in place. Every safety gate must be open before a byte is written:
``validators.preflight("pointer_apply", ...)`` (which requires the scanner
enabled, ``pointer_replacement.enabled``, ``allow_source_mutation``, and
``dry_run=False``) *and* an explicit ``confirm=True`` from the caller. A closed
gate yields a structured ``not_permitted`` result rather than raising.

Each applied item ingests the docstring as a record, allocates a pointer, writes
a backup, replaces the exact span with a one-line stub, and verifies the pointer
resolves. Every action is recorded in the append-only ``pointer_rewrite_events``
ledger so :func:`rollback` can restore each file byte-for-byte from its backup.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import validators
from .pointer_plan import BACKUP_SUFFIX, _parse_span, stub_for
from .. import ingest, pointers
from ..schema import CATEGORY_DOCSTRING, POINTER_DOCSTRING


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _not_permitted(plan_id: str, reason: str) -> dict:
    """Build the structured result returned when a gate is closed."""
    return {
        "status": "not_permitted",
        "plan_id": plan_id,
        "reason": reason,
        "applied": [],
        "skipped": [],
        "errors": [],
    }


def _gates_open(cfg, confirm: bool) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the pointer-apply gate set.

    Combines the scanner-level :func:`validators.preflight` gate (which raises
    :class:`validators.GateError` when closed) with the caller's explicit
    ``confirm`` flag. Never raises.
    """
    try:
        validators.preflight("pointer_apply", cfg)
    except validators.GateError as exc:
        return False, str(exc)
    if not confirm:
        return False, "pointer_apply requires confirm=True"
    return True, ""


def _load_plan_items(conn: sqlite3.Connection, plan_id: str) -> list[dict] | None:
    """Return a plan's items list, or ``None`` if the plan id is unknown."""
    row = conn.execute(
        "SELECT items_json FROM pointer_rewrite_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["items_json"])


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file, fsync, rename)."""
    tmp = path.with_name(path.name + ".ke-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _replace_span(text: str, start: int, end: int, stub: str) -> str:
    """Replace 1-based inclusive line span ``[start, end]`` with ``stub``.

    The stub inherits the indentation of the first replaced line so the source
    stays syntactically valid, and the file's newline style is preserved.
    """
    lines = text.splitlines(keepends=True)
    first = lines[start - 1]
    indent = first[: len(first) - len(first.lstrip())]
    newline = "\r\n" if first.endswith("\r\n") else "\n"
    replacement = f"{indent}{stub}{newline}"
    return "".join(lines[: start - 1] + [replacement] + lines[end:])


def _record_event(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    pointer_id: str | None,
    action: str,
    backup_path: str | None,
    result: str | None = None,
    target_file: str | None = None,
) -> None:
    """Insert an append-only ``pointer_rewrite_events`` audit row.

    ``target_file`` is stored in the ``detail`` column as a small JSON object so
    rollback can locate the live file to restore.
    """
    detail = json.dumps({"target_file": target_file}) if target_file else None
    conn.execute(
        """
        INSERT INTO pointer_rewrite_events
            (plan_id, pointer_id, ts, action, backup_path, result, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, pointer_id, _utcnow(), action, backup_path, result, detail),
    )
    conn.commit()


def run(
    plan_id: str,
    root: str,
    cfg,
    project_conn: sqlite3.Connection,
    *,
    project_fp: str,
    branch_fp: str | None = None,
    confirm: bool = False,
    registry_conn: sqlite3.Connection | None = None,
) -> dict:
    """Apply a previously generated pointer-rewrite plan, fully gated.

    Requires every :func:`validators.preflight` gate for ``pointer_apply`` to be
    open *and* ``confirm=True``. When any gate is closed this returns a
    ``not_permitted`` result and writes nothing. ``registry_conn`` is required to
    ingest docstring records (it backs ``ingest.ingest_record``'s context
    validation); when absent, the call returns ``not_permitted``.
    """
    ok, reason = _gates_open(cfg, confirm)
    if not ok:
        return _not_permitted(plan_id, reason)

    if registry_conn is None:
        return _not_permitted(plan_id, "pointer_apply requires a registry connection")

    items = _load_plan_items(project_conn, plan_id)
    if items is None:
        return {
            "status": "unknown_plan",
            "plan_id": plan_id,
            "applied": [],
            "skipped": [],
            "errors": [],
        }

    applied: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    branch = branch_fp or ""

    for item in items:
        outcome = _apply_item(
            item,
            root,
            cfg,
            project_conn,
            registry_conn,
            plan_id=plan_id,
            project_fp=project_fp,
            branch_fp=branch,
        )
        bucket = outcome.pop("_bucket")
        {"applied": applied, "skipped": skipped, "errors": errors}[bucket].append(outcome)

    project_conn.execute(
        "UPDATE pointer_rewrite_plans SET status = 'applied' WHERE plan_id = ?",
        (plan_id,),
    )
    project_conn.commit()
    return {
        "status": "applied",
        "plan_id": plan_id,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
    }


def _apply_item(
    item: dict,
    root: str,
    cfg,
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    *,
    plan_id: str,
    project_fp: str,
    branch_fp: str,
) -> dict:
    """Apply one plan item; return a result dict carrying a ``_bucket`` tag."""
    target_file = item.get("target_file")
    start, end = _parse_span(item.get("span"))
    if not target_file or start is None or end is None:
        return {"_bucket": "skipped", "target_file": target_file, "reason": "incomplete_item"}

    path = Path(root) / target_file
    if not path.is_file():
        _record_event(
            project_conn,
            plan_id=plan_id,
            pointer_id=None,
            action="error",
            backup_path=None,
            result="source_file_not_found",
            target_file=str(path),
        )
        return {"_bucket": "errors", "target_file": target_file, "reason": "source_file_not_found"}

    # Read raw bytes for a byte-exact backup, and a decoded copy for span work.
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    line_count = len(original.splitlines())
    if start < 1 or end > line_count or start > end:
        _record_event(
            project_conn,
            plan_id=plan_id,
            pointer_id=None,
            action="error",
            backup_path=None,
            result="span_out_of_range",
            target_file=str(path),
        )
        return {"_bucket": "errors", "target_file": target_file, "reason": "span_out_of_range"}

    # Ingest the docstring body as a first-class record (computes its own hash).
    docstring_body = "\n".join(original.splitlines()[start - 1 : end])
    rec = ingest.ingest_record(
        project_conn,
        registry_conn,
        project_fp=project_fp,
        branch_fp=branch_fp,
        source_path=target_file,
        category=CATEGORY_DOCSTRING,
        subtype="docstring",
        text=docstring_body,
        cfg=cfg,
    )

    # Allocate the pointer and mark it applied.
    pointer_id = pointers.allocate(
        project_conn,
        rec.record_id,
        POINTER_DOCSTRING,
        project_fp,
        branch_fp,
        target_file,
        [start, end],
        rec.content_hash,
    )
    project_conn.execute(
        "UPDATE doc_pointers SET status = 'applied' WHERE pointer_id = ?",
        (pointer_id,),
    )
    project_conn.commit()

    # Backup (byte-exact), then atomically replace the span with the stub.
    backup_path: str | None = None
    if cfg.scanner.pointer_replacement.write_backups:
        backup_path = str(path) + BACKUP_SUFFIX
        Path(backup_path).write_bytes(original_bytes)

    stub = stub_for(pointer_id, rec.content_hash)
    _atomic_write(path, _replace_span(original, start, end, stub))

    # Verify the pointer resolves to the same content hash.
    resolved = pointers.resolve(project_conn, pointer_id)
    if resolved is None or resolved.get("content_hash") != rec.content_hash:
        _record_event(
            project_conn,
            plan_id=plan_id,
            pointer_id=pointer_id,
            action="error",
            backup_path=backup_path,
            result="pointer_did_not_resolve",
            target_file=str(path),
        )
        return {
            "_bucket": "errors",
            "target_file": target_file,
            "reason": "pointer_did_not_resolve",
            "pointer_id": pointer_id,
        }

    _record_event(
        project_conn,
        plan_id=plan_id,
        pointer_id=pointer_id,
        action="apply",
        backup_path=backup_path,
        result="ok",
        target_file=str(path),
    )
    return {
        "_bucket": "applied",
        "target_file": target_file,
        "record_id": rec.record_id,
        "pointer_id": pointer_id,
        "backup_path": backup_path,
    }


def rollback(
    plan_id: str,
    project_conn: sqlite3.Connection,
) -> dict:
    """Restore source files from a plan's apply backups, byte-for-byte.

    Reads each ``apply`` event for the plan, restores the recorded backup over
    the live file (located via the event ``detail``), marks the pointer
    ``rolled_back``, and records a ``rollback`` audit event. Events lacking a
    backup path are skipped.
    """
    events = project_conn.execute(
        """
        SELECT pointer_id, backup_path, detail FROM pointer_rewrite_events
        WHERE plan_id = ? AND action = 'apply' ORDER BY ts, id
        """,
        (plan_id,),
    ).fetchall()

    restored: list[dict] = []
    skipped: list[dict] = []
    for event in events:
        backup_path = event["backup_path"]
        pointer_id = event["pointer_id"]
        if not backup_path:
            skipped.append({"pointer_id": pointer_id, "reason": "no_backup"})
            continue
        backup = Path(backup_path)
        if not backup.is_file():
            skipped.append({"pointer_id": pointer_id, "reason": "backup_missing"})
            continue
        detail = json.loads(event["detail"]) if event["detail"] else {}
        target_file = detail.get("target_file")
        target = Path(target_file) if target_file else backup
        # Restore byte-for-byte (no newline translation).
        target.write_bytes(backup.read_bytes())
        if pointer_id:
            project_conn.execute(
                "UPDATE doc_pointers SET status = 'rolled_back' WHERE pointer_id = ?",
                (pointer_id,),
            )
            project_conn.commit()
        _record_event(
            project_conn,
            plan_id=plan_id,
            pointer_id=pointer_id,
            action="rollback",
            backup_path=backup_path,
            result="ok",
            target_file=target_file,
        )
        restored.append({"pointer_id": pointer_id, "target_file": str(target)})

    project_conn.execute(
        "UPDATE pointer_rewrite_plans SET status = 'rolled_back' WHERE plan_id = ?",
        (plan_id,),
    )
    project_conn.commit()
    return {"status": "rolled_back", "plan_id": plan_id, "restored": restored, "skipped": skipped}
