"""SQLite connection factory and the project-context migration sets.

The project-context subsystem spans 20+ tables across two DB shapes, each
with its own ordered ``NNN_*.sql`` migrations tracked by a ``schema_version``
table and applied by the engine's shared runner
(:func:`knowledge_engine.core.util.sqlite.apply_migrations`):

* the shared fingerprint **registry** DB -- ``migrations/registry/``
  (:func:`apply_registry_migrations`);
* the per-project **content** DBs -- ``migrations/`` itself
  (:func:`apply_project_migrations`).

The sets are discovered from their directories: adding a migration means
dropping the next ``NNN_name.sql`` file into the right one. Versions are
unique across both sets, so the historical prefix filter
(``apply_migrations(conn, only_prefixes=("001_",))``) keeps working.

Storage note: on-disk table names keep the legacy ``project_doc*`` prefix
(``project_docs``, ``project_docs_fts``, ``project_doc_bodies``, ...) for
schema/data compatibility. The Python subsystem was renamed
``project_docs`` -> ``project_context`` in v2.0.0; the table names were
intentionally left unchanged to avoid a storage migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..core.util import sqlite as ke_sqlite
from ..core.util.sqlite import connect as sqlite_connect

#: Opens a connection with the engine's standard pragmas (WAL, busy timeout,
#: foreign keys, ``sqlite3.Row`` rows); see :func:`knowledge_engine.core.util.sqlite.connect`.
connect = sqlite_connect

#: Project content DB migrations (and, under ``registry/``, the registry set).
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
#: Fingerprint registry DB migrations.
REGISTRY_MIGRATIONS_DIR = MIGRATIONS_DIR / "registry"


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version (0 if none)."""
    ke_sqlite.ensure_version_table(conn)
    return ke_sqlite.schema_version(conn)


def apply_registry_migrations(conn: sqlite3.Connection) -> int:
    """Bring a fingerprint registry DB up to date; return how many migrations ran."""
    return ke_sqlite.apply_migrations(conn, REGISTRY_MIGRATIONS_DIR)


def apply_project_migrations(conn: sqlite3.Connection) -> int:
    """Bring a project content DB up to date; return how many migrations ran."""
    return ke_sqlite.apply_migrations(conn, MIGRATIONS_DIR)


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
    only_prefixes: tuple[str, ...] | None = None,
) -> int:
    """Apply pending migrations in numeric order. Returns the count applied.

    Idempotent: migrations whose version is already recorded are skipped.
    ``migrations_dir`` (default :data:`MIGRATIONS_DIR`) is searched
    recursively, so it covers both DB shapes; ``only_prefixes`` restricts
    which files apply (``("001_",)`` is the registry set). Prefer
    :func:`apply_registry_migrations` / :func:`apply_project_migrations`.
    Each migration runs in its own transaction and records its version on
    success.
    """
    found = ke_sqlite.discover_migrations(
        migrations_dir or MIGRATIONS_DIR, only_prefixes=only_prefixes, recursive=True
    )
    return ke_sqlite.apply_migrations(conn, found)


__all__ = [
    "MIGRATIONS_DIR",
    "REGISTRY_MIGRATIONS_DIR",
    "apply_migrations",
    "apply_project_migrations",
    "apply_registry_migrations",
    "connect",
    "schema_version",
]
