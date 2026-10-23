"""SQLite connection factory and a small versioned-migration runner.

The base engine uses inline ``executescript`` schema creation; the project-context
subsystem spans 20+ tables across two DB shapes (a shared fingerprint registry
and per-project content stores), so it uses ordered ``NNN_*.sql`` migration
files tracked by a ``schema_version`` table instead.

Storage note: on-disk table names keep the legacy ``project_doc*`` prefix
(``project_docs``, ``project_docs_fts``, ``project_doc_bodies``, ...) for
schema/data compatibility. The Python subsystem was renamed
``project_docs`` -> ``project_context`` in v2.0.0; the table names were
intentionally left unchanged to avoid a storage migration.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..core.util.sqlite import connect as sqlite_connect

#: Opens a connection with the engine's standard pragmas (WAL, busy timeout,
#: foreign keys, ``sqlite3.Row`` rows); see :func:`knowledge_engine.core.util.sqlite.connect`.
connect = sqlite_connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version (0 if none)."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _discover(
    migrations_dir: Path, only_prefixes: tuple[str, ...] | None
) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_RE.match(sql_file.name)
        if not m:
            continue
        if only_prefixes is not None and not sql_file.name.startswith(only_prefixes):
            continue
        found.append((int(m.group(1)), sql_file))
    found.sort(key=lambda t: t[0])
    return found


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
    only_prefixes: tuple[str, ...] | None = None,
) -> int:
    """Apply pending migrations in numeric order. Returns the count applied.

    Idempotent: migrations whose version is already recorded are skipped.
    ``only_prefixes`` restricts which files apply (the registry DB applies only
    ``("001_",)``; a project DB applies ``("002_", ..., "007_")``). Each
    migration runs in its own transaction and records its version on success.
    """
    directory = migrations_dir or MIGRATIONS_DIR
    current = schema_version(conn)
    applied = 0
    for version, sql_file in _discover(directory, only_prefixes):
        if version <= current:
            continue
        sql = sql_file.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        applied += 1
    return applied
