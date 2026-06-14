"""FTS5 indexer over enabled registry entries."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import Config
from .registry import Registry

INDEX_FILENAME = "index.db"
TEXT_SUFFIXES = {".md", ".txt", ".rst"}

_logger = logging.getLogger(__name__)


@dataclass
class IndexedDoc:
    entry_id: str
    entry_kind: str
    relpath: str
    content: str


class Indexer:
    """SQLite FTS5 indexer. Idempotent: rebuild on demand.

    The connection is opened with ``check_same_thread=False`` and every cursor
    operation is serialized with a lock, so a single shared ``Indexer`` can be
    safely held on ``app.state`` and used from FastAPI's worker threadpool.
    """

    SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
        entry_id UNINDEXED,
        entry_kind UNINDEXED,
        relpath UNINDEXED,
        content,
        tokenize='porter unicode61'
    );
    """

    def __init__(self, config: Config, registry: Registry) -> None:
        self.config = config
        self.registry = registry
        self.db_path = config.data_dir / INDEX_FILENAME
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)

    def rebuild(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM docs")
            counts = {"entries": 0, "files": 0, "missing": 0}
            for entry in self.registry.list(enabled_only=True):
                counts["entries"] += 1
                entry_root = (self.config.corpus_root / entry["path"]).resolve()
                if not entry_root.exists():
                    counts["missing"] += 1
                    _logger.warning(
                        "Library %r referenced in registry.json but not found "
                        "at %s — see README for the Pro bundle (delivered via Polar) "
                        "if you expected curated libraries to be present.",
                        entry.get("name") or entry.get("id"),
                        entry_root,
                    )
                    continue
                if entry_root.is_file():
                    files: Iterable[Path] = [entry_root]
                else:
                    files = (
                        p for p in entry_root.rglob("*")
                        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
                    )
                for path in files:
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    rel = path.relative_to(self.config.corpus_root).as_posix()
                    cur.execute(
                        "INSERT INTO docs (entry_id, entry_kind, relpath, content) VALUES (?, ?, ?, ?)",
                        (entry["id"], entry["kind"], rel, text),
                    )
                    counts["files"] += 1
            self._conn.commit()
            return counts

    def search(self, query: str, limit: int = 20) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT entry_id, entry_kind, relpath, snippet(docs, 3, '<mark>', '</mark>', '...', 12), bm25(docs) "
                    "FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
                    (query, limit),
                )
            except sqlite3.OperationalError:
                return []
            rows = cur.fetchall()
        return [
            {
                "entry_id": r[0],
                "entry_kind": r[1],
                "path": r[2],
                "snippet": r[3],
                "score": r[4],
            }
            for r in rows
        ]
