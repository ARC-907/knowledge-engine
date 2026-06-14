"""Test/build/runtime log records as first-class documentation.

This module turns a single test or build invocation into a sanitized, classified,
branch-aware record in a per-project store. Logs are sensitive: the *sanitized*
text is always stored, but the raw output is retained only when
``cfg.ingestion.retain_raw_content`` is explicitly enabled (default off).

Classification is deterministic and conservative:

* ``exit_code == 0``                 -> ``pass``
* ``exit_code`` non-zero             -> ``fail``
* ``exit_code is None`` and output   -> ``error`` if the output looks like a crash
  (a ``Traceback`` / ``Error:`` marker), else ``unknown``
* ``exit_code is None`` and empty    -> ``unknown``

All controlled-vocabulary values come from :mod:`schema`; all sanitization goes
through :mod:`sanitize`. This module performs no subprocess or network I/O.
"""

from __future__ import annotations

import uuid
from typing import Any

from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.models import LogRecord, TestRun
from knowledge_engine.project_docs.sanitize import sanitize

# How many trailing lines of output to keep in the failure summary.
_FAILURE_TAIL_LINES = 20
# Maximum characters for the one-line ``summary`` field.
_SUMMARY_MAX_CHARS = 200
# Markers that indicate a crash rather than an ordinary test failure.
_ERROR_MARKERS = ("Traceback", "Error:")


def classify(exit_code: int | None, output: str) -> str:
    """Classify a run outcome into a :mod:`schema` test-classification constant.

    Args:
        exit_code: The process exit code, or ``None`` when it is unknown.
        output: The combined stdout/stderr text (raw or sanitized — only its
            shape matters here).

    Returns:
        One of :data:`schema.PASS`, :data:`schema.FAIL`, :data:`schema.ERROR`,
        :data:`schema.UNKNOWN`.
    """
    if exit_code == 0:
        return schema.PASS
    if exit_code is not None:
        return schema.FAIL
    # exit_code is None: distinguish a crash from a no-signal run.
    if output and any(marker in output for marker in _ERROR_MARKERS):
        return schema.ERROR
    return schema.UNKNOWN


def _summary_line(sanitized: str) -> str:
    """Derive a compact one-line summary from sanitized output."""
    for line in sanitized.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_SUMMARY_MAX_CHARS]
    return ""


def _failure_summary(sanitized: str, classification: str) -> str:
    """Extract a tail-of-output failure summary for non-passing runs."""
    if classification == schema.PASS:
        return ""
    lines = [ln for ln in sanitized.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-_FAILURE_TAIL_LINES:]
    return "\n".join(tail)


def _new_id() -> str:
    """Allocate a stable, unique record id."""
    return uuid.uuid4().hex


