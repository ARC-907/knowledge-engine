"""Registry: corpus/registry.json is source of truth; SQLite mirror at data/registry.db."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

EntryKind = Literal["library", "skill", "kit", "tool"]
#: Public name for the entry-kind vocabulary (HTTP query/body validation uses it).
RegistryKind = EntryKind
REGISTRY_KINDS: tuple[str, ...] = get_args(EntryKind)

_logger = logging.getLogger(__name__)

_KIND_TO_BUCKET = {
    "library": "libraries",
    "skill": "skills",
    "kit": "kits",
    "tool": "tools",
}
_BUCKET_TO_KIND = {v: k for k, v in _KIND_TO_BUCKET.items()}


@dataclass
class RegistryEntry:
    id: str
    kind: EntryKind
    name: str
    path: str  # path relative to corpus_root
    enabled: bool = True
    auto_registered: bool = False
    description: str = ""
    tags: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


class Registry:
    """JSON-backed registry with SQLite mirror.

    JSON is canonical — SQLite is a queryable cache rebuilt from JSON on load.

    Thread safety: one re-entrant lock serialises every read and mutation of
    the in-memory document, the JSON write and the SQLite mirror, so a single
    instance can be shared by FastAPI's worker threadpool. The JSON file is
    replaced atomically (temp file + ``os.replace``), so a crash mid-write
    never leaves a truncated registry behind. :meth:`close` releases the
    SQLite connection; any later use raises ``sqlite3.ProgrammingError``.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS entries (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        auto_registered INTEGER NOT NULL,
        description TEXT,
        tags TEXT,
        created REAL,
        last_updated REAL
    );
    CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);
    CREATE INDEX IF NOT EXISTS idx_entries_enabled ON entries(enabled);
    """

    def __init__(self, registry_path: Path, db_path: Path) -> None:
        self.registry_path = registry_path
        self.db_path = db_path
        self._lock = threading.RLock()
        self._closed = False
        # check_same_thread=False + the lock above let a single shared Registry
        # be held on app.state and mutated from FastAPI's worker threadpool.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            self._conn.executescript(self.SCHEMA)
            self._load()
        except BaseException:
            self._conn.close()
            raise

    # ---------- Lifecycle ----------
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the SQLite mirror connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
        _logger.debug("registry mirror %s closed", self.db_path)

    def _check_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed registry.")

    # ---------- JSON I/O ----------
    def _load(self) -> None:
        with self._lock:
            if not self.registry_path.exists():
                self._write_default()
            with self.registry_path.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._sync_to_sqlite()

    def _write_default(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        default = {
            "version": 1,
            "libraries": [],
            "skills": [],
            "kits": [],
            "tools": [],
            "lifecycle": {"watch_enabled": True, "auto_register": True},
        }
        self._write_json(default)

    def _write_json(self, data: dict[str, Any]) -> None:
        """Write ``data`` to the registry file atomically (temp file + replace)."""
        text = json.dumps(data, indent=2)
        directory = self.registry_path.parent
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.registry_path.name}.", suffix=".tmp", dir=directory
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            tmp.replace(self.registry_path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _persist(self) -> None:
        with self._lock:
            self._check_open()
            self._data["last_updated"] = time.time()
            self._write_json(self._data)
            self._sync_to_sqlite()

    def _sync_to_sqlite(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM entries")
            for bucket, kind in _BUCKET_TO_KIND.items():
                for row in self._data.get(bucket, []):
                    cur.execute(
                        "INSERT INTO entries (id, kind, name, path, enabled, auto_registered, description, tags, created, last_updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["id"],
                            kind,
                            row.get("name", ""),
                            row.get("path", ""),
                            1 if row.get("enabled", True) else 0,
                            1 if row.get("auto_registered", False) else 0,
                            row.get("description", ""),
                            json.dumps(row.get("tags", [])),
                            row.get("created", time.time()),
                            row.get("last_updated", time.time()),
                        ),
                    )
            self._conn.commit()

    # ---------- Public API ----------
    def list(
        self, kind: EntryKind | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            results: list[dict[str, Any]] = []
            kinds = [kind] if kind else list(_KIND_TO_BUCKET.keys())
            for k in kinds:
                for row in self._data.get(_KIND_TO_BUCKET[k], []):
                    if enabled_only and not row.get("enabled", True):
                        continue
                    results.append({**row, "kind": k})
            return results

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            for bucket, kind in _BUCKET_TO_KIND.items():
                for row in self._data.get(bucket, []):
                    if row["id"] == entry_id:
                        return {**row, "kind": kind}
            return None

    def upsert(self, entry: RegistryEntry) -> dict[str, Any]:
        with self._lock:
            self._check_open()
            bucket = _KIND_TO_BUCKET[entry.kind]
            rows: list[dict[str, Any]] = self._data.setdefault(bucket, [])
            payload = asdict(entry)
            payload["last_updated"] = time.time()
            for i, row in enumerate(rows):
                if row["id"] == entry.id:
                    rows[i] = {**row, **payload}
                    self._persist()
                    return dict(rows[i])
            payload["created"] = payload.get("created", time.time())
            rows.append(payload)
            self._persist()
            return dict(payload)

    def set_enabled(self, entry_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._lock:
            self._check_open()
            for bucket, kind in _BUCKET_TO_KIND.items():
                for row in self._data.get(bucket, []):
                    if row["id"] == entry_id:
                        row["enabled"] = bool(enabled)
                        row["last_updated"] = time.time()
                        self._persist()
                        return {**row, "kind": kind}
            return None

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            self._check_open()
            for bucket in _BUCKET_TO_KIND:
                rows = self._data.get(bucket, [])
                for i, row in enumerate(rows):
                    if row["id"] == entry_id:
                        rows.pop(i)
                        self._persist()
                        return True
            return False

    def lifecycle(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("lifecycle", {}))

    def set_lifecycle(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self._check_open()
            lc = self._data.setdefault("lifecycle", {})
            lc.update(kwargs)
            self._persist()
            return dict(lc)
