"""Project and branch registry helpers built on the fingerprint store.

This module provides the small, conservative surface used by the MCP tools and
CLI to register projects, enumerate them, validate a fingerprint, and resolve
the *current* working context (project + branch) for a given root.

It builds on the frozen P0 foundation (``db``/``paths``) and the wave-1
``fingerprints`` module. ``fingerprints.ensure_project`` /
``fingerprints.ensure_branch`` return the allocated fingerprint string and
record the human-facing fields (name, root path, branch name) in the registry
DB; this module reads those fields back to build its result dicts.

Branch detection is delegated to the optional ``git_context`` module; when that
module is unavailable, git is disabled by config, or the binary is missing, this
module degrades gracefully to the configured default branch rather than raising.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import fingerprints
from .paths import canonical_root


def register_project(
    conn: sqlite3.Connection,
    root: str,
    cfg,
    name: str | None = None,
    fingerprint: str | None = None,
) -> dict:
    """Register (or look up) a project for ``root`` in the registry DB.

    ``name`` defaults to the final path component of ``root``. The canonical
    form of ``root`` is used so that equivalent paths map to one project. The
    ``fingerprint`` argument is forwarded as a manual override when provided;
    otherwise the canonical derived fingerprint is used.

    Returns ``{"project_fp", "name", "root_path"}``.
    """
    resolved_name = name if name is not None else Path(root).name
    croot = canonical_root(root)
    project_fp = fingerprints.ensure_project(
        conn, croot, resolved_name, override=fingerprint
    )
    row = conn.execute(
        "SELECT name, root_path FROM projects WHERE project_fp = ?",
        (project_fp,),
    ).fetchone()
    return {
        "project_fp": project_fp,
        "name": row["name"] if row is not None else resolved_name,
        "root_path": row["root_path"] if row is not None else croot,
    }


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """Return every registered project ordered by name.

    Each entry is ``{"project_fp", "name", "root_path", "created_at"}``.
    """
    cur = conn.execute(
        "SELECT project_fp, name, root_path, created_at "
        "FROM projects ORDER BY name, project_fp"
    )
    return [
        {
            "project_fp": row["project_fp"],
            "name": row["name"],
            "root_path": row["root_path"],
            "created_at": row["created_at"],
        }
        for row in cur.fetchall()
    ]


def list_branches(conn: sqlite3.Connection, project_fp: str) -> list[dict]:
    """Return the branches registered for ``project_fp`` ordered by name.

    Each entry is ``{"branch_fp", "project_fp", "branch", "created_at"}``.
    """
    cur = conn.execute(
        "SELECT branch_fp, project_fp, branch_name, created_at "
        "FROM branches WHERE project_fp = ? "
        "ORDER BY branch_name, branch_fp",
        (project_fp,),
    )
    return [
        {
            "branch_fp": row["branch_fp"],
            "project_fp": row["project_fp"],
            "branch": row["branch_name"],
            "created_at": row["created_at"],
        }
        for row in cur.fetchall()
    ]


def validate_project(conn: sqlite3.Connection, project_fp: str) -> dict:
    """Validate that ``project_fp`` exists and report its branch count.

    Returns ``{"exists", "name", "branch_count"}``. For an unknown fingerprint
    this returns ``{"exists": False, "name": None, "branch_count": 0}`` rather
    than raising.
    """
    row = conn.execute(
        "SELECT name FROM projects WHERE project_fp = ?",
        (project_fp,),
    ).fetchone()
    if row is None:
        return {"exists": False, "name": None, "branch_count": 0}

    count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM branches WHERE project_fp = ?",
        (project_fp,),
    ).fetchone()
    branch_count = int(count_row["n"]) if count_row is not None else 0
    return {"exists": True, "name": row["name"], "branch_count": branch_count}


def _detect_branch(root: str, cfg) -> tuple[str, bool]:
    """Resolve ``(branch, git_available)`` for ``root``.

    Branch detection is delegated to the optional ``git_context`` module. When
    git is disabled by config, the module is unavailable, or detection yields
    no branch, the configured default branch is returned with
    ``git_available=False``.
    """
    default_branch = getattr(cfg.git, "default_branch", None) or "main"

    if not getattr(cfg.git, "enabled", False):
        return default_branch, False

    try:
        from . import git_context
    except ImportError:
        return default_branch, False

    if git_context is None:
        return default_branch, False

    collect = getattr(git_context, "collect", None)
    if collect is None:
        return default_branch, False

    try:
        ctx = collect(root, cfg)
    except Exception:
        return default_branch, False

    if ctx is None:
        # Git tooling ran but produced no context (no repo / no binary).
        return default_branch, False

    branch = getattr(ctx, "branch", None) or default_branch
    return branch, True


def current_context(root: str, cfg, conn: sqlite3.Connection) -> dict:
    """Resolve and register the current project + branch context for ``root``.

    Ensures the project and branch fingerprints exist in the registry DB and
    returns ``{"project_fp", "name", "branch", "branch_fp", "git_available"}``.
    ``git_available`` is True when ``git_context.collect`` returned a context.
    Detection never raises: an absent ``git_context`` module or a disabled git
    config falls back to the configured default branch with
    ``git_available=False``.
    """
    branch, git_available = _detect_branch(root, cfg)
    croot = canonical_root(root)
    name = Path(root).name

    project_fp = fingerprints.ensure_project(conn, croot, name)
    branch_fp = fingerprints.ensure_branch(conn, project_fp, branch)

    row = conn.execute(
        "SELECT name FROM projects WHERE project_fp = ?",
        (project_fp,),
    ).fetchone()

    return {
        "project_fp": project_fp,
        "name": row["name"] if row is not None else name,
        "branch": branch,
        "branch_fp": branch_fp,
        "git_available": git_available,
    }
