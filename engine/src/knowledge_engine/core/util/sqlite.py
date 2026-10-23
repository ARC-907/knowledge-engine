"""SQLite connections, connection caches and the numbered-SQL migration runner.

Every database the engine opens (registry substrates, the pipeline/board
store, the FTS index, project-context stores) goes through :func:`connect`, so
they share one policy: WAL journaling for concurrent readers, a 5 s busy
timeout instead of an immediate ``database is locked``, foreign keys on, and
``check_same_thread=False`` so a connection guarded by the caller's lock can
be used from a worker thread.

:class:`ConnectionCache` hands out one connection per (thread, path), keeps at
most a bounded number per thread, and can close every connection it opened
from any thread (app shutdown, CLI exit, test teardown).

:func:`apply_migrations` is the one schema runner: ordered ``NNN_name.sql``
files discovered in a directory, each applied in its own explicit transaction
and recorded in a ``schema_version`` table. A database created before it was
versioned is adopted through a :class:`LegacyBaseline` instead of being
re-created.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

MEMORY = ":memory:"

RowFactory = Callable[[sqlite3.Cursor, tuple[Any, ...]], Any]
ConnectionInit = Callable[[sqlite3.Connection], None]


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


# ── Connection caches ─────────────────────────────────────────────────

#: Default bound on the connections one thread keeps open in a
#: :class:`ConnectionCache` (least recently used ones are closed first).
DEFAULT_MAX_CONNECTIONS_PER_THREAD = 32


@dataclass
class _ThreadSlot:
    thread: threading.Thread
    conns: OrderedDict[str, sqlite3.Connection] = field(default_factory=OrderedDict)


def _close_quietly(conns: Iterable[sqlite3.Connection]) -> int:
    closed = 0
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            _logger.debug("closing a cached sqlite connection failed", exc_info=True)
        closed += 1
    return closed


class ConnectionCache:
    """One connection per (thread, path), opened lazily with :func:`connect`.

    * ``init`` runs once on every new connection (schema migrations ...). A
      failing ``init`` closes the connection again and propagates.
    * Each thread keeps at most ``max_per_thread`` connections; opening one
      more closes that thread's least recently used connection.
    * Connections of threads that have exited are closed the next time any
      thread opens a connection.
    * :meth:`close_all` and :meth:`discard` work from any thread; a thread
      that asks again afterwards gets a fresh connection.

    The lock only guards the bookkeeping: opening and initialising a
    connection happens outside it, so a slow migration on one thread never
    blocks another thread's lookups.
    """

    def __init__(
        self,
        *,
        init: ConnectionInit | None = None,
        max_per_thread: int = DEFAULT_MAX_CONNECTIONS_PER_THREAD,
        **connect_kwargs: Any,
    ) -> None:
        if max_per_thread < 1:
            raise ValueError("max_per_thread must be >= 1")
        self._init = init
        self._max_per_thread = max_per_thread
        self._connect_kwargs = connect_kwargs
        self._lock = threading.Lock()
        self._slots: dict[int, _ThreadSlot] = {}

    @property
    def max_per_thread(self) -> int:
        return self._max_per_thread

    def get(self, path: Path | str) -> sqlite3.Connection:
        """This thread's connection to ``path`` (opened and initialised on first use)."""
        key = str(path)
        current = threading.current_thread()
        ident = threading.get_ident()
        stale: list[sqlite3.Connection] = []
        with self._lock:
            slot = self._slots.get(ident)
            if slot is not None and slot.thread is not current:
                # The OS reused a finished thread's id: its connections are orphans.
                stale.extend(self._slots.pop(ident).conns.values())
                slot = None
            if slot is not None and key in slot.conns:
                slot.conns.move_to_end(key)
                return slot.conns[key]
        _close_quietly(stale)

        conn = connect(key, **self._connect_kwargs)
        try:
            if self._init is not None:
                self._init(conn)
        except BaseException:
            conn.close()
            raise

        evicted: list[sqlite3.Connection] = []
        with self._lock:
            evicted.extend(self._prune_dead_threads_locked())
            slot = self._slots.setdefault(ident, _ThreadSlot(current))
            slot.conns[key] = conn
            while len(slot.conns) > self._max_per_thread:
                old_path, old = slot.conns.popitem(last=False)
                _logger.debug("closing least recently used connection to %s", old_path)
                evicted.append(old)
        _close_quietly(evicted)
        return conn

    def _prune_dead_threads_locked(self) -> list[sqlite3.Connection]:
        dead = [ident for ident, slot in self._slots.items() if not slot.thread.is_alive()]
        orphans: list[sqlite3.Connection] = []
        for ident in dead:
            orphans.extend(self._slots.pop(ident).conns.values())
        return orphans

    def discard(self, path: Path | str) -> int:
        """Close every thread's cached connection to ``path``; return how many."""
        key = str(path)
        closing: list[sqlite3.Connection] = []
        with self._lock:
            for slot in self._slots.values():
                conn = slot.conns.pop(key, None)
                if conn is not None:
                    closing.append(conn)
        return _close_quietly(closing)

    def close_all(self) -> int:
        """Close every connection opened so far, from any thread; return how many."""
        with self._lock:
            slots, self._slots = self._slots, {}
        closed = _close_quietly(conn for slot in slots.values() for conn in slot.conns.values())
        if closed:
            _logger.debug("closed %d cached sqlite connection(s)", closed)
        return closed

    def open_count(self) -> int:
        """How many connections are currently cached (all threads)."""
        with self._lock:
            return sum(len(slot.conns) for slot in self._slots.values())

    def paths(self) -> set[str]:
        """The database paths that currently have at least one cached connection."""
        with self._lock:
            return {path for slot in self._slots.values() for path in slot.conns}


