"""Search modes over the corpus: ``fts`` (default), ``vector`` and ``hybrid``.

* ``fts`` -- the FTS5 index (:meth:`knowledge_engine.retrieval.fts5.Indexer.search`),
  unchanged: BM25 ranking, ``<mark>`` snippets, the phrase fallback.
* ``vector`` -- the corpus embedding index
  (:class:`knowledge_engine.retrieval.embeddings.index.CorpusEmbeddingIndex`):
  files ranked by the cosine similarity of their best chunk.
* ``hybrid`` -- both rankings merged with reciprocal rank fusion
  (:func:`reciprocal_rank_fusion`, ``k = 60``): a document's fused score is the
  sum of ``1 / (k + rank)`` over the rankings it appears in.

Every mode answers with the same hit shape -- ``entry_id``, ``entry_kind``,
``path``, ``snippet``, ``score`` -- so ``score`` is "lower is better" in every
mode: the BM25 rank (``fts``), the negated cosine similarity (``vector``) or
the negated fused score (``hybrid``). ``vector`` and ``hybrid`` hits add
``vector_score`` (cosine, higher is better) when the vector side found the
document; ``hybrid`` hits add ``fused_score`` (higher is better) and
``fts_score`` (BM25) when FTS found it.

Vector hits are restricted to files of *enabled* registry entries (and to
``kind``) before the result is cut to ``limit``, exactly as the FTS index is
built from enabled entries only; a vector-only hit's snippet is a plain
excerpt of its best chunk (no ``<mark>``).
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Sequence
from typing import TYPE_CHECKING, Any, Literal, get_args

from ..core.registry import Registry
from .embeddings.errors import EmbeddingUnavailableError
from .fts5 import Indexer, SearchResults

if TYPE_CHECKING:
    from .embeddings.index import CorpusEmbeddingIndex, VectorHit

_logger = logging.getLogger(__name__)

SearchMode = Literal["fts", "vector", "hybrid"]
#: Accepted ``mode`` values, default first.
SEARCH_MODES: tuple[SearchMode, ...] = get_args(SearchMode)
DEFAULT_MODE: SearchMode = "fts"
#: The RRF constant: larger values flatten the advantage of top ranks.
DEFAULT_RRF_K = 60
#: Candidates taken from each ranking before fusing (at least this many).
MIN_CANDIDATES = 30
#: Upper bound on candidates per ranking.
MAX_CANDIDATES = 200
#: Characters of a vector-only hit's excerpt.
EXCERPT_CHARS = 240


def parse_mode(value: str | None) -> SearchMode:
    """``value`` as a :data:`SearchMode` (``None``/blank → ``fts``); else ``ValueError``."""
    text = (value or "").strip().lower() or DEFAULT_MODE
    if text not in SEARCH_MODES:
        raise ValueError(f"unknown search mode {value!r}; use one of: {', '.join(SEARCH_MODES)}")
    return text


def reciprocal_rank_fusion[K: Hashable](
    rankings: Sequence[Sequence[K]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[K, float]]:
    """Fuse ranked lists of keys with reciprocal rank fusion.

    Each ranking lists keys best first (rank 1). A key's fused score is
    ``sum(1 / (k + rank))`` over the rankings that contain it (a key repeated
    within one ranking counts at its best rank only). Returns ``(key, score)``
    pairs by descending score; ties keep the order in which keys first
    appear (ranking by ranking), so the result is deterministic.
    """
    if k < 0:
        raise ValueError("k must be >= 0")
    scores: dict[K, float] = {}
    first_seen: dict[K, int] = {}
    for ranking in rankings:
        seen_here: set[K] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen_here:
                continue
            seen_here.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(key, len(first_seen))
    return sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]]))


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """``text`` with whitespace collapsed, cut to ``limit`` characters (``...`` when cut)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


class EntryResolver:
    """Map corpus-relative file paths to the enabled registry entry that contains them.

    The longest matching entry path wins (nested entries). ``kind`` restricts
    the candidates to one entry kind.
    """

    def __init__(self, entries: Sequence[dict[str, Any]], kind: str | None = None) -> None:
        prefixes: list[tuple[str, dict[str, Any]]] = []
        for entry in entries:
            if kind and entry.get("kind") != kind:
                continue
            raw = str(entry.get("path") or "").replace("\\", "/").strip("/")
            if raw:
                prefixes.append((raw, entry))
        prefixes.sort(key=lambda item: len(item[0]), reverse=True)
        self._prefixes = prefixes
        self._cache: dict[str, dict[str, Any] | None] = {}

    def resolve(self, rel_path: str) -> dict[str, Any] | None:
        if rel_path in self._cache:
            return self._cache[rel_path]
        found: dict[str, Any] | None = None
        for prefix, entry in self._prefixes:
            if rel_path == prefix or rel_path.startswith(prefix + "/"):
                found = entry
                break
        self._cache[rel_path] = found
        return found

    def accepts(self, rel_path: str) -> bool:
        return self.resolve(rel_path) is not None


