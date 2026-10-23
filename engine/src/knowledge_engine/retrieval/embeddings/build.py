"""Historical entry point of the corpus embedding index (kept for compatibility).

The index now lives in :mod:`.index` (:class:`~.index.CorpusEmbeddingIndex`)
and is operated with ``knowledge-engine embeddings build|status``. This module
keeps the original functions working on top of it:

* :func:`build_index` / :func:`show_stats` -- build / report the index
  described by the current settings (``KE_EMBED_PROVIDER``, ``KE_EMBED_MODEL``,
  ``KE_EMBEDDINGS_DB``, ...);
* :func:`get_embedding` / :func:`get_embeddings` -- embed with Ollama at
  ``KE_OLLAMA_URL`` using ``KE_EMBED_MODEL`` (raise ``OllamaError``);
* :func:`embedding_to_blob` / :func:`blob_to_embedding` /
  :func:`cosine_similarity` -- the shared vector helpers of
  :mod:`knowledge_engine.core.util.vectors` (``cosine_similarity`` raises
  ``VectorDimensionError`` for vectors of different lengths instead of
  silently truncating them).

``OLLAMA_URL``, ``EMBED_MODEL``, ``CORPUS_ROOT`` and ``DB_PATH`` are resolved
from the settings each time they are read; nothing is resolved at import.

``python -m knowledge_engine.retrieval.embeddings.build [--incremental | --rebuild | --stats]``
runs the matching ``knowledge-engine embeddings`` command.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from ...core.ollama import OllamaClient
from ...core.util.vectors import (
    cosine as cosine_similarity,
    pack_vector as embedding_to_blob,
    unpack_vector as blob_to_embedding,
)
from .factory import EMBED_TIMEOUT, default_db_path
from .index import CHUNK_OVERLAP, CHUNK_SIZE, MAX_EMBED_CHARS, SKIP_DIRS, CorpusEmbeddingIndex


def _settings() -> Any:
    from ...core.settings import Settings  # noqa: PLC0415 - read at call time

    return Settings()


def __getattr__(name: str) -> Any:
    # PEP 562: the old import-time globals, now read from the settings on access.
    if name == "OLLAMA_URL":
        return _settings().ollama_url
    if name == "EMBED_MODEL":
        return _settings().embed_model
    if name == "CORPUS_ROOT":
        return _settings().resolve_paths(warn_missing_corpus=False).corpus_root
    if name == "DB_PATH":
        return default_db_path(_settings())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` with ``KE_EMBED_MODEL`` at ``KE_OLLAMA_URL`` (batched). Raises ``OllamaError``."""
    settings = _settings()
    client = OllamaClient(settings.ollama_url, EMBED_TIMEOUT)
    return client.embed(texts, settings.embed_model, batch_size=settings.embed_batch)


def get_embedding(text: str) -> list[float]:
    """Fetch a single embedding from Ollama. Raises ``OllamaError`` on failure."""
    return get_embeddings([text])[0]


def build_index(incremental: bool = False) -> dict[str, Any]:
    """Build or update the index described by the settings. Returns a stats dict."""
    return CorpusEmbeddingIndex.from_settings(_settings()).build(incremental=incremental)


def show_stats() -> None:
    """Print index statistics (``knowledge-engine embeddings status`` prints them as JSON)."""
    report = CorpusEmbeddingIndex.from_settings(_settings()).status()
    out = sys.stdout
    if not report.get("built"):
        out.write(
            f"No embedding index at {report['db_path']}. "
            "Run `knowledge-engine embeddings build` first.\n"
        )
        return
    out.write(f"Embedding Index: {report['db_path']}\n")
    out.write(f"  Model: {report['model']} ({report['dim']} dims, provider {report['provider']})\n")
    out.write(f"  Total files: {report['files']}\n")
    out.write(f"  Total chunks: {report['chunks']}\n")
    out.write(f"  DB size: {report['size_mb']:.1f} MB\n")
    out.write("\n  Per top-level corpus subdirectory:\n")
    for row in report["libraries"]:
        out.write(f"    {row['library']}: {row['files']} files, {row['chunks']} chunks\n")


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m knowledge_engine.retrieval.embeddings.build`` → ``knowledge-engine embeddings``."""
    parser = argparse.ArgumentParser(description="Build / inspect the corpus embedding index.")
    parser.add_argument("--incremental", action="store_true", help="Only index new/changed files")
    parser.add_argument("--rebuild", action="store_true", help="Drop the index and rebuild it")
    parser.add_argument("--stats", action="store_true", help="Show index statistics and exit")
    args = parser.parse_args(argv)

    from ...cli import main as cli_main  # noqa: PLC0415 - the CLI owns logging and output

    if args.stats:
        return cli_main(["embeddings", "status"])
    command = ["embeddings", "build"]
    if args.incremental:
        command.append("--incremental")
    if args.rebuild:
        command.append("--rebuild")
    return cli_main(command)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "EMBED_TIMEOUT",
    "MAX_EMBED_CHARS",
    "SKIP_DIRS",
    "blob_to_embedding",
    "build_index",
    "cosine_similarity",
    "embedding_to_blob",
    "get_embedding",
    "get_embeddings",
    "main",
    "show_stats",
]


if __name__ == "__main__":
    raise SystemExit(main())
