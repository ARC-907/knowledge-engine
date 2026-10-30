"""FTS5 indexer over enabled registry entries."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.registry import Registry
from ..core.util.sqlite import connect as sqlite_connect, transaction
from .ownership import EntryOwnership

INDEX_FILENAME = "index.db"
TEXT_SUFFIXES = {".md", ".txt", ".rst"}
#: Most ranked rows :meth:`Indexer.search_where` examines for one query.
MAX_FILTER_SCAN = 50_000

_logger = logging.getLogger(__name__)


@dataclass
class IndexedDoc:
    entry_id: str
    entry_kind: str
    relpath: str
    content: str


class SearchResults(list[dict[str, Any]]):
    """The hits of one search (a plain ``list`` of dicts).

    ``syntax_fallback`` is true when the query was not valid FTS5 syntax and
    was retried as one quoted phrase. ``mode`` is the search mode that
    produced the hits (``fts``, or ``vector``/``hybrid`` from
    :mod:`knowledge_engine.retrieval.hybrid`). :attr:`meta` carries both for
    responses.
    """

    def __init__(
        self,
        hits: Iterable[dict[str, Any]] = (),
        *,
        syntax_fallback: bool = False,
        mode: str = "fts",
    ):
        super().__init__(hits)
        self.syntax_fallback = syntax_fallback
        self.mode = mode

    @property
    def meta(self) -> dict[str, Any]:
        """Result metadata (empty for a plain ``fts`` search with nothing noteworthy)."""
        meta: dict[str, Any] = {}
        if self.mode != "fts":
            meta["mode"] = self.mode
        if self.syntax_fallback:
            meta["syntax_fallback"] = True
        return meta


_FTS5_QUERY_ERROR_MARKERS = (
    "fts5: syntax error",
    "syntax error",
    "unknown special query",
    "unterminated string",
    "no such column",
)


def _is_fts5_query_error(exc: sqlite3.OperationalError) -> bool:
    """True when ``exc`` is FTS5 rejecting the MATCH expression (not a DB fault)."""
    message = str(exc).lower()
    return any(marker in message for marker in _FTS5_QUERY_ERROR_MARKERS)


def quote_phrase(query: str) -> str:
    """``query`` as a single FTS5 string (phrase) literal: ``"..."`` with ``"`` doubled."""
    return '"' + query.replace('"', '""') + '"'


