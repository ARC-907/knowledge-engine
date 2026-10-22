"""Embedding index — opt-in semantic-search layer over the corpus.

Builds bge-m3 (or any Ollama embedding model) embeddings of every markdown file
in the configured corpus root. Stores them in a SQLite table with cosine search.

See `build.py` for the indexer and `search.py` for retrieval helpers. All paths
and endpoints are configured via `KE_*` environment variables; defaults are
sensible for a local Ollama install.
"""

from .build import (
    blob_to_embedding,
    build_index,
    cosine_similarity,
    embedding_to_blob,
    get_embedding,
    show_stats,
)

__all__ = [
    "blob_to_embedding",
    "build_index",
    "cosine_similarity",
    "embedding_to_blob",
    "get_embedding",
    "show_stats",
]
