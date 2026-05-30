"""Full-text search over the per-project documentation store.

This module queries the contentless ``project_docs_fts`` FTS5 index and joins
back to ``project_docs`` so callers can apply branch / category / path / commit
filters and order results by BM25 relevance.

Results are compact by default: ``mode="summary"`` returns metadata plus a
highlighted snippet and never includes the document body. ``mode="full"``
includes the stored body only when the caller passes a config whose
``mcp.allow_full_content`` gate is enabled; otherwise the result carries a
``body_status`` of ``"not_permitted"`` instead of the body.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .schema import RESULT_FULL, RESULT_SUMMARY

# Columns selected from ``project_docs`` that populate each result dict.
_RECORD_COLUMNS = (
    "record_id",
    "category",
    "branch_fp",
    "source_path",
    "summary",
)

# FTS snippet helper: column 0 is ``searchable_body``; mark hits with <mark>.
_SNIPPET_EXPR = "snippet(project_docs_fts, 0, '<mark>', '</mark>', '...', 12)"


def search(
    project_conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    branch_fp: str | None = None,
    category: str | None = None,
    source_path: str | None = None,
    git_commit: str | None = None,
    since: str | None = None,
    mode: str = RESULT_SUMMARY,
    cfg: Any | None = None,
) -> list[dict[str, Any]]:
    """Search the project FTS index and return ranked result dicts.

    Args:
        project_conn: Open connection to a project DB (migrations 002-007).
        query: An FTS5 MATCH expression.
        limit: Maximum number of rows to return.
        branch_fp: Restrict to a single branch fingerprint.
        category: Restrict to a single document category.
        source_path: Restrict to an exact source path.
        git_commit: Restrict to a single git commit.
        since: Only include records whose ``created_at`` is ``>= since``.
        mode: ``"summary"`` (default) or ``"full"``.
        cfg: Optional config; required for bodies in ``"full"`` mode.

    Returns:
        A list of result dicts ordered by BM25 relevance (best first). Each
        dict contains ``record_id``, ``category``, ``branch_fp``,
        ``source_path``, ``summary``, ``snippet`` and ``score``. In full mode a
        ``body`` (or ``body_status``) key is added.
    """
    where: list[str] = ["project_docs_fts MATCH ?"]
    params: list[Any] = [query]
    _apply_filters(
        where,
        params,
        branch_fp=branch_fp,
        category=category,
        source_path=source_path,
        git_commit=git_commit,
        since=since,
    )

    sql = (
        f"SELECT pd.record_id, pd.category, pd.branch_fp, pd.source_path, "
        f"pd.summary, {_SNIPPET_EXPR} AS snippet, bm25(project_docs_fts) AS score "
        f"FROM project_docs_fts f "
        f"JOIN project_docs pd ON pd.rowid = f.rowid "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY bm25(project_docs_fts) "
        f"LIMIT ?"
    )
    params.append(int(limit))

    rows = project_conn.execute(sql, params).fetchall()
    return [_build_result(project_conn, row, mode=mode, cfg=cfg) for row in rows]


def get_record(
    project_conn: sqlite3.Connection,
    record_id: str,
    *,
    mode: str = RESULT_SUMMARY,
    cfg: Any | None = None,
) -> dict[str, Any] | None:
    """Return a single record by id, or ``None`` if it does not exist.

    The shape mirrors :func:`search` results minus the ``snippet`` and
    ``score`` fields (there is no MATCH query to derive them from).
    """
    sql = (
        "SELECT record_id, category, branch_fp, source_path, summary "
        "FROM project_docs WHERE record_id = ?"
    )
    row = project_conn.execute(sql, (record_id,)).fetchone()
    if row is None:
        return None

    result: dict[str, Any] = {col: row[col] for col in _RECORD_COLUMNS}
    _apply_body(project_conn, result, record_id, mode=mode, cfg=cfg)
    return result


def search_by_path(
    project_conn: sqlite3.Connection,
    source_path: str,
    **kw: Any,
) -> list[dict[str, Any]]:
    """List records restricted to a single source path (newest first)."""
    return _filtered_listing(project_conn, source_path=source_path, **kw)


def search_by_type(
    project_conn: sqlite3.Connection,
    category: str,
    **kw: Any,
) -> list[dict[str, Any]]:
    """List records restricted to a single document category (newest first)."""
    return _filtered_listing(project_conn, category=category, **kw)


def search_by_branch(
    project_conn: sqlite3.Connection,
    branch_fp: str,
    **kw: Any,
) -> list[dict[str, Any]]:
    """List records restricted to a single branch fingerprint (newest first)."""
    return _filtered_listing(project_conn, branch_fp=branch_fp, **kw)


def search_recent(
    project_conn: sqlite3.Connection,
    *,
    limit: int = 10,
    **kw: Any,
) -> list[dict[str, Any]]:
    """Return the most recently created records (no FTS query needed)."""
    return _filtered_listing(project_conn, limit=limit, **kw)


def _filtered_listing(
    project_conn: sqlite3.Connection,
    *,
    limit: int = 10,
    branch_fp: str | None = None,
    category: str | None = None,
    source_path: str | None = None,
    git_commit: str | None = None,
    since: str | None = None,
    mode: str = RESULT_SUMMARY,
    cfg: Any | None = None,
) -> list[dict[str, Any]]:
    """List records by metadata filters, ordered by ``created_at`` desc.

    Unlike :func:`search`, this path does not run an FTS MATCH, so results omit
    ``snippet``/``score`` and are ordered newest-first.
    """
    where: list[str] = ["1=1"]
    params: list[Any] = []
    _apply_filters(
        where,
        params,
        branch_fp=branch_fp,
        category=category,
        source_path=source_path,
        git_commit=git_commit,
        since=since,
        column_prefix="",
    )

    sql = (
        "SELECT record_id, category, branch_fp, source_path, summary "
        "FROM project_docs "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC "
        "LIMIT ?"
    )
    params.append(int(limit))

    rows = project_conn.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        result = {col: row[col] for col in _RECORD_COLUMNS}
        _apply_body(project_conn, result, row["record_id"], mode=mode, cfg=cfg)
        results.append(result)
    return results


def _apply_filters(
    where: list[str],
    params: list[Any],
    *,
    branch_fp: str | None,
    category: str | None,
    source_path: str | None,
    git_commit: str | None,
    since: str | None,
    column_prefix: str = "pd.",
) -> None:
    """Append metadata WHERE clauses and their bound parameters in place."""
    if branch_fp is not None:
        where.append(f"{column_prefix}branch_fp = ?")
        params.append(branch_fp)
    if category is not None:
        where.append(f"{column_prefix}category = ?")
        params.append(category)
    if source_path is not None:
        where.append(f"{column_prefix}source_path = ?")
        params.append(source_path)
    if git_commit is not None:
        where.append(f"{column_prefix}git_commit = ?")
        params.append(git_commit)
    if since is not None:
        where.append(f"{column_prefix}created_at >= ?")
        params.append(since)


def _build_result(
    project_conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    mode: str,
    cfg: Any | None,
) -> dict[str, Any]:
    """Convert an FTS result row into a result dict, attaching body if asked."""
    result: dict[str, Any] = {col: row[col] for col in _RECORD_COLUMNS}
    result["snippet"] = row["snippet"]
    result["score"] = row["score"]
    _apply_body(project_conn, result, row["record_id"], mode=mode, cfg=cfg)
    return result


def _apply_body(
    project_conn: sqlite3.Connection,
    result: dict[str, Any],
    record_id: str,
    *,
    mode: str,
    cfg: Any | None,
) -> None:
    """Attach (or gate) the document body for ``mode == "full"`` requests.

    Summary mode never includes a body. Full mode includes the stored
    ``searchable_body`` only when ``cfg`` is provided and
    ``cfg.mcp.allow_full_content`` is true; otherwise it records
    ``body_status="not_permitted"``.
    """
    if mode != RESULT_FULL:
        return

    if cfg is None or not cfg.mcp.allow_full_content:
        result["body_status"] = "not_permitted"
        return

    body_row = project_conn.execute(
        "SELECT searchable_body FROM project_doc_bodies WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    result["body"] = body_row["searchable_body"] if body_row is not None else None
