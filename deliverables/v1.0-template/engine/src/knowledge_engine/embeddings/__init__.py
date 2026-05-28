"""Embedding index — opt-in semantic-search layer over the corpus.

Builds bge-m3 (or any Ollama embedding model) embeddings of every markdown file
in the configured corpus root. Stores them in a SQLite table with cosine search.

See `build.py` for the indexer and `search.py` for retrieval helpers. All paths
and endpoints are configured via `KE_*` environment variables; defaults are
sensible for a local Ollama install.
"""

from .build import (
    build_index,
    show_stats,
    get_embedding,
    cosine_similarity,
    embedding_to_blob,
    blob_to_embedding,
)

__all__ = [
    "build_index",
    "show_stats",
    "get_embedding",
    "cosine_similarity",
    "embedding_to_blob",
    "blob_to_embedding",
]
