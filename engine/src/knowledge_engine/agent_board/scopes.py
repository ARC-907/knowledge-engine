"""Agent Board — scope → physical database routing.

A *scope* is a short key naming an isolation boundary: a project, a branch,
a worktree, an agent, or an agentic loop. Each scope maps to its own SQLite
file under ``<data_dir>/board-scopes/{name}.db`` — a self-contained
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

Lifecycle rules (see ``store._scoped``):

* Scope names must match :data:`SCOPE_NAME_RE` — lowercase letters, digits,
  ``_`` and ``-``, 1-64 characters, starting with a letter or digit. Anything
  else is rejected with ``ValueError`` before it touches the filesystem.
* **Reads never create a scope.** An unknown scope reads as empty.
* Scopes are created by a message post, an admin config update, or the
  explicit ``PUT /board/scopes/{name}`` route — and only while fewer than
  ``KE_BOARD_MAX_SCOPES`` (default 64) exist.

The default (unscoped) board remains the shared ``pipeline.db`` — passing
``scope=None`` anywhere is fully backward compatible.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..core import paths
from ..core.logsafe import log_safe
from ..core.util.slug import slugify
from ..runtime.foundation import db

_logger = logging.getLogger(__name__)

# Strict allow-list for scope names supplied over HTTP / MCP / CLI. Scope
# names become file names, so the charset is deliberately conservative: no
# dots (hidden files, traversal), no slashes, no uppercase, no whitespace.
SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Legacy normaliser kept for callers that pre-process free-form keys: ASCII
# letters/digits plus these characters survive, every other run becomes "-".
_SLUG_KEEP = "._-"
_MAX_SLUG_LEN = 80
SCOPES_DIRNAME = "board-scopes"

MAX_SCOPES_ENV = "KE_BOARD_MAX_SCOPES"
DEFAULT_MAX_SCOPES = 64


class ScopeLimitError(ValueError):
    """Raised when creating a scope would exceed ``KE_BOARD_MAX_SCOPES``."""


def validate_scope(scope: str) -> str:
    """Return ``scope`` unchanged if it is a valid scope name.

    Raises ``ValueError`` otherwise. This is the only gate between an
    operator/agent-supplied key and a path under :func:`scopes_root`.
    """
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty string")
    if not SCOPE_NAME_RE.fullmatch(scope):
        raise ValueError(
            f"invalid scope {scope!r}: must match {SCOPE_NAME_RE.pattern} "
            "(lowercase letters, digits, '_' and '-'; max 64 characters)"
        )
    return scope


def slugify_scope(scope: str) -> str:
    """Normalize a free-form key to a filesystem-safe slug.

    Lowercases, replaces runs of unsafe characters with a single hyphen,
    strips leading/trailing separators and dots (no hidden files, no
    traversal), and caps length. Raises ``ValueError`` on an empty result.

    Routing no longer slugifies implicitly — a scope must already satisfy
    :func:`validate_scope`. Clients that want to derive a scope from a
    branch name or path can run it through this helper first.
    """
    if not scope or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    slug = slugify(
        scope.strip(),
        separators="",
        keep=_SLUG_KEEP,
        replace_unknown=True,
        ascii_only=True,
        strip="-._",
        max_length=_MAX_SLUG_LEN,
    )
    if not slug:
        raise ValueError(f"scope {scope!r} slugifies to empty; choose another key")
    return slug


def max_scopes() -> int:
    """The scope cap from ``KE_BOARD_MAX_SCOPES`` (default 64; ``0`` = unlimited).

    A non-integer value is logged and falls back to the default.
    """
    raw = os.environ.get(MAX_SCOPES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_SCOPES
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "ignoring non-integer %s=%r; using %d",
            MAX_SCOPES_ENV,
            log_safe(raw),
            DEFAULT_MAX_SCOPES,
        )
        return DEFAULT_MAX_SCOPES
    return max(value, 0)


def scopes_root() -> Path:
    """Directory that holds per-scope DB files (created on first write).

    Lives under the engine data dir resolved by ``core.paths`` — the same
    directory that holds the default ``pipeline.db``.
    """
    return paths.resolve_data_dir() / SCOPES_DIRNAME


def scope_db_path(scope: str, *, create: bool = False) -> str:
    """Absolute path to the SQLite file backing ``scope``.

    With ``create=False`` (the default) this is a pure computation: nothing
    is touched on disk. With ``create=True`` the parent directory is made
    and the scope cap is enforced for a scope that does not exist yet: the
    count and the creation of the DB file (+ schema) happen under one lock.
    """
    name = validate_scope(scope)
    root = scopes_root()
    target = root / f"{name}.db"
    if create and not target.exists():
        _create_scope_db(name, root, target)
    return str(target.resolve())


def _create_scope_db(name: str, root: Path, target: Path) -> bool:
    """Count against the cap and create ``target`` as one step.

    Holding :func:`db.scope_creation_lock` while the database file is
    created (and migrated) closes the count-then-create race: concurrent
    first posts to new scopes in this process can no longer overshoot
    ``KE_BOARD_MAX_SCOPES``. Separate processes sharing the data directory
    are not serialised (bounded overshoot, as before).
    """
    with db.scope_creation_lock():
        if target.exists():  # created by a concurrent caller meanwhile
            return False
        limit = max_scopes()
        existing = len(list_scopes())
        if limit and existing >= limit:
            _logger.warning(
                "refusing to create scope %r: %d scopes exist, %s=%d",
                log_safe(name),
                existing,
                MAX_SCOPES_ENV,
                limit,
            )
            raise ScopeLimitError(
                f"scope limit reached ({existing}/{limit}); raise {MAX_SCOPES_ENV} "
                "or remove unused scope databases"
            )
        root.mkdir(parents=True, exist_ok=True)
        _logger.info("creating board scope %r at %s", log_safe(name), target)
        db.get_connection(target)
        return True


def scope_exists(scope: str) -> bool:
    """True if ``scope`` already has a database file (never creates one)."""
    return Path(scope_db_path(scope)).exists()


def create_scope(scope: str) -> dict[str, object]:
    """Explicitly create ``scope`` (idempotent).

    Returns ``{"scope", "db_path", "created"}``; ``created`` is False when
    the scope already existed. Raises ``ValueError`` on a bad name and
    :class:`ScopeLimitError` at the cap.
    """
    name = validate_scope(scope)
    root = scopes_root()
    target = root / f"{name}.db"
    created = False if target.exists() else _create_scope_db(name, root, target)
    return {"scope": scope, "db_path": str(target.resolve()), "created": created}


def list_scopes() -> list[dict[str, object]]:
    """List known scopes by scanning the scopes directory for ``*.db`` files.

    Returns ``[{"scope": name, "db_path": str, "size_bytes": int}, ...]``.
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


