"""Vector search backends of the corpus embedding index (``KE_EMBED_BACKEND``).

* ``scan`` (default) -- :class:`~.index.CorpusEmbeddingIndex` streams every
  stored vector through a cosine scorer. Needs nothing beyond the standard
  library; always available.
* ``sqlite-vec`` -- the vectors are mirrored into a ``vec0`` virtual table of
  the `sqlite-vec <https://github.com/asg017/sqlite-vec>`_ extension (the
  optional ``vec`` extra). A query asks ``vec0`` for the nearest chunks and the
  index re-scores those candidates exactly, so results equal the scan's.

The ``vec0`` table lives in a **sibling database** next to the index
(``embeddings.db`` -> ``embeddings.vec.db``, :func:`vec_db_path`), never in the
index database itself: a virtual table whose module is not loaded cannot even
be dropped, so keeping it apart means the index stays fully usable (and
rebuildable) on an installation without the extension.

Keeping the mirror in sync: once the backend has been set up, two triggers on
``embedding_chunks`` (plain SQL, no extension needed) log every inserted or
deleted chunk id into ``embedding_vec_log``. A build run with the
``sqlite-vec`` backend replays that log into the ``vec0`` table (or rebuilds
the table when it is missing or cannot be trusted); a build run without the
extension simply leaves the log for the next one. :meth:`SqliteVecStore.staleness`
says why the mirror cannot be used (no package, no table, pending changes,
another model or dimension, a count that does not match); the index then
falls back to the scan with a WARNING -- the accelerator never fails a search.

The :class:`VectorAccelerator` protocol is the seam: anything that can return
the ids of the stored chunks nearest to a query vector, best first, can stand
in for ``vec0``.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ...core.settings import EMBED_BACKENDS
from ...core.util.sqlite import connect as sqlite_connect, transaction
from ...core.util.time import utc_now_iso
from .errors import same_model

if TYPE_CHECKING:
    from .index import IndexInfo

_logger = logging.getLogger(__name__)

#: The streaming cosine scan (always available).
SCAN = "scan"
#: The sqlite-vec ``vec0`` accelerator (optional ``vec`` extra).
SQLITE_VEC = "sqlite-vec"
#: Name of the ``vec0`` table in the sibling database.
VEC_TABLE = "embedding_vec"
#: Change log of ``embedding_chunks`` in the index database (filled by triggers).
LOG_TABLE = "embedding_vec_log"
#: Largest ``k`` a ``vec0`` KNN query accepts.
MAX_KNN = 4096
#: Rows copied per statement batch while syncing.
SYNC_BATCH = 512
#: The command that creates or refreshes the mirror.
SYNC_COMMAND = "knowledge-engine embeddings build --backend sqlite-vec"
#: Install hint shown when the package is missing.
INSTALL_HINT = (
    "install the `vec` extra (`uv sync --extra vec` or `pip install 'knowledge-engine[vec]'`)"
)

_TRIGGERS = {
    "embedding_vec_log_insert": (
        f"CREATE TRIGGER IF NOT EXISTS embedding_vec_log_insert AFTER INSERT ON embedding_chunks "
        f"BEGIN INSERT INTO {LOG_TABLE} (chunk_id) VALUES (new.id); END"
    ),
    "embedding_vec_log_delete": (
        f"CREATE TRIGGER IF NOT EXISTS embedding_vec_log_delete AFTER DELETE ON embedding_chunks "
        f"BEGIN INSERT INTO {LOG_TABLE} (chunk_id) VALUES (old.id); END"
    ),
}

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS vec_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    chunks INTEGER NOT NULL,
    sqlite_vec_version TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
"""

if len(EMBED_BACKENDS) != 2 or set(EMBED_BACKENDS) != {SCAN, SQLITE_VEC}:  # pragma: no cover
    raise RuntimeError("KE_EMBED_BACKEND values and the backend module disagree")


class BackendUnavailableError(Exception):
    """The accelerator cannot be used; ``str(exc)`` says why (shown as a warning)."""