class ThreadLocalConnections:
    """One connection to ``path`` per thread: a :class:`ConnectionCache` for one path.

    ``init`` runs once on each new connection (schema creation, migrations).
    :meth:`close_all` closes every connection handed out so far, from any
    thread; a thread that asks again afterwards gets a fresh connection.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        init: ConnectionInit | None = None,
        **connect_kwargs: Any,
    ) -> None:
        self.path = str(path)
        self._cache = ConnectionCache(init=init, max_per_thread=1, **connect_kwargs)

    def get(self) -> sqlite3.Connection:
        """This thread's connection (opened and initialised on first use)."""
        return self._cache.get(self.path)

    def close_all(self) -> int:
        """Close every connection opened so far; return how many were closed."""
        closed = self._cache.close_all()
        _logger.debug("closed %d thread-local connection(s) to %s", closed, self.path)
        return closed


def thread_local_connections(
    path: Path | str,
    *,
    init: ConnectionInit | None = None,
    **connect_kwargs: Any,
) -> ThreadLocalConnections:
    """A :class:`ThreadLocalConnections` cache for ``path``."""
    return ThreadLocalConnections(path, init=init, **connect_kwargs)


# ── Numbered-SQL migrations ───────────────────────────────────────────

SCHEMA_VERSION_TABLE = "schema_version"

#: A migration whose file contains this line may fail (e.g. FTS5 missing from
#: the SQLite build): the failure is logged, nothing is recorded, and the
#: migration is attempted again the next time the runner sees the database.
OPTIONAL_MARKER = "-- ke:optional"

_MIGRATION_RE = re.compile(r"^(\d+)_([A-Za-z0-9_.-]+)\.sql$")
_COMMENT_LINE_RE = re.compile(r"^\s*--.*$", re.MULTILINE)

# (database, version) pairs whose optional-migration failure was already
# reported at WARNING; repeats go to DEBUG so a missing extension does not
# flood the log on every new connection.
_REPORTED_OPTIONAL: set[tuple[str, int]] = set()
_REPORTED_OPTIONAL_LOCK = threading.Lock()


class MigrationError(RuntimeError):
    """A migration failed; the database was rolled back to its previous version."""

    def __init__(self, migration: Migration, cause: BaseException) -> None:
        super().__init__(
            f"migration {migration.version:03d}_{migration.name} ({migration.path}) failed: "
            f"{type(cause).__name__}: {cause}"
        )
        self.migration = migration


