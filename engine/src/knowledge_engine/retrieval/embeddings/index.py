"""Corpus embedding index: chunk, embed, store and search the corpus by meaning.

:class:`CorpusEmbeddingIndex` walks every markdown file under the corpus root,
splits it into overlapping chunks (:func:`chunk_text`), embeds the chunks with
an :class:`~knowledge_engine.project_context.embeddings.providers.EmbeddingProvider`
in batches and stores chunk text plus packed vector in its own SQLite database
(``KE_EMBEDDINGS_DB``, default ``<data_dir>/embeddings.db``), separate from the
FTS5 index.

The database records what produced it, in the one-row ``embedding_index``
table: provider, model, vector dimension, vector format, chunking parameters
and creation/update times. The index never mixes models: building with another
model (or a vector of another dimension) and querying with another model both
raise :class:`~.errors.EmbeddingModelMismatchError`, which says how to rebuild.

Chunking (unchanged from the original builder): chunks of about
``chunk_size`` (1500) characters with ``chunk_overlap`` (200) characters of
overlap, cut at line boundaries, each labelled with the nearest preceding
markdown heading. Only the first ``max_embed_chars`` (2000) characters of a
chunk are sent to the model -- a chunk can exceed ``chunk_size`` when a single
line is very long -- while the full chunk text is stored and returned.

Search streams the stored vectors from SQLite in pages and keeps only the best
``top`` candidates, so memory does not grow with the index. Nothing here runs
at import time; every connection is opened per call and closed again, so one
instance can be shared across threads.
"""

from __future__ import annotations

import heapq
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ...core.ollama import DEFAULT_EMBED_BATCH, OllamaError
from ...core.util.hashing import sha256_hex
from ...core.util.sqlite import connect as sqlite_connect, transaction
from ...core.util.time import utc_now_iso
from ...core.util.vectors import (
    VECTOR_FORMAT_VERSION,
    CosineScorer,
    VectorDimensionError,
    pack_vector,
    unpack_vector,
)
from .errors import (
    BUILD_COMMAND,
    REBUILD_COMMAND,
    EmbeddingError,
    EmbeddingIndexMissingError,
    EmbeddingModelMismatchError,
    EmbeddingProviderError,
)

if TYPE_CHECKING:
    from ...core.settings import Settings
    from ...project_context.embeddings.providers import EmbeddingProvider

_logger = logging.getLogger(__name__)

#: Version of the tables below; stored in ``embedding_index.schema_version``.
SCHEMA_VERSION = 1
#: Characters per chunk (bge-m3's context is ~8192 tokens).
CHUNK_SIZE = 1500
#: Characters of overlap between consecutive chunks.
CHUNK_OVERLAP = 200
#: Characters of a chunk sent to the model (the rest is stored, not embedded).
MAX_EMBED_CHARS = 2000
#: Files whose stripped text is shorter than this are skipped.
MIN_FILE_CHARS = 50
#: Rows fetched per page while scanning vectors.
SCAN_PAGE = 256