def nonconforming_scopes() -> list[dict[str, object]]:
    """Scope databases whose file name is not a valid scope name.

    Releases before 3.0 accepted dots and other characters in scope names
    (``v1.2.db``). Such a database is still listed by :func:`list_scopes`
    (and swept), but every scoped route rejects its name with 400. Each
    entry carries a ``suggested`` valid name to rename the file to.
    """
    out: list[dict[str, object]] = []
    for entry in list_scopes():
        name = str(entry["scope"])
        if SCOPE_NAME_RE.fullmatch(name):
            continue
        try:
            suggested: str | None = slugify_scope(name).replace(".", "-")[:64]
        except ValueError:
            suggested = None
        if suggested is not None and not SCOPE_NAME_RE.fullmatch(suggested):
            suggested = None
        out.append({**entry, "suggested": suggested})
    return out


def warn_nonconforming_scopes() -> int:
    """Log one WARNING per non-conforming scope database; return how many.

    Called at board startup (engine lifespan and standalone service).
    """
    try:
        legacy = nonconforming_scopes()
    except OSError:
        _logger.warning("could not scan %s for legacy scope names", scopes_root(), exc_info=True)
        return 0
    for entry in legacy:
        suggested = entry["suggested"]
        _logger.warning(
            "board scope database %s has a name that is not a valid scope (must match %s); "
            "it is listed and swept but cannot be addressed over HTTP/MCP/CLI -- rename it to %s",
            entry["db_path"],
            SCOPE_NAME_RE.pattern,
            f"{suggested}.db" if suggested else "a valid name",
        )
    return len(legacy)


def scope_db_paths() -> list[str]:
    """Just the resolved DB file paths for every known scope."""
    return [str(entry["db_path"]) for entry in list_scopes()]


__all__ = [
    "DEFAULT_MAX_SCOPES",
    "MAX_SCOPES_ENV",
    "SCOPES_DIRNAME",
    "SCOPE_NAME_RE",
    "ScopeLimitError",
    "create_scope",
    "list_scopes",
    "max_scopes",
    "nonconforming_scopes",
    "scope_db_path",
    "scope_db_paths",
    "scope_exists",
    "scopes_root",
    "slugify_scope",
    "validate_scope",
    "warn_nonconforming_scopes",
]
