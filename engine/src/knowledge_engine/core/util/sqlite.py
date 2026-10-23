"""SQLite connections with the engine's standard pragmas.

Every database the engine opens (registry substrates, the pipeline/board
store, the FTS index, project-context stores) goes through :func:`connect`, so
they share one policy: WAL journaling for concurrent readers, a 5 s busy
timeout instead of an immediate ``database is locked``, foreign keys on, and
``check_same_thread=False`` so a connection guarded by the caller's lock can
be used from a worker thread.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

MEMORY = ":memory:"

RowFactory = Callable[[sqlite3.Cursor, tuple[Any, ...]], Any]


def connect(
    path: Path | str,
    *,
    wal: bool = True,
    busy_timeout_ms: int = 5000,
    foreign_keys: bool = True,
    check_same_thread: bool = False,
    row_factory: RowFactory | None = sqlite3.Row,
    create_parent: bool = True,
) -> sqlite3.Connection:
    """Open ``path`` with the standard pragmas and return the connection.

    ``row_factory`` defaults to :class:`sqlite3.Row` (mapping *and* index
    access); pass ``None`` for plain tuples. The parent directory is created
    unless ``create_parent`` is false or ``path`` is ``:memory:``. The
    connection is closed again if a pragma fails.
    """
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be >= 0")
    target = str(path)
    if create_parent and target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=check_same_thread)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def transaction(
    conn: sqlite3.Connection, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]:
    """Commit the block's statements on success and roll them back on any error.

    ``immediate`` starts the transaction with ``BEGIN IMMEDIATE`` (take the
    write lock up front) unless one is already open on ``conn``.
    """
    if immediate and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


class ThreadLocalConnections:
    """One connection to ``path`` per thread, opened lazily with :func:`connect`.

    ``init`` runs once on each new connection (schema creation, migrations).
    :meth:`close_all` closes every connection handed out so far, from any
    thread; a thread that asks again afterwards gets a fresh connection.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        init: Callable[[sqlite3.Connection], None] | None = None,
        **connect_kwargs: Any,
    ) -> None:
        self.path = str(path)
        self._init = init
        self._connect_kwargs = connect_kwargs
        self._local = threading.local()
        self._lock = threading.Lock()
        self._opened: list[sqlite3.Connection] = []
        self._generation = 0

    def get(self) -> sqlite3.Connection:
        """This thread's connection (opened and initialised on first use)."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "generation", -1) == self._generation:
            return conn
        conn = connect(self.path, **self._connect_kwargs)
        try:
            if self._init is not None:
                self._init(conn)
        except BaseException:
            conn.close()
            raise
        with self._lock:
            self._opened.append(conn)
            self._local.generation = self._generation
        self._local.conn = conn
        return conn

    def close_all(self) -> int:
        """Close every connection opened so far; return how many were closed."""
        with self._lock:
            opened, self._opened = self._opened, []
            self._generation += 1
        for conn in opened:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        _logger.debug("closed %d thread-local connection(s) to %s", len(opened), self.path)
        return len(opened)


def thread_local_connections(
    path: Path | str,
    *,
    init: Callable[[sqlite3.Connection], None] | None = None,
    **connect_kwargs: Any,
) -> ThreadLocalConnections:
    """A :class:`ThreadLocalConnections` cache for ``path``."""
    return ThreadLocalConnections(path, init=init, **connect_kwargs)


__all__ = [
    "MEMORY",
    "ThreadLocalConnections",
    "connect",
    "thread_local_connections",
    "transaction",
]
