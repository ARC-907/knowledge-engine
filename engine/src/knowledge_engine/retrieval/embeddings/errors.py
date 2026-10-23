"""Structured errors of the corpus embedding index and vector/hybrid search.

Every error carries a stable machine-readable :attr:`EmbeddingError.code`, the
HTTP status the engine answers with, a human :attr:`EmbeddingError.hint` (what
to run to fix it) and :attr:`EmbeddingError.details` (JSON-safe context such as
the index and query model). The HTTP app turns them into
``{"detail", "type", "hint", "embedding"}`` bodies, the MCP ``search`` tool
into an ``isError`` result with the same fields, and the CLI into a JSON error
on stdout with a non-zero exit status.
"""

from __future__ import annotations

from typing import Any

REBUILD_COMMAND = "knowledge-engine embeddings build --rebuild"
BUILD_COMMAND = "knowledge-engine embeddings build"


class EmbeddingError(RuntimeError):
    """Base class: vector search or index maintenance cannot proceed."""

    code = "embedding_error"
    status_code = 503

    def __init__(
        self, message: str, *, hint: str = "", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """``{"type", "detail", "hint", "embedding"}`` (JSON-safe)."""
        return {
            "type": self.code,
            "detail": self.message,
            "hint": self.hint,
            "embedding": self.details,
        }


class EmbeddingIndexMissingError(EmbeddingError, FileNotFoundError):
    """No usable corpus embedding index exists yet (never built, or empty).

    Also a :class:`FileNotFoundError`, which is what the historical
    ``retrieval.embeddings.search.search`` raised for a missing index.
    """

    code = "embedding_index_missing"
    status_code = 503


class EmbeddingModelMismatchError(EmbeddingError):
    """Vectors of a different model or dimension than the index's were offered.

    Raised when building with another model than the one the index was built
    with, and when a query is embedded with another model. The index is never
    mixed; rebuild it (or switch back to the index's model).
    """

    code = "embedding_model_mismatch"
    status_code = 409


class EmbeddingProviderError(EmbeddingError):
    """The embedding provider failed (unreachable, misconfigured, bad response)."""

    code = "embedding_provider_error"
    status_code = 502


class EmbeddingUnavailableError(EmbeddingError):
    """Vector search is not configured in this process (e.g. remote not allowed)."""

    code = "embedding_unavailable"
    status_code = 503


__all__ = [
    "BUILD_COMMAND",
    "REBUILD_COMMAND",
    "EmbeddingError",
    "EmbeddingIndexMissingError",
    "EmbeddingModelMismatchError",
    "EmbeddingProviderError",
    "EmbeddingUnavailableError",
]