def record_test_run(
    project_conn: Any,
    registry_conn: Any,
    cfg: ProjectDocsConfig,
    *,
    command: str,
    exit_code: int | None,
    started_at: str,
    duration_ms: int | None = None,
    framework: str | None = None,
    target: str | None = None,
    output: str = "",
    git: Any = None,
    project_fp: str,
    branch_fp: str,
) -> TestRun:
    """Sanitize, classify, and store a single test invocation.

    Writes one ``test_runs`` row plus one ``test_log_records`` row holding the
    sanitized log. The raw log is stored only when
    ``cfg.ingestion.retain_raw_content`` is true; otherwise it is omitted.

    Args:
        project_conn: Connection to the per-project content DB (migrations 002+).
        registry_conn: Connection to the shared registry DB. Accepted for symmetry
            with sibling writers; not mutated here.
        cfg: Active project-docs config (governs raw retention + sanitization).
        command: The command line that was run.
        exit_code: Process exit code, or ``None`` if unknown.
        started_at: ISO-ish timestamp string for when the run started.
        duration_ms: Optional wall-clock duration in milliseconds.
        framework: Optional test framework name (e.g. ``"pytest"``).
        target: Optional test target/selector.
        output: Combined stdout/stderr of the run.
        git: Optional git context object exposing ``commit_hash`` and ``dirty``.
        project_fp: Owning project fingerprint.
        branch_fp: Owning branch fingerprint.

    Returns:
        The stored :class:`TestRun`.
    """
    del registry_conn  # symmetry with other record writers; not needed here

    result = sanitize(output, cfg, content_kind="log")
    sanitized_log = result.text
    classification = classify(exit_code, sanitized_log)
    retain_raw = bool(cfg.ingestion.retain_raw_content)
    git_commit, git_dirty_json = _git_fields(git)

    run = TestRun(
        id=_new_id(),
        project_fp=project_fp,
        branch_fp=branch_fp,
        started_at=started_at,
        command=command,
        framework=framework,
        target=target,
        exit_code=exit_code,
        classification=classification,
        duration_ms=duration_ms,
        git_commit=git_commit,
        git_dirty_json=git_dirty_json,
        summary=_summary_line(sanitized_log),
        failure_summary=_failure_summary(sanitized_log, classification),
        raw_retained=1 if retain_raw else 0,
    )

    _insert(project_conn, "test_runs", run.to_row())
    project_conn.execute(
        "INSERT INTO test_log_records (test_run_id, record_id, sanitized_log, raw_log) "
        "VALUES (?, ?, ?, ?)",
        (run.id, None, sanitized_log, output if retain_raw else None),
    )
    project_conn.commit()
    return run


def record_build_log(
    project_conn: Any,
    registry_conn: Any,
    cfg: ProjectDocsConfig,
    *,
    command: str,
    exit_code: int | None,
    started_at: str,
    duration_ms: int | None = None,
    output: str = "",
    git: Any = None,
    project_fp: str,
    branch_fp: str,
) -> LogRecord:
    """Sanitize, classify, and store a single build invocation.

    Writes one ``build_log_records`` row. The raw log is stored only when
    ``cfg.ingestion.retain_raw_content`` is true.

    Returns:
        The stored :class:`LogRecord`.
    """
    return _record_log(
        project_conn,
        registry_conn,
        cfg,
        table="build_log_records",
        command=command,
        exit_code=exit_code,
        started_at=started_at,
        duration_ms=duration_ms,
        output=output,
        git=git,
        project_fp=project_fp,
        branch_fp=branch_fp,
    )


def record_runtime_log(
    project_conn: Any,
    registry_conn: Any,
    cfg: ProjectDocsConfig,
    *,
    command: str,
    exit_code: int | None,
    started_at: str,
    duration_ms: int | None = None,
    output: str = "",
    git: Any = None,
    project_fp: str,
    branch_fp: str,
) -> LogRecord:
    """Sanitize, classify, and store a single runtime-log capture.

    Writes one ``runtime_log_records`` row. The raw log is stored only when
    ``cfg.ingestion.retain_raw_content`` is true.

    Returns:
        The stored :class:`LogRecord`.
    """
    return _record_log(
        project_conn,
        registry_conn,
        cfg,
        table="runtime_log_records",
        command=command,
        exit_code=exit_code,
        started_at=started_at,
        duration_ms=duration_ms,
        output=output,
        git=git,
        project_fp=project_fp,
        branch_fp=branch_fp,
    )


def _record_log(
    project_conn: Any,
    registry_conn: Any,
    cfg: ProjectDocsConfig,
    *,
    table: str,
    command: str,
    exit_code: int | None,
    started_at: str,
    duration_ms: int | None,
    output: str,
    git: Any,
    project_fp: str,
    branch_fp: str,
) -> LogRecord:
    """Shared implementation for build/runtime log records (identical shape)."""
    del registry_conn  # symmetry with other record writers; not needed here

    result = sanitize(output, cfg, content_kind="log")
    sanitized_log = result.text
    classification = classify(exit_code, sanitized_log)
    retain_raw = bool(cfg.ingestion.retain_raw_content)
    git_commit, _ = _git_fields(git)

    record = LogRecord(
        id=_new_id(),
        project_fp=project_fp,
        branch_fp=branch_fp,
        started_at=started_at,
        command=command,
        exit_code=exit_code,
        classification=classification,
        duration_ms=duration_ms,
        git_commit=git_commit,
        summary=_summary_line(sanitized_log),
        sanitized_log=sanitized_log,
        raw_log=output if retain_raw else None,
        raw_retained=1 if retain_raw else 0,
    )
    _insert(project_conn, table, record.to_row())
    project_conn.commit()
    return record