@dataclass(frozen=True, order=True)
class Migration:
    """One ``NNN_name.sql`` file."""

    version: int
    name: str = field(compare=False)
    path: Path = field(compare=False)

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @functools.cached_property
    def optional(self) -> bool:
        return any(line.strip() == OPTIONAL_MARKER for line in self.sql().splitlines())


@dataclass(frozen=True)
class LegacyBaseline:
    """How :func:`apply_migrations` adopts a database created before versioning.

    A database is *pre-versioned* when it has user tables but no
    ``schema_version`` table. Adoption runs in one transaction: ``prepare``
    (if given) brings old table shapes up to date (e.g. adds columns that
    later releases introduced with ``ALTER TABLE``), then every non-optional
    migration up to ``version`` is replayed and recorded. Baseline migrations
    must therefore be idempotent (``CREATE ... IF NOT EXISTS``): replaying
    them never re-creates an existing table, it only adds what an older
    release had not created yet.
    """

    version: int
    prepare: Callable[[sqlite3.Connection], object] | None = None


def discover_migrations(
    directory: Path | str,
    *,
    only_prefixes: tuple[str, ...] | None = None,
    recursive: bool = False,
) -> list[Migration]:
    """The ``NNN_name.sql`` files in ``directory``, ordered by version.

    ``recursive`` also searches subdirectories (versions must then be unique
    across all of them). ``only_prefixes`` keeps only files whose name starts
    with one of the prefixes. Files that do not match the naming pattern are
    ignored; a duplicate version raises ``ValueError``.
    """
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {root}")
    files = root.rglob("*.sql") if recursive else root.glob("*.sql")
    found: dict[int, Migration] = {}
    for sql_file in sorted(files):
        match = _MIGRATION_RE.match(sql_file.name)
        if match is None:
            _logger.debug("ignoring %s: not a NNN_name.sql migration", sql_file)
            continue
        if only_prefixes is not None and not sql_file.name.startswith(only_prefixes):
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(
                f"duplicate migration version {version}: {found[version].path} and {sql_file}"
            )
        found[version] = Migration(version, match.group(2), sql_file)
    return [found[v] for v in sorted(found)]


def split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (``CREATE TRIGGER`` aware).

    Raises ``ValueError`` if the script ends with an incomplete statement.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if _COMMENT_LINE_RE.sub("", statement).strip():
                statements.append(statement)
            buffer = ""
    if _COMMENT_LINE_RE.sub("", buffer).strip():
        raise ValueError(f"incomplete SQL statement at end of script: {buffer.strip()[:80]!r}")
    return statements


def _create_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def ensure_version_table(conn: sqlite3.Connection) -> None:
    """Create the ``schema_version`` table if it does not exist (and commit)."""
    _create_version_table(conn)
    if conn.in_transaction:
        conn.commit()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """The migration versions recorded in ``schema_version`` (empty if none)."""
    if not _has_table(conn, SCHEMA_VERSION_TABLE):
        return set()
    return {int(row[0]) for row in conn.execute(f"SELECT version FROM {SCHEMA_VERSION_TABLE}")}


