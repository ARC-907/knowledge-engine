"""Registry: corpus/registry.json is source of truth; SQLite mirror at data/registry.db."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

EntryKind = Literal["library", "skill", "tool"]

_KIND_TO_BUCKET = {"library": "libraries", "skill": "skills", "tool": "tools"}
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
        self._lock = threading.Lock()
        # check_same_thread=False + the lock below let a single shared Registry
        # be held on app.state and mutated from FastAPI's worker threadpool.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._load()

    # ---------- JSON I/O ----------
    def _load(self) -> None:
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
            "tools": [],
            "lifecycle": {"watch_enabled": True, "auto_register": True},
        }
        self.registry_path.write_text(json.dumps(default, indent=2), encoding="utf-8")

    def _persist(self) -> None:
        self._data["last_updated"] = time.time()
        self.registry_path.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8"
        )
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
    def list(self, kind: EntryKind | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        kinds = [kind] if kind else list(_KIND_TO_BUCKET.keys())
        for k in kinds:
            for row in self._data.get(_KIND_TO_BUCKET[k], []):
                if enabled_only and not row.get("enabled", True):
                    continue
                results.append({**row, "kind": k})
        return results

    def get(self, entry_id: str) -> dict[str, Any] | None:
        for bucket, kind in _BUCKET_TO_KIND.items():
            for row in self._data.get(bucket, []):
                if row["id"] == entry_id:
                    return {**row, "kind": kind}
        return None

    def upsert(self, entry: RegistryEntry) -> dict[str, Any]:
        bucket = _KIND_TO_BUCKET[entry.kind]
        rows = self._data.setdefault(bucket, [])
        payload = asdict(entry)
        payload["last_updated"] = time.time()
        for i, row in enumerate(rows):
            if row["id"] == entry.id:
                rows[i] = {**row, **payload}
                self._persist()
                return rows[i]
        payload["created"] = payload.get("created", time.time())
        rows.append(payload)
        self._persist()
        return payload

    def set_enabled(self, entry_id: str, enabled: bool) -> dict[str, Any] | None:
        for bucket, kind in _BUCKET_TO_KIND.items():
            for row in self._data.get(bucket, []):
                if row["id"] == entry_id:
                    row["enabled"] = bool(enabled)
                    row["last_updated"] = time.time()
                    self._persist()
                    return {**row, "kind": kind}
        return None

    def remove(self, entry_id: str) -> bool:
        for bucket in _BUCKET_TO_KIND:
            rows = self._data.get(bucket, [])
            for i, row in enumerate(rows):
                if row["id"] == entry_id:
                    rows.pop(i)
                    self._persist()
                    return True
        return False

    def lifecycle(self) -> dict[str, Any]:
        return dict(self._data.get("lifecycle", {}))

    def set_lifecycle(self, **kwargs: Any) -> dict[str, Any]:
        lc = self._data.setdefault("lifecycle", {})
        lc.update(kwargs)
        self._persist()
        return dict(lc)
