"""``knowledge-engine embeddings build|search|status``: the corpus embedding index.

Each command prints JSON on stdout. A failure the index reports -- no index
yet, a model mismatch, an unreachable provider -- prints the structured error
(``{"error": {"type", "detail", "hint", "embedding"}}``), is logged at ERROR
on stderr, and exits with status 1. An invalid option combination is a
``value_error``; a database that cannot be opened or written is an ``io_error``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..retrieval.embeddings.index import CorpusEmbeddingIndex

_logger = logging.getLogger(__name__)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _open_index(args: argparse.Namespace) -> CorpusEmbeddingIndex:
    from ..core.settings import Settings
    from ..retrieval.embeddings.index import CorpusEmbeddingIndex

    chunking = {
        key: value
        for key, value in (
            ("chunk_size", getattr(args, "chunk_size", None)),
            ("chunk_overlap", getattr(args, "chunk_overlap", None)),
            ("max_embed_chars", getattr(args, "max_chars", None)),
        )
        if value is not None
    }
    return CorpusEmbeddingIndex.from_settings(
        Settings(),
        provider=args.provider,
        model=args.model,
        batch_size=getattr(args, "batch", None),
        db_path=args.db,
        **chunking,
    )


def _run(args: argparse.Namespace, action: str) -> int:
    from ..retrieval.embeddings.errors import EmbeddingError

    try:
        index = _open_index(args)
        if action == "build":
            _print_json(index.build(incremental=args.incremental, rebuild=args.rebuild))
        elif action == "search":
            hits = index.search(args.query, top=args.top, library=args.library)
            _print_json([hit.to_dict() for hit in hits])
        else:
            _print_json(index.status())
    except EmbeddingError as exc:
        _logger.error(
            "embeddings %s failed: %s%s", action, exc, f" ({exc.hint})" if exc.hint else ""
        )
        _print_json({"error": exc.to_dict()})
        return 1
    except ValueError as exc:
        _logger.error("embeddings %s failed: %s", action, exc)
        _print_json({"error": {"type": "value_error", "detail": str(exc)}})
        return 1
    except (OSError, sqlite3.Error) as exc:
        # The index database could not be opened, read or written (permissions,
        # a directory where the file should be, a corrupt file, a full disk).
        _logger.error("embeddings %s failed: %s", action, exc)
        _print_json({"error": {"type": "io_error", "detail": str(exc)}})
        return 1
    return 0


def cmd_embeddings_build(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    return _run(args, "build")


def cmd_embeddings_search(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    return _run(args, "search")


def cmd_embeddings_status(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    return _run(args, "status")
