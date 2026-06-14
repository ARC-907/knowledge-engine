"""Agent Board — scope → physical database routing.

A *scope* is a short key naming an isolation boundary: a project, a branch,
a worktree, an agent, or an agentic loop. Each scope maps to its own SQLite
file under ``<data_dir>/board-scopes/{slug}.db`` — a self-contained
engine-block of board state (messages, FTS index, key vault, config,
sweeper lease). Scopes share one process but nothing else: a post to
``branch-feat-auth`` is invisible to ``branch-main`` unless an agent
explicitly reads both.

This is the physical counterpart to the *logical* segregation the board
already offers via channel / task_id / product_id / visibility_scope.
Logical segregation keeps everything co-queryable in one DB; scopes give
hard separation (separate files, separate FTS, separate keys) for when an
agent or tenant must not see another's traffic at all.

Resolution flows through ``foundation.db.using_db`` (a ContextVar), so
setting a scope routes the board, queue, key vault, and sweeper underneath
it with no per-call argument plumbing.

The default (unscoped) board remains the shared ``pipeline.db`` — passing
``scope=None`` anywhere is fully backward compatible.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config

# Scope keys are operator/agent-supplied, so they're slugified before they
# ever touch the filesystem. Only a conservative charset survives; anything
# else collapses to a hyphen. This blocks path traversal (`../`), absolute
# paths, drive letters, and NTFS/POSIX reserved characters.
_SAFE_CHARS = re.compile(r"[^a-z0-9._-]+")
_MAX_SLUG_LEN = 80
SCOPES_DIRNAME = "board-scopes"


def slugify_scope(scope: str) -> str:
    """Normalize a scope key to a filesystem-safe slug.

    Lowercases, replaces runs of unsafe characters with a single hyphen,
    strips leading/trailing separators and dots (no hidden files, no
    traversal), and caps length. Raises ``ValueError`` on an empty result.
    """
    if not scope or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    slug = _SAFE_CHARS.sub("-", scope.strip().lower())
    slug = slug.strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip("-._")
    if not slug:
        raise ValueError(f"scope {scope!r} slugifies to empty; choose another key")
    return slug


def scopes_root() -> Path:
    """Directory that holds per-scope DB files (created on demand)."""
    root = Config.from_env().data_dir / SCOPES_DIRNAME
    return root


def scope_db_path(scope: str) -> str:
    """Absolute path to the SQLite file backing ``scope``.

    The parent directory is created lazily here; the DB + schema are created
    by ``foundation.db.get_connection`` on first use under ``using_db``.
    """
    slug = slugify_scope(scope)
    root = scopes_root()
    root.mkdir(parents=True, exist_ok=True)
    return str((root / f"{slug}.db").resolve())


def list_scopes() -> list[dict[str, object]]:
    """List known scopes by scanning the scopes directory for ``*.db`` files.

    Returns ``[{"scope": slug, "db_path": str, "size_bytes": int}, ...]``.
    SQLite sidecar files (``-wal`` / ``-shm``) are ignored.
    """
    root = scopes_root()
    if not root.exists():
        return []
    out: list[dict[str, object]] = []
    for p in sorted(root.glob("*.db")):
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        out.append({"scope": p.stem, "db_path": str(p.resolve()), "size_bytes": size})
    return out


def scope_db_paths() -> list[str]:
    """Just the resolved DB file paths for every known scope."""
    return [str(entry["db_path"]) for entry in list_scopes()]


__all__ = [
    "SCOPES_DIRNAME",
    "slugify_scope",
    "scopes_root",
    "scope_db_path",
    "list_scopes",
    "scope_db_paths",
]