def schema_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration version (0 if none)."""
    return max(applied_versions(conn), default=0)


def is_pre_versioned(conn: sqlite3.Connection) -> bool:
    """True if the database has user tables but no ``schema_version`` table."""
    if _has_table(conn, SCHEMA_VERSION_TABLE):
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _db_label(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return "?"
    return str(row[2] or MEMORY) if row is not None else "?"


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        # A caller's pending writes would otherwise be swept into (and rolled
        # back with) the migration transaction; executescript() used to
        # commit them implicitly, so keep that contract explicitly.
        _logger.debug("committing an open transaction before applying migrations")
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _record(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at) VALUES (?, datetime('now'))",
        (version,),
    )


def _execute_script(conn: sqlite3.Connection, migration: Migration) -> None:
    for statement in split_statements(migration.sql()):
        conn.execute(statement)


def _adopt_legacy(
    conn: sqlite3.Connection, migrations: list[Migration], legacy: LegacyBaseline
) -> None:
    label = _db_label(conn)
    _begin(conn)
    try:
        if not is_pre_versioned(conn):  # another connection adopted it first
            conn.rollback()
            return
        _create_version_table(conn)
        if legacy.prepare is not None:
            legacy.prepare(conn)
        recorded: list[int] = []
        for migration in migrations:
            if migration.version > legacy.version or migration.optional:
                continue
            _execute_script(conn, migration)
            _record(conn, migration.version)
            recorded.append(migration.version)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _logger.info(
        "adopted pre-versioned database %s at schema version %d (baseline %s)",
        label,
        legacy.version,
        recorded,
    )


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply ``migration`` in its own transaction; False if already recorded."""
    _begin(conn)
    try:
        if migration.version in applied_versions(conn):  # applied concurrently
            conn.rollback()
            return False
        _execute_script(conn, migration)
        _record(conn, migration.version)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def _report_optional_failure(
    conn: sqlite3.Connection, migration: Migration, exc: Exception
) -> None:
    key = (_db_label(conn), migration.version)
    with _REPORTED_OPTIONAL_LOCK:
        first = key not in _REPORTED_OPTIONAL
        _REPORTED_OPTIONAL.add(key)
    _logger.log(
        logging.WARNING if first else logging.DEBUG,
        "optional migration %03d_%s skipped on %s (%s: %s); it is retried on the next open",
        migration.version,
        migration.name,
        key[0],
        type(exc).__name__,
        exc,
    )


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Iterable[Migration] | Path | str,
    *,
    only_prefixes: tuple[str, ...] | None = None,
    legacy: LegacyBaseline | None = None,
) -> int:
    """Apply every pending migration in version order; return how many ran.

    ``migrations`` is a directory (see :func:`discover_migrations`) or an
    explicit list. Each migration runs inside its own ``BEGIN IMMEDIATE``
    transaction together with its ``schema_version`` row, so a failure
    leaves the database exactly at the previous version (and raises
    :class:`MigrationError`). The write lock taken up front serialises
    concurrent migrators; each re-checks the recorded versions inside its
    transaction, so a migration is never applied twice.

    Optional migrations (see :data:`OPTIONAL_MARKER`) that fail are logged
    and skipped. ``legacy`` adopts a pre-versioned database (see
    :class:`LegacyBaseline`) before anything else runs.
    """
    if isinstance(migrations, (str, Path)):
        pending = discover_migrations(migrations, only_prefixes=only_prefixes)
    else:
        pending = sorted(
            m for m in migrations if only_prefixes is None or m.path.name.startswith(only_prefixes)
        )
    if legacy is not None and is_pre_versioned(conn):
        _adopt_legacy(conn, pending, legacy)
    ensure_version_table(conn)
    done = applied_versions(conn)
    count = 0
    for migration in pending:
        if migration.version in done:
            continue
        try:
            ran = _apply_one(conn, migration)
        except sqlite3.Error as exc:
            if migration.optional:
                _report_optional_failure(conn, migration, exc)
                continue
            _logger.error("migration %03d_%s failed: %s", migration.version, migration.name, exc)
            raise MigrationError(migration, exc) from exc
        if ran:
            count += 1
            _logger.debug(
                "applied migration %03d_%s to %s",
                migration.version,
                migration.name,
                _db_label(conn),
            )
    return count


__all__ = [
    "DEFAULT_MAX_CONNECTIONS_PER_THREAD",
    "MEMORY",
    "OPTIONAL_MARKER",
    "SCHEMA_VERSION_TABLE",
    "ConnectionCache",
    "LegacyBaseline",
    "Migration",
    "MigrationError",
    "ThreadLocalConnections",
    "applied_versions",
    "apply_migrations",
    "connect",
    "discover_migrations",
    "ensure_version_table",
    "is_pre_versioned",
    "schema_version",
    "split_statements",
    "thread_local_connections",
    "transaction",
]
