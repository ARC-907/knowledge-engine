"""Local (Ollama) embedding provider.

:class:`LocalOllamaProvider` embeds through the engine's shared
:class:`knowledge_engine.core.ollama.OllamaClient` (batched ``/api/embed``,
``batch_size`` texts per request). It serves both the project-context store
and the corpus embedding index. No network access happens at import time or
at construction time -- the server is only contacted when
:meth:`LocalOllamaProvider.embed` is called, and failures surface as a clear
:class:`RuntimeError` rather than crashing the import graph.

``OllamaProvider`` is the historical name of the same class and stays
importable.
"""

from __future__ import annotations

import json

import httpx

from knowledge_engine.core.ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_EMBED_BATCH,
    OllamaClient,
    OllamaError,
    parse_embeddings,
)
from knowledge_engine.project_context.embeddings.providers import EmbeddingProvider

#: Default Ollama base URL when none is configured.
_DEFAULT_URL = DEFAULT_BASE_URL
#: Network timeout (seconds) for a single embed request.
_TIMEOUT = 30.0


class LocalOllamaProvider(EmbeddingProvider):
    """Embedding provider backed by a local Ollama ``/api/embed`` endpoint."""

    def __init__(
        self,
        model: str,
        url: str = _DEFAULT_URL,
        *,
        timeout: float = _TIMEOUT,
        batch_size: int = DEFAULT_EMBED_BATCH,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self._model = (model or "").strip()
        self._client = OllamaClient(url or _DEFAULT_URL, timeout, transport=transport)
        self._url = self._client.base_url
        self._batch_size = int(batch_size)
        # Lazily discovered from the first successful response.
        self._dim = 0

    def __repr__(self) -> str:
        return f"LocalOllamaProvider(model={self._model!r}, url={self._url!r})"

    @property
    def name(self) -> str:
        return f"ollama:{self._model}" if self._model else "ollama"

    @property
    def model_id(self) -> str:
        return self._model or "ollama"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def base_url(self) -> str:
        return self._url

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` via the Ollama ``/api/embed`` endpoint (batched).

        Raises:
            RuntimeError: if no model is configured or the server is
                unreachable / returns an unexpected payload.
        """
        if not self._model:
            raise RuntimeError("OllamaProvider requires a model name")
        if not texts:
            return []
        try:
            vectors = self._client.embed(texts, self._model, batch_size=self._batch_size)
        except OllamaError as exc:
            raise RuntimeError(f"Ollama embedding request failed: {exc}") from exc
        self._dim = len(vectors[-1])
        return vectors

    @staticmethod
    def _parse_vector(body: str) -> list[float]:
        """Extract a single float vector from an Ollama embed response body."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama returned non-JSON response: {exc}") from exc
        return parse_embeddings(data, 1)[0]


#: Historical name, kept for callers that import it.
OllamaProvider = LocalOllamaProvider

__all__ = ["LocalOllamaProvider", "OllamaProvider"]
