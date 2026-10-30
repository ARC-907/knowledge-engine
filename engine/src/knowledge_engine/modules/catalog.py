"""The module catalog store (``modules.db``): intent, observed health, runs, jobs, artifacts.

``modules.db`` (``KE_MODULES_DB``, default ``<KE_DATA_DIR>/modules.db``) is
its own store (ADR 0014): the catalog owns executable capabilities, while
``knowledge.db`` owns source metadata and the pipeline DB follows the board's
per-scope routing, which a catalog must never do. The schema is the numbered
SQL in ``modules/migrations/`` applied by the shared runner.

Per ADR 0009, **intent and configuration** (``modules``: manifest, enabled
flag, operator settings, secret *references*, capability grants) are kept
apart from **observed health** (``module_health``), which the host rewrites
on every start and is not trusted across restarts. Every intent change bumps
``catalog_version``, which each run records.

One connection guarded by a lock, shared by threads (like the knowledge store).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from ..core.util.sqlite import apply_migrations, connect, discover_migrations, schema_version
from ..knowledge.errors import (
    IncompatibleError,
    InvalidInputError,
    KnowledgeError,
    UnavailableError,
)
from .artifacts import NormalizedArtifact
from .manifest import ModuleManifest, manifest_digest

_logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
#: Newest runs kept in ``module_runs`` (older rows are pruned on insert).
MAX_RUNS_KEPT = 5000

_T = TypeVar("_T")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class CatalogEntry:
    """One registered module: its manifest and the operator's intent."""

    module_id: str
    manifest: ModuleManifest
    manifest_digest: str
    source_dir: Path
    enabled: bool
    configured: bool
    settings: dict[str, Any] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)
    grants: tuple[str, ...] = ()
    registered_at: str = ""
    updated_at: str = ""

    def public(self) -> dict[str, Any]:
        """JSON-safe view (secret references are env var *names*, never values)."""
        manifest = self.manifest
        return {
            "module_id": self.module_id,
            "version": manifest.version,
            "description": manifest.description,
            "license": manifest.license,
            "transport": manifest.transport.kind,
            "capabilities": list(manifest.capabilities),
            "grants": list(self.grants),
            "enabled": self.enabled,
            "configured": self.configured,
            "settings": dict(self.settings),
            "secret_refs": {
                s.name: self.secret_env.get(s.name, s.env) for s in manifest.configuration.secrets
            },
            "tools": [
                {
                    "name": tool.name,
                    "public_name": f"{self.module_id}.{tool.name}",
                    "effect": tool.effect,
                    "idempotent": tool.idempotent,
                    "long_running": tool.long_running,
                }
                for tool in manifest.tools
            ],
            "manifest_digest": self.manifest_digest,
            "source_dir": str(self.source_dir),
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
        }


