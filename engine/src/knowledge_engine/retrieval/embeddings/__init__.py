"""Corpus embedding index -- opt-in semantic (vector) and hybrid search.

:class:`~.index.CorpusEmbeddingIndex` chunks every markdown file under the
corpus root, embeds the chunks with an embedding provider (Ollama ``bge-m3`` by
default, see :mod:`.factory`) and stores them in their own SQLite database
together with the model, dimension and chunking that produced them. Search is
cosine similarity, streamed over the stored vectors; :mod:`knowledge_engine.retrieval.hybrid`
fuses it with the FTS5 ranking.

Operate it with ``knowledge-engine embeddings build|search|status``; HTTP
``GET /search?mode=vector|hybrid`` and the MCP ``search`` tool's ``mode`` use it.
:mod:`.build` and :mod:`.search` keep the historical functions
(``build_index``, ``get_embedding``, ``cosine_similarity``, ...) importable.
"""

from typing import Any

from .errors import (
    EmbeddingError,
    EmbeddingIndexMissingError,
    EmbeddingModelMismatchError,
    EmbeddingProviderError,
    EmbeddingUnavailableError,
)
from .index import CorpusEmbeddingIndex, IndexInfo, VectorHit

#: Historical functions re-exported from :mod:`.build` on first access, so that
#: ``python -m knowledge_engine.retrieval.embeddings.build`` does not find the
#: module already imported (which would raise a ``RuntimeWarning``).
_BUILD_EXPORTS = frozenset(
    {
        "blob_to_embedding",
        "build_index",
        "cosine_similarity",
        "embedding_to_blob",
        "get_embedding",
        "show_stats",
    }
)


def __getattr__(name: str) -> Any:
    if name in _BUILD_EXPORTS:
        from . import build  # noqa: PLC0415 - deferred on purpose (see _BUILD_EXPORTS)

        return getattr(build, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _BUILD_EXPORTS)


__all__ = [
    "CorpusEmbeddingIndex",
    "EmbeddingError",
    "EmbeddingIndexMissingError",
    "EmbeddingModelMismatchError",
    "EmbeddingProviderError",
    "EmbeddingUnavailableError",
    "IndexInfo",
    "VectorHit",
    "blob_to_embedding",
    "build_index",
    "cosine_similarity",
    "embedding_to_blob",
    "get_embedding",
    "show_stats",
]