class HybridSearcher:
    """Run a search in any :data:`SearchMode` over one engine's indexes.

    ``embeddings`` may be ``None`` (vector search not configured);
    ``embeddings_error`` then explains why, and a ``vector``/``hybrid``
    search raises :class:`EmbeddingUnavailableError` with it. ``fts`` never
    touches the embedding index.
    """

    def __init__(
        self,
        indexer: Indexer,
        registry: Registry,
        embeddings: CorpusEmbeddingIndex | None = None,
        *,
        embeddings_error: str | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        if rrf_k < 0:
            raise ValueError("rrf_k must be >= 0")
        self.indexer = indexer
        self.registry = registry
        self.embeddings = embeddings
        self.embeddings_error = embeddings_error
        self.rrf_k = rrf_k

    def search(
        self,
        query: str,
        *,
        mode: str = DEFAULT_MODE,
        limit: int = 20,
        kind: str | None = None,
    ) -> SearchResults:
        """Hits for ``query`` in ``mode`` (see the module docstring for the shape)."""
        chosen = parse_mode(mode)
        if limit < 1:
            raise ValueError("limit must be a positive integer")
        if chosen == "fts":
            return self.indexer.search(query, limit=limit, kind=kind)
        embeddings = self._require_embeddings()
        resolver = EntryResolver(self.registry.list(enabled_only=True), kind)
        if chosen == "vector":
            hits = embeddings.search_files(query, top=limit, accept=resolver.accepts)
            return SearchResults((self._vector_hit(hit, resolver) for hit in hits), mode="vector")
        return self._hybrid(query, limit=limit, kind=kind, embeddings=embeddings, resolver=resolver)

    # ── internals ────────────────────────────────────────────────

    def _require_embeddings(self) -> CorpusEmbeddingIndex:
        if self.embeddings is None:
            reason = self.embeddings_error or "the corpus embedding index is not configured"
            raise EmbeddingUnavailableError(
                f"vector search is unavailable: {reason}",
                hint="configure KE_EMBED_PROVIDER / KE_EMBED_MODEL and run "
                "`knowledge-engine embeddings build`",
            )
        return self.embeddings

    @staticmethod
    def _vector_hit(hit: VectorHit, resolver: EntryResolver) -> dict[str, Any]:
        entry = resolver.resolve(hit.rel_path) or {}
        return {
            "entry_id": entry.get("id", ""),
            "entry_kind": entry.get("kind", ""),
            "path": hit.rel_path,
            "snippet": excerpt(hit.text),
            "score": -hit.score,
            "vector_score": hit.score,
        }

    def _hybrid(
        self,
        query: str,
        *,
        limit: int,
        kind: str | None,
        embeddings: CorpusEmbeddingIndex,
        resolver: EntryResolver,
    ) -> SearchResults:
        candidates = min(max(limit * 3, MIN_CANDIDATES), MAX_CANDIDATES)
        fts_hits = self.indexer.search(query, limit=candidates, kind=kind)
        vector_hits = embeddings.search_files(query, top=candidates, accept=resolver.accepts)

        docs: dict[tuple[str, str], dict[str, Any]] = {}
        fts_order: list[tuple[str, str]] = []
        for hit in fts_hits:
            key = (str(hit["entry_id"]), str(hit["path"]))
            if key not in docs:
                docs[key] = {**hit, "fts_score": hit["score"]}
            fts_order.append(key)
        vector_order: list[tuple[str, str]] = []
        for vhit in vector_hits:
            row = self._vector_hit(vhit, resolver)
            key = (str(row["entry_id"]), vhit.rel_path)
            if key in docs:
                docs[key]["vector_score"] = vhit.score
            else:
                docs[key] = row
            vector_order.append(key)

        fused = reciprocal_rank_fusion([fts_order, vector_order], k=self.rrf_k)[:limit]
        results: list[dict[str, Any]] = []
        for key, score in fused:
            row = dict(docs[key])
            row["fused_score"] = score
            row["score"] = -score
            results.append(row)
        _logger.debug(
            "hybrid search %r: %d fts + %d vector candidates -> %d hits",
            query,
            len(fts_hits),
            len(vector_hits),
            len(results),
        )
        return SearchResults(
            results,
            syntax_fallback=bool(getattr(fts_hits, "syntax_fallback", False)),
            mode="hybrid",
        )


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_RRF_K",
    "SEARCH_MODES",
    "EntryResolver",
    "HybridSearcher",
    "SearchMode",
    "excerpt",
    "parse_mode",
    "reciprocal_rank_fusion",
]
