"""The corpus source adapter: the FTS index and the embedding index behind ``corpus:base``.

One :class:`CorpusAdapter` is bound to one request's
:class:`~knowledge_engine.knowledge.store.ScopeSnapshot`. Its scope filter is
the set of in-scope records of the snapshot (project, collections, research
types, tags and curation all already applied by the store's SQL), keyed by the
record's corpus-relative path (the record ids of ADR 0007). The filter is
applied **before** each index ranks and cuts:

* ``fts`` -- :meth:`knowledge_engine.retrieval.fts5.Indexer.search_where`
  reads matches in BM25 order and keeps only in-scope paths until ``limit``
  are found (at most :data:`~knowledge_engine.retrieval.fts5.MAX_FILTER_SCAN`
  ranked matches are examined; reaching that cap is reported as a warning);
* ``vector`` -- ``CorpusEmbeddingIndex.search_files(accept=...)`` drops
  out-of-scope files before taking the top files;
* ``hybrid`` -- both, each with the filter, fused by reciprocal rank like the
  legacy hybrid search (bounded candidates per side).

Every path is attributed to its owning registry entry with
:class:`~knowledge_engine.retrieval.ownership.EntryOwnership` (ADR 0005); a
file no enabled entry owns is not searchable. A hit's ``revision_id`` is the
content-addressed id of the file's current bytes (ADR 0007); ``open`` serves
exactly that revision and reports ``unsupported`` for any other, because the
corpus keeps no older revisions yet.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...retrieval.embeddings.errors import EmbeddingError, EmbeddingModelMismatchError
from ...retrieval.hybrid import (
    DEFAULT_RRF_K,
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    EntryResolver,
    excerpt,
    reciprocal_rank_fusion,
)
from ..adapter import AdapterCapabilities, AdapterSearchResult
from ..contracts import (
    EffectiveScope,
    ErrorInfo,
    Locator,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    SearchHit,
    SearchMode,
    SourceState,
    SourceStatus,
)
from ..errors import IncompatibleError, KnowledgeError, UnavailableError, UnknownScopeError
from ..store import ScopeSnapshot, SnapshotRecord
from ._common import MAX_EXCERPT_CHARS, MAX_REVISION_BYTES, clip, open_result, revision_of

if TYPE_CHECKING:
    from ...core.registry import Registry
    from ...retrieval.embeddings.index import CorpusEmbeddingIndex
    from ...retrieval.fts5 import Indexer

_logger = logging.getLogger(__name__)

#: Media types of the indexable corpus suffixes.
MEDIA_TYPES = {".md": "text/markdown", ".txt": "text/plain", ".rst": "text/x-rst"}


@dataclass(frozen=True)
class CorpusBackend:
    """The long-lived corpus objects the adapter searches (owned by the caller)."""

    indexer: Indexer
    registry: Registry
    corpus_root: Path
    embeddings: CorpusEmbeddingIndex | None = None
    embeddings_error: str | None = None


def _embedding_failure(exc: EmbeddingError) -> KnowledgeError:
    details: dict[str, Any] = {"embedding_error": exc.code}
    if exc.hint:
        details["hint"] = exc.hint
    message = exc.to_dict(public=True)["detail"]
    if isinstance(exc, EmbeddingModelMismatchError):
        return IncompatibleError(str(message), details=details)
    return UnavailableError(str(message), details=details)


class CorpusAdapter:
    """Search/open of the corpus registry's files within one snapshot's scope."""

    def __init__(self, source_id: str, backend: CorpusBackend, snapshot: ScopeSnapshot) -> None:
        self._source_id = source_id
        self.backend = backend
        self._records: dict[str, SnapshotRecord] = snapshot.records.get(source_id, {})
        self._by_path = snapshot.by_path(source_id)
        self.warnings: list[str] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def describe_capabilities(self) -> AdapterCapabilities:
        modes: tuple[SearchMode, ...] = ("fts",)
        if self.backend.embeddings is not None:
            modes = ("fts", "vector", "hybrid")
        return AdapterCapabilities(
            source_id=self._source_id,
            source_kind="corpus",
            modes=modes,
            filters=("project", "collection", "research_type", "tag"),
            locator_kinds=("text", "unsupported"),
            supports_open=True,
        )

    def health(self) -> SourceStatus:
        if self.backend.indexer.closed:
            return SourceStatus(
                source_id=self._source_id,
                state=SourceState.UNAVAILABLE,
                error=ErrorInfo(code="unavailable", message="the FTS index is closed"),
            )
        return SourceStatus(source_id=self._source_id, state=SourceState.OK)

    # ── search ───────────────────────────────────────────────────

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        started = time.monotonic()
        ranked: list[tuple[str, float, str, str]] = []
        if not scope.empty and self._source_id in scope.source_ids and self._by_path:
            resolver = EntryResolver(self.backend.registry.list(enabled_only=True))

            def accept(rel_path: str) -> bool:
                return rel_path in self._by_path and resolver.accepts(rel_path)

            try:
                ranked = self._rank(context.mode, query, context.limit, accept)
            except EmbeddingError as exc:
                raise _embedding_failure(exc) from exc
            except sqlite3.Error as exc:
                _logger.error("corpus FTS index failed: %s", exc)
                raise UnavailableError("the corpus index is unavailable; retry later") from exc
            hits = [
                self._hit(rank, path, score, kind, text, scope, resolver)
                for rank, (path, score, kind, text) in enumerate(ranked, start=1)
            ]
        else:
            hits = []
        elapsed = int((time.monotonic() - started) * 1000)
        return AdapterSearchResult(
            source_id=self._source_id,
            hits=tuple(hits),
            status=SourceStatus(
                source_id=self._source_id,
                state=SourceState.OK,
                hit_count=len(hits),
                elapsed_ms=elapsed,
            ),
        )

    def _rank(
        self, mode: str, query: str, limit: int, accept: Any
    ) -> list[tuple[str, float, str, str]]:
        """``(path, native score, score kind, excerpt)`` best first, at most ``limit``."""
        if mode == "fts":
            return self._fts(query, limit, accept)
        embeddings = self.backend.embeddings
        if embeddings is None:
            raise UnavailableError(
                "vector search is unavailable: "
                + (self.backend.embeddings_error or "the corpus embedding index is not configured")
            )
        if mode == "vector":
            return [
                (hit.rel_path, hit.score, "cosine", excerpt(hit.text))
                for hit in embeddings.search_files(query, top=limit, accept=accept)
            ]
        candidates = min(max(limit * 3, MIN_CANDIDATES), MAX_CANDIDATES)
        fts = self._fts(query, candidates, accept)
        vector = embeddings.search_files(query, top=candidates, accept=accept)
        texts = {path: text for path, _, _, text in fts}
        for hit in vector:
            texts.setdefault(hit.rel_path, excerpt(hit.text))
        fused = reciprocal_rank_fusion(
            [[path for path, *_ in fts], [hit.rel_path for hit in vector]], k=DEFAULT_RRF_K
        )[:limit]
        return [(path, score, "rrf", texts[path]) for path, score in fused]

    def _fts(self, query: str, limit: int, accept: Any) -> list[tuple[str, float, str, str]]:
        results, capped = self.backend.indexer.search_where(query, limit=limit, accept=accept)
        if results.syntax_fallback:
            self.warnings.append("the query is not valid FTS5 syntax; it was searched as a phrase")
        if capped:
            self.warnings.append(
                "the filtered FTS scan reached its cap before finding enough in-scope matches"
            )
        return [
            (str(row["path"]), float(row["score"]), "bm25", str(row["snippet"] or ""))
            for row in results
        ]

    def _hit(
        self,
        rank: int,
        path: str,
        score: float,
        kind: str,
        text: str,
        scope: EffectiveScope,
        resolver: EntryResolver,
    ) -> SearchHit:
        record = self._by_path[path]
        owner = resolver.resolve(path) or {}
        data = self._read(path)
        revision = (
            revision_of(self._source_id, record.view.record_id, data)[0]
            if data is not None
            else None
        )
        return SearchHit(
            source_id=self._source_id,
            record_id=record.view.record_id,
            revision_id=revision,
            scope=RecordScope(workspace_id=scope.workspace_id, project_id=record.view.project_id),
            collection_ids=tuple(record.view.collection_ids),
            research_type=record.view.research_type,
            title=clip(record.title or path, 1000),
            excerpt=clip(text, MAX_EXCERPT_CHARS),
            provenance_id=clip(f"entry:{owner.get('id', '')}", 200) if owner else None,
            ranking=RankingInfo(native_rank=rank, native_score=score, score_kind=kind),
        )

    # ── open ─────────────────────────────────────────────────────

    def _record(self, record_id: str) -> SnapshotRecord:
        record = self._records.get(record_id)
        if record is None or record.logical_path is None:
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id} in the effective scope",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        return record

    def _read(self, rel_path: str) -> bytes | None:
        """The file's bytes, or ``None`` when missing, outside the corpus or too large."""
        root = self.backend.corpus_root.resolve()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root):
            _logger.warning("record path %r leaves the corpus root; not served", rel_path)
            return None
        try:
            if not target.is_file() or target.stat().st_size > MAX_REVISION_BYTES:
                return None
            return target.read_bytes()
        except OSError as exc:
            _logger.debug("cannot read %s: %s", rel_path, exc)
            return None

    def current_revision(self, context: QueryContext, scope: EffectiveScope, record_id: str) -> str:
        record = self._record(record_id)
        data = self._read(str(record.logical_path))
        if data is None:
            raise UnavailableError(
                f"the content of {self._source_id}/{record_id} is not available",
                details={"source_id": self._source_id, "record_id": record_id},
            )
        return revision_of(self._source_id, record_id, data)[0]

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: str,
        revision_id: str,
        locator: Locator | None = None,
    ) -> OpenResult:
        record = self._record(record_id)
        path = str(record.logical_path)
        owner = EntryResolver(self.backend.registry.list(enabled_only=True)).resolve(path)
        return open_result(
            source_id=self._source_id,
            record_id=record_id,
            requested=revision_id,
            scope=RecordScope(workspace_id=scope.workspace_id, project_id=record.view.project_id),
            media_type=MEDIA_TYPES.get(Path(path).suffix.lower(), "text/plain"),
            data=self._read(path),
            locator=locator,
            provenance_id=clip(f"entry:{owner.get('id', '')}", 200) if owner else None,
        )


__all__ = ["MEDIA_TYPES", "CorpusAdapter", "CorpusBackend"]
