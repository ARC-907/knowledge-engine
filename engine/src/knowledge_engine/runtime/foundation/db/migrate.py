"""Schema migrations of the pipeline / board database.

The schema lives in ``schema/NNN_<domain>.sql`` (one file per domain, each
headed by a comment naming its consumers) and is applied by the shared
numbered-SQL runner :func:`knowledge_engine.core.util.sqlite.apply_migrations`:
every migration in its own transaction, recorded in ``schema_version``.

Databases created before the schema was versioned (the old inline
``executescript`` schema plus ``ALTER TABLE ADD COLUMN`` patches) are adopted
in place: :data:`LEGACY_BASELINE` adds any column an older release lacked,
creates any table it had not created yet, and records versions 1..
:data:`BASELINE_VERSION` -- nothing existing is dropped or re-created.

New schema changes go into a new numbered file (``009_...sql``); never edit
a file that has shipped.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from ....core.util.sqlite import LegacyBaseline, Migration, apply_migrations, discover_migrations
from .board_config import seed_board_config

_logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

#: The migrations that together equal the last pre-versioned inline schema.
BASELINE_VERSION = 8

#: Columns that releases before the versioned schema added with ``ALTER TABLE``.
#: A pre-versioned database gets whichever of them it is missing on adoption.
LEGACY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("messages", "model_id", "TEXT"),
    ("messages", "thread_id", "TEXT"),
    ("workers", "safeguard_locked", "INTEGER DEFAULT 0"),
    ("workers", "safeguard_reason", "TEXT"),
    ("workers", "resources_json", "TEXT DEFAULT '{}'"),
    ("tasks", "input_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("tasks", "meta_json", "TEXT"),
    ("chat_conversations", "system_prompt", "TEXT"),
    ("chat_conversations", "context_limit", "INTEGER DEFAULT 20"),
    ("chat_conversations", "pinned", "INTEGER DEFAULT 0"),
    ("chat_conversations", "persona_id", "TEXT"),
    ("chat_messages", "excluded_from_context", "INTEGER DEFAULT 0"),
    ("chat_messages", "bookmarked", "INTEGER DEFAULT 0"),
    ("hosted_tools", "allow_inline", "INTEGER DEFAULT 0"),
    ("hosted_tools", "allowed_methods", "TEXT"),
    ("hosted_tools", "allowed_headers", "TEXT"),
)

# Per-process guard so the one-shot FTS5 backfill runs exactly once per DB
# path; without it every new worker-thread connection would re-scan messages.
_FTS_BACKFILLED: set[str] = set()
_FTS_BACKFILLED_LOCK = threading.Lock()

_MIGRATIONS: list[Migration] | None = None
_MIGRATIONS_LOCK = threading.Lock()


def migrations() -> list[Migration]:
    """The pipeline schema migrations, discovered once per process."""
    global _MIGRATIONS  # noqa: PLW0603 - lazily discovered, immutable afterwards
    with _MIGRATIONS_LOCK:
        if _MIGRATIONS is None:
            _MIGRATIONS = discover_migrations(SCHEMA_DIR)
        return list(_MIGRATIONS)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def add_legacy_columns(conn: sqlite3.Connection) -> list[str]:
    """Add the :data:`LEGACY_COLUMNS` an existing table lacks; return ``table.column`` added.

    Tables that do not exist are left alone (the baseline migrations create
    them with every column). Runs inside the adoption transaction.
    """
    added: list[str] = []
    for table, column, decl in LEGACY_COLUMNS:
        columns = _table_columns(conn, table)
        if columns is None or column in columns:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        added.append(f"{table}.{column}")
    if added:
        _logger.info("pre-versioned pipeline DB: added legacy columns %s", ", ".join(added))
    return added


LEGACY_BASELINE = LegacyBaseline(version=BASELINE_VERSION, prepare=add_legacy_columns)


def init_schema(conn: sqlite3.Connection) -> None:
    """Bring ``conn``'s database to the current schema; seed + backfill.

    Runs on every new cached connection. On an up-to-date database it costs
    a ``schema_version`` read, a ``board_config`` probe and a set lookup.
    """
    apply_migrations(conn, migrations(), legacy=LEGACY_BASELINE)
    seed_board_config(conn)
    _backfill_messages_fts_once(conn)


def _conn_path(conn: sqlite3.Connection) -> str:
    """The file path backing ``conn`` (the FTS backfill guard key)."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[2]) if row is not None and row[2] else ""


def _backfill_messages_fts_once(conn: sqlite3.Connection) -> None:
    """Mirror any pre-FTS messages into messages_fts, once per DB path per process.

    Silently no-ops if FTS5 isn't compiled in or the FTS table is missing
    (search falls back to LIKE).
    """
    path = _conn_path(conn)
    with _FTS_BACKFILLED_LOCK:
        if path in _FTS_BACKFILLED:
            return
        _FTS_BACKFILLED.add(path)
    try:
        rows = conn.execute(
            """SELECT m.message_id, m.channel,
                      COALESCE(m.subject,'') AS subject,
                      COALESCE(m.body,'')    AS body
               FROM messages m
               LEFT JOIN messages_fts f ON f.message_id = m.message_id
               WHERE f.message_id IS NULL"""
        ).fetchall()
        if not rows:
            return
        conn.executemany(
            "INSERT INTO messages_fts(message_id, channel, subject, body) VALUES (?, ?, ?, ?)",
            [(r["message_id"], r["channel"], r["subject"], r["body"]) for r in rows],
        )
        conn.commit()
        _logger.info("backfilled %d message(s) into messages_fts in %s", len(rows), path)
    except sqlite3.OperationalError:
        _logger.debug("messages_fts backfill skipped for %s", path, exc_info=True)


__all__ = [
    "BASELINE_VERSION",
    "LEGACY_BASELINE",
    "LEGACY_COLUMNS",
    "SCHEMA_DIR",
    "add_legacy_columns",
    "init_schema",
    "migrations",
]