def get_test_history(
    project_conn: Any,
    *,
    branch_fp: str | None = None,
    limit: int = 20,
) -> list[TestRun]:
    """Return recent test runs, newest first.

    Args:
        project_conn: Per-project content DB connection.
        branch_fp: If given, restrict to one branch fingerprint.
        limit: Maximum number of runs to return.

    Returns:
        A list of :class:`TestRun`, ordered newest-first
        (``started_at`` descending, then insertion order).
    """
    sql = "SELECT * FROM test_runs"
    params: list[Any] = []
    if branch_fp is not None:
        sql += " WHERE branch_fp = ?"
        params.append(branch_fp)
    sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
    params.append(int(limit))
    rows = project_conn.execute(sql, params).fetchall()
    return [TestRun.from_row(dict(r)) for r in rows]


def get_failure_context(project_conn: Any, test_run_id: str) -> dict[str, Any]:
    """Return failure detail for a single test run.

    Joins the run with its sanitized log record so an agent can inspect why a
    run failed without pulling the raw log.

    Returns:
        A dict with run metadata plus ``sanitized_log``. Empty dict when the run
        id is unknown.
    """
    row = project_conn.execute(
        "SELECT * FROM test_runs WHERE id = ?", (test_run_id,)
    ).fetchone()
    if row is None:
        return {}
    run = TestRun.from_row(dict(row))
    log_row = project_conn.execute(
        "SELECT sanitized_log FROM test_log_records WHERE test_run_id = ? "
        "ORDER BY id ASC LIMIT 1",
        (test_run_id,),
    ).fetchone()
    sanitized_log = log_row["sanitized_log"] if log_row is not None else ""
    return {
        "id": run.id,
        "project_fp": run.project_fp,
        "branch_fp": run.branch_fp,
        "command": run.command,
        "exit_code": run.exit_code,
        "classification": run.classification,
        "started_at": run.started_at,
        "summary": run.summary,
        "failure_summary": run.failure_summary,
        "sanitized_log": sanitized_log,
    }


def get_latest_test_summary(
    project_conn: Any,
    *,
    branch_fp: str | None = None,
) -> dict[str, Any] | None:
    """Return a compact summary of the most recent test run, or ``None``.

    Args:
        project_conn: Per-project content DB connection.
        branch_fp: If given, restrict to one branch fingerprint.

    Returns:
        A dict with the latest run's id/classification/summary/timing, or
        ``None`` when there are no runs.
    """
    history = get_test_history(project_conn, branch_fp=branch_fp, limit=1)
    if not history:
        return None
    run = history[0]
    return {
        "id": run.id,
        "branch_fp": run.branch_fp,
        "classification": run.classification,
        "command": run.command,
        "started_at": run.started_at,
        "duration_ms": run.duration_ms,
        "summary": run.summary,
        "failure_summary": run.failure_summary,
    }


# ── internal helpers ─────────────────────────────────────────────────


def _git_fields(git: Any) -> tuple[str | None, str | None]:
    """Extract ``(commit, dirty_json)`` from an optional git context object.

    Degrades gracefully: a ``None`` git context yields ``(None, None)``.
    """
    if git is None:
        return None, None
    commit = getattr(git, "commit_hash", None)
    dirty = getattr(git, "dirty", None)
    dirty_json = None
    if dirty is not None:
        dirty_json = "true" if dirty else "false"
    return commit, dirty_json


def _insert(conn: Any, table: str, row: dict[str, Any]) -> None:
    """Insert a row dict into ``table`` using its keys as columns."""
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    conn.execute(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
        [row[c] for c in columns],
    )