class ModuleCatalog:
    """Repository over ``modules.db``. Every failure is a typed ``KnowledgeError``."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, path: Path | str) -> ModuleCatalog:
        """Open (creating if needed) and migrate ``path``."""
        target = Path(path)
        try:
            conn = connect(target)
        except (OSError, sqlite3.Error) as exc:
            raise UnavailableError("the module catalog cannot be opened") from exc
        ok = False
        try:
            latest = max((m.version for m in discover_migrations(MIGRATIONS_DIR)), default=0)
            current = schema_version(conn)
            if current > latest:
                raise IncompatibleError(
                    "the module catalog was written by a newer engine",
                    details={"schema_version": current, "supported": latest},
                )
            applied = apply_migrations(conn, MIGRATIONS_DIR)
            ok = True
        except sqlite3.Error as exc:
            raise UnavailableError("the module catalog cannot be migrated") from exc
        finally:
            if not ok:
                conn.close()
        if applied:
            _logger.info("module catalog %s migrated (%d migration(s))", target, applied)
        return cls(conn, target)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    # ── transactions ─────────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise UnavailableError("the module catalog is closed")
            self._conn.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                yield self._conn
                self._conn.commit()
                committed = True
            finally:
                if not committed:
                    self._conn.rollback()

    def _run(self, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        try:
            with self._tx() as conn:
                return fn(conn)
        except KnowledgeError:
            raise
        except sqlite3.IntegrityError as exc:
            raise InvalidInputError(f"constraint violated: {exc}") from exc
        except sqlite3.Error as exc:
            _logger.error("module catalog %s: %s", self.path, exc)
            raise UnavailableError("the module catalog is unavailable; retry later") from exc

    def _bump(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO catalog_meta(key, value) VALUES ('catalog_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )

    # ── modules ──────────────────────────────────────────────────

    def catalog_version(self) -> int:
        def run(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT value FROM catalog_meta WHERE key = 'catalog_version'"
            ).fetchone()
            return int(row[0]) if row else 0

        return self._run(run)

    @staticmethod
    def _entry(row: sqlite3.Row) -> CatalogEntry:
        manifest = ModuleManifest.model_validate_json(row["manifest_json"])
        return CatalogEntry(
            module_id=row["module_id"],
            manifest=manifest,
            manifest_digest=row["manifest_digest"],
            source_dir=Path(row["source_dir"]),
            enabled=bool(row["enabled"]),
            configured=bool(row["configured"]),
            settings=json.loads(row["settings_json"]),
            secret_env=json.loads(row["secret_env_json"]),
            grants=tuple(json.loads(row["grants_json"])),
            registered_at=row["registered_at"],
            updated_at=row["updated_at"],
        )

    def upsert(self, manifest: ModuleManifest, source_dir: Path) -> tuple[CatalogEntry, bool]:
        """Install or replace a module's manifest. Replacing disables it (re-enable re-checks)."""

        def run(conn: sqlite3.Connection) -> tuple[CatalogEntry, bool]:
            now = now_iso()
            exists = conn.execute(
                "SELECT 1 FROM modules WHERE module_id = ?", (manifest.module_id,)
            ).fetchone()
            payload = manifest.model_dump_json()
            if exists:
                conn.execute(
                    "UPDATE modules SET version = ?, manifest_json = ?, manifest_digest = ?, "
                    "source_dir = ?, enabled = 0, updated_at = ? WHERE module_id = ?",
                    (
                        manifest.version,
                        payload,
                        manifest_digest(manifest),
                        str(source_dir),
                        now,
                        manifest.module_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO modules(module_id, version, manifest_json, manifest_digest, "
                    "source_dir, registered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        manifest.module_id,
                        manifest.version,
                        payload,
                        manifest_digest(manifest),
                        str(source_dir),
                        now,
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO module_health(module_id, state, detail, observed_at) "
                "VALUES (?, 'registered', '', ?) ON CONFLICT(module_id) DO UPDATE SET "
                "state = 'registered', detail = '', observed_at = excluded.observed_at",
                (manifest.module_id, now),
            )
            self._bump(conn)
            row = conn.execute(
                "SELECT * FROM modules WHERE module_id = ?", (manifest.module_id,)
            ).fetchone()
            return self._entry(row), not exists

        return self._run(run)

    def get(self, module_id: str) -> CatalogEntry:
        def run(conn: sqlite3.Connection) -> CatalogEntry:
            row = conn.execute("SELECT * FROM modules WHERE module_id = ?", (module_id,)).fetchone()
            if row is None:
                raise InvalidInputError(
                    f"module {module_id!r} is not registered", details={"module_id": module_id}
                )
            return self._entry(row)

        return self._run(run)

    def entries(self) -> list[CatalogEntry]:
        def run(conn: sqlite3.Connection) -> list[CatalogEntry]:
            rows = conn.execute("SELECT * FROM modules ORDER BY module_id").fetchall()
            return [self._entry(row) for row in rows]

        return self._run(run)

    def set_configuration(
        self, module_id: str, settings: dict[str, Any], secret_env: dict[str, str]
    ) -> CatalogEntry:
        def run(conn: sqlite3.Connection) -> CatalogEntry:
            cur = conn.execute(
                "UPDATE modules SET settings_json = ?, secret_env_json = ?, configured = 1, "
                "updated_at = ? WHERE module_id = ?",
                (
                    json.dumps(settings, sort_keys=True),
                    json.dumps(secret_env, sort_keys=True),
                    now_iso(),
                    module_id,
                ),
            )
            if cur.rowcount == 0:
                raise InvalidInputError(f"module {module_id!r} is not registered")
            self._bump(conn)
            return self._entry(
                conn.execute("SELECT * FROM modules WHERE module_id = ?", (module_id,)).fetchone()
            )

        return self._run(run)

    def set_enabled(
        self, module_id: str, enabled: bool, grants: tuple[str, ...] | None = None
    ) -> CatalogEntry:
        def run(conn: sqlite3.Connection) -> CatalogEntry:
            if grants is None:
                cur = conn.execute(
                    "UPDATE modules SET enabled = ?, updated_at = ? WHERE module_id = ?",
                    (1 if enabled else 0, now_iso(), module_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE modules SET enabled = ?, grants_json = ?, updated_at = ? "
                    "WHERE module_id = ?",
                    (1 if enabled else 0, json.dumps(sorted(grants)), now_iso(), module_id),
                )
            if cur.rowcount == 0:
                raise InvalidInputError(f"module {module_id!r} is not registered")
            self._bump(conn)
            return self._entry(
                conn.execute("SELECT * FROM modules WHERE module_id = ?", (module_id,)).fetchone()
            )

        return self._run(run)

    def is_enabled(self, module_id: str) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT enabled FROM modules WHERE module_id = ?", (module_id,)
            ).fetchone()
            return bool(row and row[0])

        return self._run(run)

    # ── observed health ──────────────────────────────────────────

    def set_health(self, module_id: str, state: str, detail: str = "") -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO module_health(module_id, state, detail, observed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(module_id) DO UPDATE SET state = excluded.state, "
                "detail = excluded.detail, observed_at = excluded.observed_at",
                (module_id, state, detail[:500], now_iso()),
            )

        self._run(run)

    def health(self) -> dict[str, dict[str, str]]:
        def run(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
            rows = conn.execute("SELECT * FROM module_health").fetchall()
            return {
                r["module_id"]: {
                    "state": r["state"],
                    "detail": r["detail"],
                    "observed_at": r["observed_at"],
                }
                for r in rows
            }

        return self._run(run)

    # ── runs (sanitized audit) ───────────────────────────────────

    def record_run(self, run: dict[str, Any]) -> None:
        columns = (
            "run_id",
            "module_id",
            "module_version",
            "tool",
            "surface",
            "principal",
            "workspace_id",
            "project_id",
            "outcome",
            "error_code",
            "elapsed_ms",
            "attempts",
            "catalog_version",
            "policy_json",
            "params_digest",
            "artifact_ids_json",
            "job_id",
            "started_at",
            "finished_at",
        )

        def go(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT OR REPLACE INTO module_runs({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(run.get(c) for c in columns),
            )
            conn.execute(
                "DELETE FROM module_runs WHERE run_id IN (SELECT run_id FROM module_runs "
                "ORDER BY started_at DESC LIMIT -1 OFFSET ?)",
                (MAX_RUNS_KEPT,),
            )

        self._run(go)

    def list_runs(self, module_id: str, limit: int = 20) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM module_runs WHERE module_id = ? ORDER BY started_at DESC LIMIT ?",
                (module_id, max(1, min(limit, 200))),
            ).fetchall()
            return [dict(r) for r in rows]

        return self._run(run)

    # ── jobs ─────────────────────────────────────────────────────

    def create_job(self, job: dict[str, Any]) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO module_jobs(job_id, run_id, module_id, tool, workspace_id, "
                "project_id, owner, state, created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["job_id"],
                    job["run_id"],
                    job["module_id"],
                    job["tool"],
                    job["workspace_id"],
                    job.get("project_id"),
                    job["owner"],
                    job["state"],
                    job["created_at"],
                    job["created_at"],
                    job["expires_at"],
                ),
            )

        self._run(run)

    def finish_job(
        self,
        job_id: str,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE module_jobs SET state = ?, result_json = ?, error_json = ?, "
                "updated_at = ?, expires_at = COALESCE(?, expires_at) WHERE job_id = ?",
                (
                    state,
                    json.dumps(result, default=str) if result is not None else None,
                    json.dumps(error) if error is not None else None,
                    now_iso(),
                    expires_at,
                    job_id,
                ),
            )

        self._run(run)

    def set_job_state(self, job_id: str, state: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE module_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (state, now_iso(), job_id),
            )

        self._run(run)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute("SELECT * FROM module_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _job_dict(row) if row else None

        return self._run(run)

    def list_jobs(self, workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM module_jobs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
                (workspace_id, max(1, min(limit, 200))),
            ).fetchall()
            return [_job_dict(r) for r in rows]

        return self._run(run)

    def request_cancel(self, job_id: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE module_jobs SET cancel_requested = 1, updated_at = ? WHERE job_id = ?",
                (now_iso(), job_id),
            )

        self._run(run)

    def cancel_requested(self, job_id: str) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT cancel_requested FROM module_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return bool(row and row[0])

        return self._run(run)

    def expire_jobs(self, now: str) -> int:
        """Drop finished jobs past their expiry; mark unfinished ones as expired."""

        def run(conn: sqlite3.Connection) -> int:
            conn.execute(
                "UPDATE module_jobs SET state = 'expired', result_json = NULL, updated_at = ? "
                "WHERE expires_at < ? AND state IN ('queued', 'running')",
                (now, now),
            )
            cur = conn.execute(
                "DELETE FROM module_jobs WHERE expires_at < ? AND state NOT IN "
                "('queued', 'running', 'expired')",
                (now,),
            )
            return cur.rowcount

        return self._run(run)

    # ── artifacts ────────────────────────────────────────────────

    def put_artifact(self, artifact: NormalizedArtifact) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO module_artifacts(artifact_id, run_id, module_id, "
                "module_version, workspace_id, project_id, media_type, digest, byte_length, "
                "descriptor_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.run_id,
                    artifact.module_id,
                    artifact.module_version,
                    artifact.scope.workspace_id,
                    artifact.scope.project_id,
                    artifact.media_type,
                    artifact.digest,
                    artifact.byte_length,
                    artifact.model_dump_json(),
                    now_iso(),
                ),
            )

        self._run(run)

    def get_artifact(self, artifact_id: str) -> NormalizedArtifact | None:
        def run(conn: sqlite3.Connection) -> NormalizedArtifact | None:
            row = conn.execute(
                "SELECT descriptor_json FROM module_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return NormalizedArtifact.model_validate_json(row[0]) if row else None

        return self._run(run)


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "run_id": row["run_id"],
        "module_id": row["module_id"],
        "tool": row["tool"],
        "workspace_id": row["workspace_id"],
        "project_id": row["project_id"],
        "owner": row["owner"],
        "state": row["state"],
        "cancel_requested": bool(row["cancel_requested"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


__all__ = ["MAX_RUNS_KEPT", "CatalogEntry", "ModuleCatalog", "now_iso"]
