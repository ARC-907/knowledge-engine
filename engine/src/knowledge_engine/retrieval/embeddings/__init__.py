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

from .build import (
    blob_to_embedding,
    build_index,
    cosine_similarity,
    embedding_to_blob,
    get_embedding,
    show_stats,
)
from .errors import (
    EmbeddingError,
    EmbeddingIndexMissingError,
    EmbeddingModelMismatchError,
    EmbeddingProviderError,
    EmbeddingUnavailableError,
)
from .index import CorpusEmbeddingIndex, IndexInfo, VectorHit

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