class Indexer:
    """SQLite FTS5 indexer. Idempotent: rebuild on demand.

    The connection comes from :func:`knowledge_engine.core.util.sqlite.connect`
    (WAL, busy timeout, ``check_same_thread=False``) and every cursor
    operation is serialized with a lock, so a single shared ``Indexer`` can be
    safely held on ``app.state`` and used from FastAPI's worker threadpool.
    :meth:`rebuild` reads the corpus *before* taking the lock, so searches
    keep being served while files are read; only the table swap is exclusive.
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
        self._closed = False
        self._conn = sqlite_connect(self.db_path, row_factory=None, foreign_keys=False)
        try:
            self._conn.executescript(self.SCHEMA)
        except BaseException:
            self._conn.close()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the index connection. Idempotent; later use raises ``ProgrammingError``."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
        _logger.debug("FTS index %s closed", self.db_path)

    # ── rebuild ──────────────────────────────────────────────────

    def _entry_files(self, entry: dict[str, Any], counts: dict[str, int]) -> Iterator[Path]:
        entry_root = (self.config.corpus_root / entry["path"]).resolve()
        if not entry_root.exists():
            counts["missing"] += 1
            _logger.warning(
                "Library %r is listed in registry.json but its content is "
                "missing from the corpus (expected at %s). Add the library "
                "folder under the corpus root (KE_CORPUS_ROOT) and reindex, "
                "or disable the entry in registry.json; see "
                "01-user-docs/BRING-YOUR-OWN-CORPUS.md.",
                entry.get("name") or entry.get("id"),
                entry_root,
            )
            return
        if entry_root.is_file():
            yield entry_root
            return
        for path in entry_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path

    def _collect(self) -> tuple[list[tuple[str, str, str, str]], dict[str, int]]:
        """Walk the enabled entries and read every indexable file (no lock held).

        Each file is indexed once, under the entry that owns it
        (:mod:`.ownership`): a file inside a nested enabled entry is skipped
        while walking the enclosing one.
        """
        counts = {"entries": 0, "files": 0, "missing": 0}
        rows: list[tuple[str, str, str, str]] = []
        entries = self.registry.list(enabled_only=True)
        ownership = EntryOwnership(entries)
        seen: set[str] = set()
        for entry in entries:
            counts["entries"] += 1
            for path in self._entry_files(entry, counts):
                rel = path.relative_to(self.config.corpus_root).as_posix()
                owner = ownership.owner(rel)
                if owner is not None and owner.get("id") != entry["id"]:
                    continue  # indexed under the nested entry that owns it
                if rel in seen:
                    continue  # two entries with the same path: the first one wins
                seen.add(rel)
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError as exc:
                    _logger.debug("skipping unreadable %s: %s", path, exc)
                    continue
                rows.append((entry["id"], entry["kind"], rel, text))
                counts["files"] += 1
        return rows, counts

    def rebuild(self) -> dict[str, int]:
        """Re-index every enabled entry; return ``{"entries", "files", "missing"}``.

        The corpus is read first; the table is then replaced in one
        transaction, so a concurrent search sees either the old or the new
        index, never a half-built one.
        """
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed index.")
        rows, counts = self._collect()
        with self._lock, transaction(self._conn, immediate=True):
            self._conn.execute("DELETE FROM docs")
            self._conn.executemany(
                "INSERT INTO docs (entry_id, entry_kind, relpath, content) VALUES (?, ?, ?, ?)",
                rows,
            )
        _logger.info(
            "FTS index rebuilt: %d entries, %d files, %d missing",
            counts["entries"],
            counts["files"],
            counts["missing"],
        )
        return counts

    # ── search ───────────────────────────────────────────────────

    def _run(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()

    def search(self, query: str, limit: int = 20, kind: str | None = None) -> SearchResults:
        """Rank matches for ``query`` by BM25.

        ``kind`` (``library`` / ``skill`` / ``kit`` / ``tool``) is applied in
        SQL *before* ``LIMIT``, so a filtered search still returns up to
        ``limit`` hits of that kind.

        A query that is not valid FTS5 syntax (an unbalanced quote, a stray
        ``-``, a bare ``AND`` ...) is retried once as a quoted phrase; the
        result then has ``syntax_fallback`` set. If even that fails the error
        is logged at WARNING and the result is empty -- never silently.
        """
        sql = (
            "SELECT entry_id, entry_kind, relpath, snippet(docs, 3, '<mark>', '</mark>', '...', 12), bm25(docs) "
            "FROM docs WHERE docs MATCH ?"
        )
        params: list[Any] = [query]
        if kind:
            sql += " AND entry_kind = ?"
            params.append(kind)
        sql += " ORDER BY bm25(docs) LIMIT ?"
        params.append(limit)
        fallback = False
        try:
            rows = self._run(sql, params)
        except sqlite3.OperationalError as exc:
            if not _is_fts5_query_error(exc):
                # A locked, missing or corrupt index is not a query problem:
                # let it propagate so the HTTP layer answers 503 instead of
                # claiming a syntax fallback.
                raise
            _logger.debug("FTS5 rejected query %r (%s); retrying as a phrase", query, exc)
            fallback = True
            params[0] = quote_phrase(query)
            try:
                rows = self._run(sql, params)
            except sqlite3.OperationalError as retry_exc:
                if not _is_fts5_query_error(retry_exc):
                    raise
                _logger.warning("FTS5 search failed for query %r: %s", query, retry_exc)
                rows = []
        return SearchResults(
            (
                {
                    "entry_id": r[0],
                    "entry_kind": r[1],
                    "path": r[2],
                    "snippet": r[3],
                    "score": r[4],
                }
                for r in rows
            ),
            syntax_fallback=fallback,
        )

    def search_where(
        self,
        query: str,
        *,
        limit: int,
        accept: Callable[[str], bool],
        max_scan: int = MAX_FILTER_SCAN,
    ) -> tuple[SearchResults, bool]:
        """BM25 hits whose path passes ``accept``, filtered *before* ``limit``.

        Matches are read in BM25 order and ``accept(relpath)`` decides each one
        until ``limit`` are accepted, so a selective filter still yields up to
        ``limit`` hits. At most ``max_scan`` ranked matches are examined (bounded
        work per query); the second value is true when that cap stopped the scan
        before ``limit`` hits were found. Snippets are computed only for the
        accepted rows. Invalid FTS5 syntax falls back to a phrase as in
        :meth:`search`.
        """
        if limit < 1 or max_scan < 1:
            raise ValueError("limit and max_scan must be positive integers")
        sql = (
            "SELECT rowid, entry_id, entry_kind, relpath, bm25(docs) FROM docs"
            " WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?"
        )
        params: list[Any] = [query, max_scan]
        fallback = False
        try:
            rows = self._run(sql, params)
        except sqlite3.OperationalError as exc:
            if not _is_fts5_query_error(exc):
                raise
            fallback = True
            params[0] = quote_phrase(query)
            try:
                rows = self._run(sql, params)
            except sqlite3.OperationalError as retry_exc:
                if not _is_fts5_query_error(retry_exc):
                    raise
                _logger.warning("FTS5 search failed for query %r: %s", query, retry_exc)
                rows = []
        accepted = [row for row in rows if accept(str(row[3]))][:limit]
        capped = len(rows) >= max_scan and len(accepted) < limit
        hits: list[dict[str, Any]] = []
        for rowid, entry_id, entry_kind, relpath, score in accepted:
            snippet_rows = self._run(
                "SELECT snippet(docs, 3, '<mark>', '</mark>', '...', 12) FROM docs"
                " WHERE docs MATCH ? AND rowid = ?",
                [params[0], rowid],
            )
            hits.append(
                {
                    "entry_id": entry_id,
                    "entry_kind": entry_kind,
                    "path": relpath,
                    "snippet": snippet_rows[0][0] if snippet_rows else "",
                    "score": score,
                }
            )
        return SearchResults(hits, syntax_fallback=fallback), capped


__all__ = [
    "INDEX_FILENAME",
    "MAX_FILTER_SCAN",
    "TEXT_SUFFIXES",
    "IndexedDoc",
    "Indexer",
    "SearchResults",
    "quote_phrase",
]
