"""Pipeline DB path resolution, scope routing and the connection cache.

The path is resolved DYNAMICALLY on every connection request, not frozen at
import. A coordination backbone that bakes its database path into a
module-level constant the moment it is first imported breaks the instant the
host reconfigures ``KE_PIPELINE_DB`` at runtime, runs two engines in one
process, or re-imports the package: a worker thread silently reads a stale
database while the main thread writes to the new one.

:func:`resolve_db_path` reads the environment on each call;
:func:`get_connection` caches the opened connection per (thread, resolved
path) in a bounded :class:`~knowledge_engine.core.util.sqlite.ConnectionCache`,
so the dynamic read costs one ``os.environ.get`` and changing the target DB
at runtime Just Works.

A ``contextvars.ContextVar`` ("scope DB") lets a caller route a whole block of
work -- every nested ``get_connection``, on the same thread or an awaited task
-- to a *different* physical database without threading a ``db_path``
argument through every function. This is what powers per-project /
per-branch / per-agent database segregation: set the scope, and the board,
queue, key vault, and sweeper underneath all follow it. Precedence is
explicit-arg -> scope-context -> env -> default, so an explicit path always
wins and the context only fills the "no arg" case.

Lifecycle: every connection is migrated on open (see :mod:`.migrate`) and
stays cached until :func:`close_all` (app lifespan shutdown, MCP server exit,
CLI exit, test teardown), :func:`close_connection`, the thread exiting, or the
per-thread bound evicting it.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from ....core import paths as engine_paths
from ....core.util.sqlite import ConnectionCache
from . import migrate

_logger = logging.getLogger(__name__)

PIPELINE_DB_ENV = "KE_PIPELINE_DB"
PIPELINE_DB_FILENAME = "pipeline.db"

#: Most pipeline/scope databases one thread keeps open at once. The board
#: sweeper visits the default DB plus every scope (``KE_BOARD_MAX_SCOPES``,
#: default 64) from one thread, so the bound leaves room for all of them plus
#: the pipeline DB; beyond it the least recently used connection is closed and
#: transparently reopened on next use (no caller holds a connection across
#: databases, so an eviction never closes a handle in use).
MAX_CONNECTIONS_PER_THREAD = 96

_scope_db_path: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ke_scope_db_path", default=None
)

_CACHE = ConnectionCache(init=migrate.init_schema, max_per_thread=MAX_CONNECTIONS_PER_THREAD)


def resolve_db_path(db_path: Path | str | None = None) -> str:
    """Resolve the active pipeline DB path. Read on every connection request.

    Precedence: explicit ``db_path`` arg -> active scope context
    (``using_db``) -> ``KE_PIPELINE_DB`` env -> ``<data_dir>/pipeline.db``, with
    ``data_dir`` resolved by ``core.paths`` (``KE_DATA_DIR``, else
    ``<KE_HOME>/.knowledge-engine/data``) so the board shares one data
    directory with the registry and FTS index.
    """
    if db_path:
        return str(Path(db_path).resolve())
    scoped = _scope_db_path.get()
    if scoped:
        return scoped
    env_db = os.environ.get(PIPELINE_DB_ENV, "").strip()
    if env_db:
        return str(Path(env_db).expanduser().resolve())
    return str(engine_paths.resolve_data_dir() / PIPELINE_DB_FILENAME)


@contextlib.contextmanager
def using_db(db_path: Path | str) -> Iterator[str]:
    """Route every no-arg ``get_connection()`` in this block to ``db_path``.

    Resolves the target DB path (it is created and migrated on first
    connect), binds it to the scope ContextVar for the duration of the block,
    and restores the prior scope on exit. Nestable. Yields the resolved path.

    Used by the agent board to give each project / branch / agent / loop
    its own physical SQLite engine-block while sharing one process::

        with using_db(scope_db_path("branch-feat-auth")):
            store.post_with_validation(...)  # lands in the scoped DB
    """
    resolved = str(Path(db_path).resolve())
    token = _scope_db_path.set(resolved)
    try:
        yield resolved
    finally:
        _scope_db_path.reset(token)


def current_db_path() -> str:
    """The DB path that a no-arg ``get_connection()`` would use right now."""
    return resolve_db_path()


def active_scope_db_path() -> str | None:
    """The scope-context DB path in effect, or None if unscoped."""
    return _scope_db_path.get()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """This thread's connection to the active pipeline DB (created + migrated if needed).

    The path is resolved dynamically (:func:`resolve_db_path`) so a host that
    reconfigures ``KE_PIPELINE_DB`` at runtime -- or runs more than one DB in
    a single process -- gets the right connection.
    """
    return _CACHE.get(resolve_db_path(db_path))


def close_connection(db_path: Path | str | None = None) -> int:
    """Close every thread's cached connection to one DB; return how many were closed."""
    return _CACHE.discard(resolve_db_path(db_path))


def close_all() -> int:
    """Close every cached pipeline-DB connection, from every thread.

    Wired into the HTTP app lifespan, the standalone board service, the MCP
    server and the CLI so a process never exits (or a test never ends) with
    open handles. A later ``get_connection()`` opens a fresh connection.
    """
    closed = _CACHE.close_all()
    if closed:
        _logger.debug("closed %d pipeline DB connection(s)", closed)
    return closed


def open_connection_count() -> int:
    """How many pipeline-DB connections are cached right now (all threads)."""
    return _CACHE.open_count()


__all__ = [
    "MAX_CONNECTIONS_PER_THREAD",
    "PIPELINE_DB_ENV",
    "PIPELINE_DB_FILENAME",
    "active_scope_db_path",
    "close_all",
    "close_connection",
    "current_db_path",
    "get_connection",
    "open_connection_count",
    "resolve_db_path",
    "using_db",
]