class VectorAccelerator(Protocol):
    """Returns the ids of the stored chunks nearest to a query vector, best first.

    ``k`` is at most :data:`MAX_KNN`; ``library`` restricts the candidates to
    one top-level corpus subdirectory. Approximate answers are allowed: the
    index re-scores every candidate exactly and widens ``k`` until the result
    is provably the scan's.
    """

    def nearest(self, query: Sequence[float], k: int, library: str | None) -> list[int]: ...


def parse_backend(value: str | None) -> str:
    """``value`` as a backend name (blank -> ``scan``); ``ValueError`` when unknown."""
    text = (value or "").strip().lower() or SCAN
    if text not in EMBED_BACKENDS:
        raise ValueError(
            f"unknown vector backend {value!r}; use one of: {', '.join(EMBED_BACKENDS)}"
        )
    return text


def vec_db_path(db_path: Path) -> Path:
    """The sibling database of ``db_path`` holding the ``vec0`` mirror."""
    suffix = db_path.suffix or ".db"
    return db_path.with_name(f"{db_path.stem}.vec{suffix}")


def load_sqlite_vec(conn: sqlite3.Connection) -> str:
    """Load the sqlite-vec extension into ``conn``; returns its version.

    Raises :class:`BackendUnavailableError` when the package is not installed
    or this Python's ``sqlite3`` cannot load extensions.
    """
    try:
        module = importlib.import_module("sqlite_vec")
    except ImportError as exc:
        raise BackendUnavailableError(
            f"the sqlite-vec package is not installed; {INSTALL_HINT}"
        ) from exc
    try:
        conn.enable_load_extension(True)
        try:
            module.load(conn)
        finally:
            conn.enable_load_extension(False)
        row = conn.execute("SELECT vec_version()").fetchone()
    except (AttributeError, sqlite3.Error, OSError) as exc:
        # AttributeError: this Python's sqlite3 was built without extension loading.
        raise BackendUnavailableError(
            f"the sqlite-vec extension could not be loaded: {exc}"
        ) from exc
    return str(row[0]) if row else "unknown"


def tracking_installed(conn: sqlite3.Connection) -> bool:
    """True when the change-log triggers (and table) exist in the index database."""
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        ).fetchall()
    }
    return LOG_TABLE in names and set(_TRIGGERS) <= names


def install_tracking(conn: sqlite3.Connection) -> None:
    """Create the change log and its triggers on ``embedding_chunks`` (idempotent)."""
    with transaction(conn):
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {LOG_TABLE} "
            "(seq INTEGER PRIMARY KEY, chunk_id INTEGER NOT NULL)"
        )
        for sql in _TRIGGERS.values():
            conn.execute(sql)


def _vector_body(blob: bytes) -> bytes:
    """The float32 payload of a stored vector (drops the version byte, if any)."""
    data = bytes(blob)
    return data[1:] if len(data) % 4 == 1 else data