#: Directory names skipped during the corpus walk (dot-directories are skipped too).
SKIP_DIRS = frozenset(
    {
        ".history",
        ".vscode",
        "node_modules",
        "__pycache__",
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)

#: Tables of the pre-3.0 builder (no model/dim recorded); dropped by ``--rebuild``.
LEGACY_TABLES = ("chunks", "files")
_TABLES = ("embedding_chunks", "embedding_files", "embedding_index")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_index (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL DEFAULT 0,
    vector_format INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    max_embed_chars INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_files (
    id INTEGER PRIMARY KEY,
    rel_path TEXT UNIQUE NOT NULL,
    file_hash TEXT NOT NULL,
    library TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    total_chunks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS embedding_chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES embedding_files(id) ON DELETE CASCADE,
    chunk_idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    heading TEXT,
    embedding BLOB NOT NULL,
    UNIQUE(file_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS idx_embedding_chunks_file ON embedding_chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_embedding_files_library ON embedding_files(library);
"""


# ── Value types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """One chunk of a file: its text and the nearest preceding heading."""

    text: str
    heading: str


@dataclass(frozen=True)
class IndexInfo:
    """What an index was built with (the ``embedding_index`` row)."""

    schema_version: int
    provider: str
    model: str
    dim: int
    vector_format: int
    chunk_size: int
    chunk_overlap: int
    max_embed_chars: int
    created_at: str
    updated_at: str

    @property
    def chunking(self) -> tuple[int, int, int]:
        return (self.chunk_size, self.chunk_overlap, self.max_embed_chars)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VectorHit:
    """One vector-search hit: a chunk and its cosine similarity to the query."""

    score: float
    rel_path: str
    library: str
    heading: str
    text: str
    chunk_idx: int

    def to_dict(self) -> dict[str, Any]:
        """The historical result shape (``score``, ``rel_path``, ``library``, ``heading``, ``text``)."""
        return asdict(self)


@dataclass
class _PendingFile:
    rel_path: str
    file_hash: str
    library: str
    chunks: list[Chunk]


# ── Helpers ──────────────────────────────────────────────────────────


def chunk_text(
    text: str,
    path_rel: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split ``text`` into overlapping chunks, tracking the nearest heading.

    Lines accumulate until the buffer reaches ``chunk_size`` characters; the
    next chunk starts with the trailing lines that make up at least
    ``chunk_overlap`` characters. The heading of a chunk is the last markdown
    heading seen (``path_rel`` before the first one).
    """
    lines = text.split("\n")
    chunks: list[Chunk] = []
    current_heading = path_rel
    buf: list[str] = []
    buf_len = 0

    for line in lines:
        if line.startswith("#"):
            current_heading = line.lstrip("#").strip()

        buf.append(line)
        buf_len += len(line) + 1

        if buf_len >= chunk_size:
            chunks.append(Chunk("\n".join(buf), current_heading))
            overlap_chars = 0
            overlap_start = len(buf)
            for i in range(len(buf) - 1, -1, -1):
                overlap_chars += len(buf[i]) + 1
                if overlap_chars >= chunk_overlap:
                    overlap_start = i
                    break
            buf = buf[overlap_start:]
            buf_len = sum(len(item) + 1 for item in buf)

    if buf:
        chunks.append(Chunk("\n".join(buf), current_heading))
    return chunks


def library_of(rel_path: str) -> str:
    """The top-level corpus subdirectory of ``rel_path`` (``unknown`` when there is none)."""
    parts = Path(rel_path).parts
    return parts[0] if parts else "unknown"


def same_model(a: str, b: str) -> bool:
    """True when two model names denote the same model (``x`` and ``x:latest`` match)."""

    def norm(name: str) -> str:
        text = name.strip()
        return text[: -len(":latest")] if text.endswith(":latest") else text

    return norm(a) == norm(b)


def _is_unreachable(exc: BaseException) -> bool:
    """True when ``exc`` (or its cause chain) is a transport-level failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TransportError):
            return True
        if isinstance(current, OllamaError) and current.status_code is None:
            return True
        current = current.__cause__
    return False


def _drop_tables(conn: sqlite3.Connection, names: Sequence[str]) -> None:
    for name in names:
        # Names come from the module constants above, never from input.
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def _read_info(conn: sqlite3.Connection) -> IndexInfo | None:
    if "embedding_index" not in _table_names(conn):
        return None
    row = conn.execute(
        "SELECT schema_version, provider, model, dim, vector_format, chunk_size, "
        "chunk_overlap, max_embed_chars, created_at, updated_at "
        "FROM embedding_index WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return IndexInfo(
        schema_version=int(row[0]),
        provider=str(row[1]),
        model=str(row[2]),
        dim=int(row[3]),
        vector_format=int(row[4]),
        chunk_size=int(row[5]),
        chunk_overlap=int(row[6]),
        max_embed_chars=int(row[7]),
        created_at=str(row[8]),
        updated_at=str(row[9]),
    )


# ── The index ────────────────────────────────────────────────────────


class CorpusEmbeddingIndex:
    """The corpus embedding index stored in ``db_path``, embedded by ``provider``.

    ``corpus_root`` is needed by :meth:`build` only. ``batch_size`` chunks are
    embedded per provider call while building (chunks of several files share
    a batch). Construction performs no I/O.
    """

    def __init__(
        self,
        db_path: Path | str,
        provider: EmbeddingProvider,
        *,
        corpus_root: Path | str | None = None,
        batch_size: int = DEFAULT_EMBED_BATCH,
        chunk_size: int = CHUNK_SIZE,
        chunk_overlap: int = CHUNK_OVERLAP,
        max_embed_chars: int = MAX_EMBED_CHARS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")
        if max_embed_chars < 1:
            raise ValueError("max_embed_chars must be a positive integer")
        self._db_path = Path(db_path).expanduser().resolve()
        self._provider = provider
        self._corpus_root = (
            Path(corpus_root).expanduser().resolve() if corpus_root is not None else None
        )
        self.batch_size = int(batch_size)
        self.chunk_size = int(chunk_size)
        self.chunk_overlap = int(chunk_overlap)
        self.max_embed_chars = int(max_embed_chars)
        self._embedded_any = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        provider: str | EmbeddingProvider | None = None,
        model: str | None = None,
        batch_size: int | None = None,
        db_path: Path | str | None = None,
        **chunking: int,
    ) -> CorpusEmbeddingIndex:
        """The index described by ``settings`` (default: the current environment).

        ``provider`` is a provider name (``ollama``/``remote``/``fake``) or a
        ready provider; ``model``, ``batch_size`` and ``db_path`` override
        ``KE_EMBED_MODEL``, ``KE_EMBED_BATCH`` and ``KE_EMBEDDINGS_DB``.
        Raises :class:`~.errors.EmbeddingUnavailableError` when the provider
        cannot be configured.
        """
        from ...core.settings import Settings  # noqa: PLC0415 - keeps import light
        from .factory import build_provider, default_db_path  # noqa: PLC0415

        settings = settings if settings is not None else Settings()
        batch = batch_size if batch_size is not None else settings.embed_batch
        if provider is None or isinstance(provider, str):
            resolved = build_provider(settings, provider=provider, model=model, batch_size=batch)
        else:
            resolved = provider
        return cls(
            db_path if db_path is not None else default_db_path(settings),
            resolved,
            corpus_root=settings.resolve_paths(warn_missing_corpus=False).corpus_root,
            batch_size=batch,
            **chunking,
        )

    def __repr__(self) -> str:
        return f"CorpusEmbeddingIndex(db_path={str(self._db_path)!r}, provider={self._provider!r})"

    # ── properties ───────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    @property
    def corpus_root(self) -> Path | None:
        return self._corpus_root

    @property
    def chunking(self) -> tuple[int, int, int]:
        return (self.chunk_size, self.chunk_overlap, self.max_embed_chars)

    def exists(self) -> bool:
        """True when the index database file exists (it may still be empty)."""
        return self._db_path.is_file()

    # ── connections ──────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite_connect(self._db_path, row_factory=None)

    def _open_existing(self) -> sqlite3.Connection:
        if not self.exists():
            raise EmbeddingIndexMissingError(
                f"no embedding index at {self._db_path}",
                hint=f"run `{BUILD_COMMAND}` first",
                details={"db_path": str(self._db_path)},
            )
        return sqlite_connect(self._db_path, row_factory=None, create_parent=False)

    def _missing(self, conn: sqlite3.Connection) -> EmbeddingIndexMissingError:
        legacy = [name for name in LEGACY_TABLES if name in _table_names(conn)]
        if legacy:
            return EmbeddingIndexMissingError(
                f"{self._db_path} holds an embedding index from an older release "
                "(no model recorded); it cannot be searched",
                hint=f"run `{REBUILD_COMMAND}` to rebuild it",
                details={"db_path": str(self._db_path), "legacy_tables": legacy},
            )
        return EmbeddingIndexMissingError(
            f"the embedding index at {self._db_path} has not been built",
            hint=f"run `{BUILD_COMMAND}` first",
            details={"db_path": str(self._db_path)},
        )

    # ── info / status ────────────────────────────────────────────

    def info(self) -> IndexInfo | None:
        """What the index was built with; ``None`` when it does not exist (yet)."""
        if not self.exists():
            return None
        conn = self._open_existing()
        try:
            return _read_info(conn)
        finally:
            conn.close()

    def _mismatch(
        self, info: IndexInfo, *, model: str | None = None, dim: int | None = None
    ) -> EmbeddingModelMismatchError:
        offered_model = model if model is not None else self._provider.model_id
        what = (
            f"model {offered_model!r}"
            if dim is None
            else f"model {offered_model!r} ({dim}-dimensional vectors)"
        )
        restore = f"KE_EMBED_MODEL={info.model}"
        if info.provider != self._provider.name:
            restore = f"KE_EMBED_PROVIDER={info.provider} {restore}"
        return EmbeddingModelMismatchError(
            f"the embedding index at {self._db_path} was built with model {info.model!r} "
            f"({info.dim}-dimensional vectors) and cannot take vectors from {what}",
            hint=(
                f"rebuild the index with `{REBUILD_COMMAND}` (uses KE_EMBED_MODEL / --model), "
                f"or set {restore} to use the existing index"
            ),
            details={
                "db_path": str(self._db_path),
                "index_provider": info.provider,
                "index_model": info.model,
                "index_dim": info.dim,
                "provider": self._provider.name,
                "model": offered_model,
                "dim": dim,
            },
        )

    def _check_model(self, info: IndexInfo) -> None:
        if not same_model(info.model, self._provider.model_id):
            raise self._mismatch(info)
        provider_dim = self._provider.dim
        if provider_dim and info.dim and provider_dim != info.dim:
            raise self._mismatch(info, dim=provider_dim)

    def status(self) -> dict[str, Any]:
        """A JSON-safe report: what the index holds and whether the provider matches it."""
        report: dict[str, Any] = {
            "db_path": str(self._db_path),
            "exists": self.exists(),
            "built": False,
            "configured": {"provider": self._provider.name, "model": self._provider.model_id},
        }
        if not report["exists"]:
            return report
        conn = self._open_existing()
        try:
            tables = _table_names(conn)
            report["legacy_tables"] = [name for name in LEGACY_TABLES if name in tables]
            info = _read_info(conn)
            if info is None:
                return report
            files, incomplete = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(file_hash = ''), 0) FROM embedding_files"
            ).fetchone()
            chunks = conn.execute("SELECT COUNT(*) FROM embedding_chunks").fetchone()[0]
            libraries = conn.execute(
                "SELECT library, COUNT(*), COALESCE(SUM(total_chunks), 0) FROM embedding_files "
                "GROUP BY library ORDER BY COUNT(*) DESC, library"
            ).fetchall()
        finally:
            conn.close()
        report.update(
            {
                "built": True,
                **info.to_dict(),
                "files": int(files),
                "incomplete_files": int(incomplete),
                "chunks": int(chunks),
                "libraries": [
                    {"library": str(lib), "files": int(count), "chunks": int(total)}
                    for lib, count, total in libraries
                ],
                "size_mb": round(self._db_path.stat().st_size / 1024 / 1024, 2),
                "compatible": same_model(info.model, self._provider.model_id),
            }
        )
        return report

    # ── build ────────────────────────────────────────────────────

    def _walk(self, root: Path) -> list[Path]:
        found: list[Path] = []
        for md_path in root.rglob("*.md"):
            parts = md_path.relative_to(root).parts
            if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
                continue
            if md_path.is_file():
                found.append(md_path)
        found.sort()
        return found

    @staticmethod
    def _has_chunks(conn: sqlite3.Connection) -> bool:
        return conn.execute("SELECT 1 FROM embedding_chunks LIMIT 1").fetchone() is not None

    def _start_info(self, conn: sqlite3.Connection, incremental: bool) -> tuple[IndexInfo, bool]:
        """Create or validate the index row; returns it and the effective ``incremental``."""
        info = _read_info(conn)
        now = utc_now_iso()
        if info is None:
            dim = self._provider.dim
            with transaction(conn):
                conn.execute(
                    "INSERT INTO embedding_index (id, schema_version, provider, model, dim, "
                    "vector_format, chunk_size, chunk_overlap, max_embed_chars, created_at, "
                    "updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        SCHEMA_VERSION,
                        self._provider.name,
                        self._provider.model_id,
                        dim,
                        VECTOR_FORMAT_VERSION,
                        self.chunk_size,
                        self.chunk_overlap,
                        self.max_embed_chars,
                        now,
                        now,
                    ),
                )
            created = _read_info(conn)
            assert created is not None  # noqa: S101 - just inserted
            return created, incremental
        if not same_model(info.model, self._provider.model_id) and not self._has_chunks(conn):
            # Nothing was ever stored (e.g. a first build that failed): adopt the new model.
            _logger.info(
                "embedding index %s is empty; switching it from model %s to %s",
                self._db_path,
                info.model,
                self._provider.model_id,
            )
            with transaction(conn):
                conn.execute("DELETE FROM embedding_files")
                conn.execute("DELETE FROM embedding_index")
            return self._start_info(conn, incremental)
        self._check_model(info)
        if info.provider != self._provider.name:
            _logger.warning(
                "embedding index %s was built by provider %s; continuing with %s (same model %s)",
                self._db_path,
                info.provider,
                self._provider.name,
                info.model,
            )
        if info.chunking != self.chunking:
            _logger.warning(
                "chunking parameters changed (index %s, now %s); re-embedding every file",
                info.chunking,
                self.chunking,
            )
            incremental = False
            with transaction(conn):
                conn.execute(
                    "UPDATE embedding_index SET chunk_size = ?, chunk_overlap = ?, "
                    "max_embed_chars = ?, updated_at = ? WHERE id = 1",
                    (*self.chunking, now),
                )
            refreshed = _read_info(conn)
            assert refreshed is not None  # noqa: S101 - the row exists
            info = refreshed
        return info, incremental

    def _embed_texts(
        self, texts: list[str], *, tolerate_total_failure: bool = False
    ) -> list[list[float] | None]:
        """Embed ``texts`` in one provider call; on failure retry each text alone.

        A text whose retry fails is ``None`` (logged at WARNING). Raises
        :class:`EmbeddingProviderError` when the provider is unreachable, and
        when nothing in the batch could be embedded unless
        ``tolerate_total_failure`` (set once earlier batches of the same build
        succeeded, so one unembeddable chunk does not abort a long build).
        """
        provider = self._provider
        try:
            vectors = provider.embed(texts)
            if len(vectors) != len(texts):
                raise EmbeddingProviderError(
                    f"provider {provider.name} returned {len(vectors)} vectors for {len(texts)} texts"
                )
            return list(vectors)
        except EmbeddingError:
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            if _is_unreachable(exc):
                raise self._provider_error(exc) from exc
            first_failure: Exception = exc
        out: list[list[float] | None] = []
        if len(texts) == 1:
            _logger.warning("embedding failed for one text: %s", first_failure)
            out.append(None)
        else:
            _logger.info(
                "batched embed of %d texts failed (%s); retrying one by one",
                len(texts),
                first_failure,
            )
            for position, text in enumerate(texts):
                try:
                    [vector] = provider.embed([text])
                except (RuntimeError, OSError, ValueError) as exc:
                    if _is_unreachable(exc):
                        raise self._provider_error(exc) from exc
                    _logger.warning("embedding failed for text %d of the batch: %s", position, exc)
                    first_failure = exc
                    out.append(None)
                    continue
                out.append(vector)
        if not tolerate_total_failure and all(vector is None for vector in out):
            raise self._provider_error(first_failure) from first_failure
        return out

    def _provider_error(self, exc: BaseException) -> EmbeddingProviderError:
        return EmbeddingProviderError(
            f"embedding provider {self._provider.name} failed: {exc}",
            hint="check that the provider is running and the model is available "
            "(for Ollama: `ollama pull <model>`, KE_OLLAMA_URL)",
            details={"provider": self._provider.name, "model": self._provider.model_id},
        )

    def _flush(
        self, conn: sqlite3.Connection, info: IndexInfo, pending: list[_PendingFile]
    ) -> tuple[IndexInfo, int, int]:
        """Embed and store the pending files; returns (info, chunks written, chunk errors)."""
        texts = [chunk.text[: self.max_embed_chars] for item in pending for chunk in item.chunks]
        vectors = self._embed_texts(texts, tolerate_total_failure=self._embedded_any)
        for vector in vectors:
            if vector is None:
                continue
            if not vector:
                raise EmbeddingProviderError(
                    f"provider {self._provider.name} returned an empty vector"
                )
            if info.dim == 0:
                with transaction(conn):
                    conn.execute(
                        "UPDATE embedding_index SET dim = ? WHERE id = 1 AND dim = 0",
                        (len(vector),),
                    )
                updated = _read_info(conn)
                assert updated is not None  # noqa: S101 - the row exists
                info = updated
            if len(vector) != info.dim:
                raise self._mismatch(info, dim=len(vector))
            self._embedded_any = True
        written = 0
        errors = 0
        now = utc_now_iso()
        position = 0
        with transaction(conn, immediate=True):
            for item in pending:
                file_vectors = vectors[position : position + len(item.chunks)]
                position += len(item.chunks)
                complete = all(vector is not None for vector in file_vectors)
                conn.execute(
                    "INSERT INTO embedding_files (rel_path, file_hash, library, indexed_at, total_chunks) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(rel_path) DO UPDATE SET "
                    "file_hash = excluded.file_hash, library = excluded.library, "
                    "indexed_at = excluded.indexed_at, total_chunks = excluded.total_chunks",
                    # An incomplete file keeps an empty hash so --incremental retries it.
                    (
                        item.rel_path,
                        item.file_hash if complete else "",
                        item.library,
                        now,
                        len(item.chunks),
                    ),
                )
                file_id = conn.execute(
                    "SELECT id FROM embedding_files WHERE rel_path = ?", (item.rel_path,)
                ).fetchone()[0]
                conn.execute("DELETE FROM embedding_chunks WHERE file_id = ?", (file_id,))
                for idx, (chunk, vector) in enumerate(zip(item.chunks, file_vectors, strict=True)):
                    if vector is None:
                        errors += 1
                        continue
                    conn.execute(
                        "INSERT INTO embedding_chunks (file_id, chunk_idx, text, heading, embedding) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (file_id, idx, chunk.text, chunk.heading, pack_vector(vector)),
                    )
                    written += 1
        return info, written, errors

    def build(self, *, incremental: bool = False, rebuild: bool = False) -> dict[str, Any]:
        """Embed the corpus into the index and return a stats dict.

        * ``incremental`` -- skip files whose content hash is unchanged.
        * ``rebuild`` -- drop the index (and any pre-3.0 tables) first; the
          only way to switch to another model.

        Files that disappeared from the corpus are removed from the index.
        Raises :class:`~.errors.EmbeddingModelMismatchError` when the provider's
        model differs from the index's, :class:`~.errors.EmbeddingProviderError`
        when the provider is unreachable, and ``ValueError`` when the corpus
        root is missing.
        """
        root = self._corpus_root
        if root is None:
            raise ValueError("this index has no corpus_root; pass one to build it")
        if not root.is_dir():
            raise ValueError(f"corpus root {root} does not exist (set KE_CORPUS_ROOT or KE_HOME)")
        start = time.monotonic()
        self._embedded_any = False
        conn = self._connect()
        try:
            if rebuild:
                with transaction(conn):
                    _drop_tables(conn, (*_TABLES, *LEGACY_TABLES))
                _logger.info("embedding index %s dropped for rebuild", self._db_path)
            conn.executescript(_SCHEMA)
            legacy = [name for name in LEGACY_TABLES if name in _table_names(conn)]
            if legacy:
                _logger.warning(
                    "%s still holds pre-3.0 embedding tables %s; they are ignored "
                    "(run `%s` to drop them)",
                    self._db_path,
                    legacy,
                    REBUILD_COMMAND,
                )
            info, incremental = self._start_info(conn, incremental)
            stats = self._build_files(conn, root, info, incremental)
            with transaction(conn):
                conn.execute(
                    "UPDATE embedding_index SET updated_at = ? WHERE id = 1", (utc_now_iso(),)
                )
            final = _read_info(conn)
        finally:
            conn.close()
        elapsed = time.monotonic() - start
        stats.update(
            {
                "elapsed_seconds": round(elapsed, 3),
                "db_path": str(self._db_path),
                "provider": self._provider.name,
                "model": final.model if final else self._provider.model_id,
                "dim": final.dim if final else 0,
            }
        )
        _logger.info(
            "embedding index built in %.1fs: %d files indexed, %d skipped, %d pruned, "
            "%d chunks, %d errors (%s)",
            elapsed,
            stats["indexed"],
            stats["skipped"],
            stats["pruned"],
            stats["chunks"],
            stats["errors"],
            self._db_path,
        )
        return stats

    def _build_files(
        self, conn: sqlite3.Connection, root: Path, info: IndexInfo, incremental: bool
    ) -> dict[str, Any]:
        paths = self._walk(root)
        _logger.info("found %d markdown files under %s", len(paths), root)
        existing = {
            str(rel): str(digest)
            for rel, digest in conn.execute("SELECT rel_path, file_hash FROM embedding_files")
        }
        current = {path.relative_to(root).as_posix() for path in paths}
        gone = sorted(set(existing) - current)
        if gone:
            with transaction(conn):
                conn.executemany(
                    "DELETE FROM embedding_files WHERE rel_path = ?", [(rel,) for rel in gone]
                )
            _logger.info(
                "removed %d files that left the corpus from the embedding index", len(gone)
            )

        stats = {"indexed": 0, "skipped": 0, "errors": 0, "chunks": 0, "pruned": len(gone)}
        pending: list[_PendingFile] = []
        pending_chunks = 0
        for number, path in enumerate(paths, start=1):
            rel = path.relative_to(root).as_posix()
            try:
                digest = sha256_hex(path, length=16)
            except OSError as exc:
                _logger.warning("cannot read %s: %s", path, exc)
                stats["errors"] += 1
                continue
            if incremental and existing.get(rel) == digest:
                stats["skipped"] += 1
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                _logger.warning("cannot read %s: %s", path, exc)
                stats["errors"] += 1
                continue
            if len(text.strip()) < MIN_FILE_CHARS:
                stats["skipped"] += 1
                continue
            chunks = chunk_text(
                text, rel, chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
            )
            pending.append(_PendingFile(rel, digest, library_of(rel), chunks))
            pending_chunks += len(chunks)
            if pending_chunks >= self.batch_size:
                info = self._commit(conn, info, pending, stats)
                pending, pending_chunks = [], 0
                _logger.info(
                    "[%d/%d] %d indexed, %d skipped, %d errors, %d chunks",
                    number,
                    len(paths),
                    stats["indexed"],
                    stats["skipped"],
                    stats["errors"],
                    stats["chunks"],
                )
        if pending:
            self._commit(conn, info, pending, stats)
        return stats

    def _commit(
        self,
        conn: sqlite3.Connection,
        info: IndexInfo,
        pending: list[_PendingFile],
        stats: dict[str, Any],
    ) -> IndexInfo:
        info, written, errors = self._flush(conn, info, pending)
        stats["indexed"] += len(pending)
        stats["chunks"] += written
        stats["errors"] += errors
        return info

    # ── search ───────────────────────────────────────────────────

    def embed_query(self, query: str) -> tuple[CosineScorer, IndexInfo]:
        """Embed ``query`` after checking the provider matches the index.

        Returns a scorer for the query vector and the index info. Raises
        :class:`~.errors.EmbeddingIndexMissingError`,
        :class:`~.errors.EmbeddingModelMismatchError` or
        :class:`~.errors.EmbeddingProviderError`.
        """
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        conn = self._open_existing()
        try:
            info = _read_info(conn)
            if info is None:
                raise self._missing(conn)
        finally:
            conn.close()
        self._check_model(info)
        try:
            vectors = self._provider.embed([query])
        except EmbeddingError:
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            raise self._provider_error(exc) from exc
        if len(vectors) != 1 or not vectors[0]:
            raise EmbeddingProviderError(
                f"provider {self._provider.name} returned no vector for the query"
            )
        vector = vectors[0]
        if info.dim and len(vector) != info.dim:
            raise self._mismatch(info, dim=len(vector))
        return CosineScorer(vector), info

    def _scan(
        self, scorer: CosineScorer, info: IndexInfo, library: str | None
    ) -> Iterator[tuple[float, int, str, str, int]]:
        """Yield ``(score, chunk id, rel_path, library, chunk_idx)`` for every stored chunk."""
        sql = (
            "SELECT c.id, c.chunk_idx, c.embedding, f.rel_path, f.library "
            "FROM embedding_chunks c JOIN embedding_files f ON f.id = c.file_id"
        )
        params: tuple[Any, ...] = ()
        if library:
            sql += " WHERE f.library = ?"
            params = (library,)
        conn = self._open_existing()
        try:
            cursor = conn.execute(sql, params)
            while rows := cursor.fetchmany(SCAN_PAGE):
                for chunk_id, chunk_idx, blob, rel_path, lib in rows:
                    try:
                        score = scorer(unpack_vector(blob))
                    except VectorDimensionError as exc:
                        raise self._mismatch(info, dim=exc.right) from exc
                    except ValueError as exc:
                        _logger.warning("skipping unreadable vector of %s: %s", rel_path, exc)
                        continue
                    yield score, int(chunk_id), str(rel_path), str(lib), int(chunk_idx)
        finally:
            conn.close()

    def _hydrate(self, scored: list[tuple[float, int, str, str, int]]) -> list[VectorHit]:
        if not scored:
            return []
        ids = [item[1] for item in scored]
        placeholders = ",".join("?" for _ in ids)
        conn = self._open_existing()
        try:
            rows = conn.execute(
                f"SELECT id, text, heading FROM embedding_chunks WHERE id IN ({placeholders})", ids
            ).fetchall()
        finally:
            conn.close()
        texts = {int(row[0]): (str(row[1]), str(row[2] or "")) for row in rows}
        hits = [
            VectorHit(
                score=score,
                rel_path=rel_path,
                library=lib,
                heading=texts.get(chunk_id, ("", ""))[1],
                text=texts.get(chunk_id, ("", ""))[0],
                chunk_idx=chunk_idx,
            )
            for score, chunk_id, rel_path, lib, chunk_idx in scored
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.rel_path, hit.chunk_idx))
        return hits

    def search(self, query: str, *, top: int = 10, library: str | None = None) -> list[VectorHit]:
        """The ``top`` chunks most similar to ``query`` (several may come from one file).

        ``library`` restricts the search to one top-level corpus subdirectory.
        """
        if top < 1:
            raise ValueError("top must be a positive integer")
        scorer, info = self.embed_query(query)
        best = heapq.nlargest(top, self._scan(scorer, info, library), key=lambda item: item[0])
        return self._hydrate(best)

    def search_files(
        self,
        query: str,
        *,
        top: int = 10,
        accept: Callable[[str], bool] | None = None,
    ) -> list[VectorHit]:
        """The ``top`` files most similar to ``query``, each with its best chunk.

        ``accept(rel_path)`` filters files *before* the cut (like a SQL
        ``WHERE`` before ``LIMIT``); it is called once per file.
        """
        if top < 1:
            raise ValueError("top must be a positive integer")
        scorer, info = self.embed_query(query)
        verdicts: dict[str, bool] = {}
        best: dict[str, tuple[float, int, str, str, int]] = {}
        for item in self._scan(scorer, info, None):
            rel_path = item[2]
            allowed = verdicts.get(rel_path)
            if allowed is None:
                allowed = verdicts[rel_path] = accept(rel_path) if accept is not None else True
            if not allowed:
                continue
            current = best.get(rel_path)
            if current is None or item[0] > current[0]:
                best[rel_path] = item
        winners = heapq.nlargest(top, best.values(), key=lambda item: item[0])
        return self._hydrate(winners)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "LEGACY_TABLES",
    "MAX_EMBED_CHARS",
    "MIN_FILE_CHARS",
    "SCHEMA_VERSION",
    "SKIP_DIRS",
    "Chunk",
    "CorpusEmbeddingIndex",
    "IndexInfo",
    "VectorHit",
    "chunk_text",
    "library_of",
    "same_model",
]
