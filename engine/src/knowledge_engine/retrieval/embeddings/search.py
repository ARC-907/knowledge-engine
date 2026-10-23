"""Historical search entry point of the corpus embedding index (kept for compatibility).

:func:`search` returns the ``top`` chunks most similar to a query, as dicts
with ``score``, ``rel_path``, ``library``, ``heading``, ``text`` (and
``chunk_idx``). It runs :meth:`~.index.CorpusEmbeddingIndex.search` on the
index described by the current settings; prefer that class, or
``knowledge-engine embeddings search``, in new code.

``python -m knowledge_engine.retrieval.embeddings.search "query" [--top N] [--library L]``
runs ``knowledge-engine embeddings search``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

from .index import CorpusEmbeddingIndex


def search(query: str, top: int = 10, library: str | None = None) -> list[dict[str, Any]]:
    """Return the top-N most-similar chunks for ``query``.

    ``library`` restricts the search to one top-level corpus subdirectory.
    Raises :class:`~.errors.EmbeddingIndexMissingError` (a ``FileNotFoundError``)
    when the index has not been built, and
    :class:`~.errors.EmbeddingModelMismatchError` when the configured model
    differs from the index's.
    """
    index = CorpusEmbeddingIndex.from_settings()
    return [hit.to_dict() for hit in index.search(query, top=top, library=library)]


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m knowledge_engine.retrieval.embeddings.search`` → ``knowledge-engine embeddings search``."""
    parser = argparse.ArgumentParser(description="Cosine search over the corpus embedding index.")
    parser.add_argument("query", help="The free-text query")
    parser.add_argument("--top", type=int, default=10, help="Top-N hits to return")
    parser.add_argument("--library", default=None, help="Restrict to a corpus subdirectory")
    args = parser.parse_args(argv)

    from ...cli import main as cli_main  # noqa: PLC0415 - the CLI owns logging and output

    command = ["embeddings", "search", args.query, "--top", str(args.top)]
    if args.library:
        command += ["--library", args.library]
    return cli_main(command)


__all__ = ["main", "search"]


if __name__ == "__main__":
    raise SystemExit(main())