class SqliteVecStore:
    """The ``vec0`` mirror of one index, stored at ``path`` (see :func:`vec_db_path`)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ── connections ──────────────────────────────────────────────

    def open(self, *, create: bool = False) -> tuple[sqlite3.Connection, str]:
        """A connection with the extension loaded, and the extension version.

        Raises :class:`BackendUnavailableError` when the extension cannot be
        loaded, or (unless ``create``) when the mirror has not been built.
        """
        if not create and not self.path.is_file():
            self.probe()  # a missing package is the more useful reason
            raise BackendUnavailableError(
                f"the sqlite-vec table has not been built ({self.path}); run `{SYNC_COMMAND}`"
            )
        conn = sqlite_connect(self.path, row_factory=None, create_parent=create)
        try:
            version = load_sqlite_vec(conn)
        except BaseException:
            conn.close()
            raise
        return conn, version

    @staticmethod
    def probe() -> str:
        """The extension version when it loads in this process; else :class:`BackendUnavailableError`."""
        conn = sqlite3.connect(":memory:")
        try:
            return load_sqlite_vec(conn)
        finally:
            conn.close()

    # ── state ────────────────────────────────────────────────────

    @staticmethod
    def meta(vec: sqlite3.Connection) -> tuple[str, int, int] | None:
        """``(model, dim, chunks)`` recorded by the last sync; ``None`` when absent."""
        names = {
            str(row[0])
            for row in vec.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "vec_meta" not in names or VEC_TABLE not in names:
            return None
        row = vec.execute("SELECT model, dim, chunks FROM vec_meta WHERE id = 1").fetchone()
        return None if row is None else (str(row[0]), int(row[1]), int(row[2]))

    def staleness(
        self, main: sqlite3.Connection, vec: sqlite3.Connection, info: IndexInfo
    ) -> str | None:
        """Why the mirror cannot answer for the index ``main`` (``None``: it can)."""
        meta = self.meta(vec)
        if meta is None:
            return f"the sqlite-vec table has not been built ({self.path}); run `{SYNC_COMMAND}`"
        model, dim, chunks = meta
        if not same_model(model, info.model) or (info.dim and dim != info.dim):
            return (
                f"the sqlite-vec table holds model {model!r} ({dim} dims) but the index "
                f"{info.model!r} ({info.dim} dims); run `{SYNC_COMMAND}`"
            )
        if not tracking_installed(main):
            return f"the index was rebuilt without the sqlite-vec backend; run `{SYNC_COMMAND}`"
        pending = main.execute(f"SELECT COUNT(*) FROM {LOG_TABLE}").fetchone()[0]
        if pending:
            return (
                f"the sqlite-vec table is behind the index by {int(pending)} chunk changes; "
                f"run `{SYNC_COMMAND}`"
            )
        total = main.execute("SELECT COUNT(*) FROM embedding_chunks").fetchone()[0]
        if int(total) != chunks:
            return (
                f"the sqlite-vec table holds {chunks} vectors but the index {int(total)}; "
                f"run `{SYNC_COMMAND}`"
            )
        return None

    # ── sync ─────────────────────────────────────────────────────

    def sync(self, main: sqlite3.Connection, info: IndexInfo, *, full: bool) -> dict[str, object]:
        """Bring the mirror up to date with the index ``main``; returns a report.

        ``full`` (or a mirror that is missing or built for another model or
        dimension) recreates the ``vec0`` table from every stored vector;
        otherwise the change log is replayed. Raises
        :class:`BackendUnavailableError` or ``sqlite3.Error``.
        """
        vec, version = self.open(create=True)
        try:
            vec.executescript(_META_SCHEMA)
            meta = self.meta(vec)
            if (
                meta is None
                or not same_model(meta[0], info.model)
                or meta[1] != info.dim
                or not tracking_installed(main)
            ):
                full = True
            if not tracking_installed(main):
                install_tracking(main)
            if full or info.dim == 0:
                mode, changed = "full", self._full(main, vec, info)
            else:
                mode, changed = "incremental", self._replay(main, vec)
            total = int(main.execute("SELECT COUNT(*) FROM embedding_chunks").fetchone()[0])
            with transaction(vec):
                vec.execute(
                    "INSERT INTO vec_meta (id, model, dim, chunks, sqlite_vec_version, synced_at) "
                    "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET model = excluded.model, "
                    "dim = excluded.dim, chunks = excluded.chunks, "
                    "sqlite_vec_version = excluded.sqlite_vec_version, synced_at = excluded.synced_at",
                    (info.model, info.dim, total, version, utc_now_iso()),
                )
        finally:
            vec.close()
        _logger.info(
            "sqlite-vec table %s synced (%s, %d vectors written)", self.path, mode, changed
        )
        return {"mode": mode, "written": changed, "chunks": total, "sqlite_vec_version": version}

    @staticmethod
    def _insert_rows(vec: sqlite3.Connection, rows: Sequence[tuple[int, bytes, str]]) -> int:
        vec.executemany(
            f"INSERT INTO {VEC_TABLE} (rowid, embedding, library) VALUES (?, ?, ?)",
            [(int(chunk_id), _vector_body(blob), str(lib)) for chunk_id, blob, lib in rows],
        )
        return len(rows)

    def _full(self, main: sqlite3.Connection, vec: sqlite3.Connection, info: IndexInfo) -> int:
        """Recreate the ``vec0`` table from every stored vector."""
        # Everything up to now is covered by the full copy; later rows are replayed next time.
        last = main.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {LOG_TABLE}").fetchone()[0]
        written = 0
        with transaction(vec, immediate=True):
            vec.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")
            vec.execute("DELETE FROM vec_meta")
            if info.dim > 0:
                vec.execute(
                    f"CREATE VIRTUAL TABLE {VEC_TABLE} USING vec0("
                    f"embedding float[{int(info.dim)}] distance_metric=cosine, library text)"
                )
                cursor = main.execute(
                    "SELECT c.id, c.embedding, f.library FROM embedding_chunks c "
                    "JOIN embedding_files f ON f.id = c.file_id"
                )
                while rows := cursor.fetchmany(SYNC_BATCH):
                    written += self._insert_rows(vec, rows)
        with transaction(main):
            main.execute(f"DELETE FROM {LOG_TABLE} WHERE seq <= ?", (int(last),))
        return written

    def _replay(self, main: sqlite3.Connection, vec: sqlite3.Connection) -> int:
        """Apply the logged chunk changes to the ``vec0`` table."""
        log = main.execute(f"SELECT seq, chunk_id FROM {LOG_TABLE} ORDER BY seq").fetchall()
        if not log:
            return 0
        last = max(int(seq) for seq, _ in log)
        ids = sorted({int(chunk_id) for _, chunk_id in log})
        written = 0
        with transaction(vec, immediate=True):
            for start in range(0, len(ids), SYNC_BATCH):
                batch = ids[start : start + SYNC_BATCH]
                # A logged id is deleted, then re-inserted if the chunk exists now
                # (ids can be reused after a delete, so an id alone proves nothing).
                vec.executemany(
                    f"DELETE FROM {VEC_TABLE} WHERE rowid = ?", [(chunk_id,) for chunk_id in batch]
                )
                placeholders = ",".join("?" for _ in batch)
                rows = main.execute(
                    "SELECT c.id, c.embedding, f.library FROM embedding_chunks c "
                    f"JOIN embedding_files f ON f.id = c.file_id WHERE c.id IN ({placeholders})",
                    batch,
                ).fetchall()
                written += self._insert_rows(vec, rows)
        with transaction(main):
            main.execute(f"DELETE FROM {LOG_TABLE} WHERE seq <= ?", (last,))
        return written


class SqliteVecAccelerator:
    """:class:`VectorAccelerator` over an open mirror connection (see :class:`SqliteVecStore`)."""

    name = SQLITE_VEC

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def nearest(self, query: Sequence[float], k: int, library: str | None) -> list[int]:
        if not 1 <= k <= MAX_KNN:
            raise ValueError(f"k must be between 1 and {MAX_KNN}")
        blob = struct.pack(f"<{len(query)}f", *query)
        sql = f"SELECT rowid FROM {VEC_TABLE} WHERE embedding MATCH ? AND k = ?"
        params: list[object] = [blob, int(k)]
        if library:
            sql += " AND library = ?"
            params.append(library)
        sql += " ORDER BY distance"
        return [int(row[0]) for row in self._conn.execute(sql, params).fetchall()]


__all__ = [
    "INSTALL_HINT",
    "LOG_TABLE",
    "MAX_KNN",
    "SCAN",
    "SQLITE_VEC",
    "SYNC_COMMAND",
    "VEC_TABLE",
    "BackendUnavailableError",
    "SqliteVecAccelerator",
    "SqliteVecStore",
    "VectorAccelerator",
    "install_tracking",
    "load_sqlite_vec",
    "parse_backend",
    "tracking_installed",
    "vec_db_path",
]
