"""Local (Ollama) embedding provider for the project-context subsystem.

:class:`OllamaProvider` embeds through the engine's shared
:class:`knowledge_engine.core.ollama.OllamaClient` (batched ``/api/embed``).
No network access happens at import time or at construction time -- the
server is only contacted when :meth:`OllamaProvider.embed` is called, and
failures degrade to a clear :class:`RuntimeError` rather than crashing the
import graph.
"""

from __future__ import annotations

import json

from knowledge_engine.core.ollama import (
    DEFAULT_BASE_URL,
    OllamaClient,
    OllamaError,
    parse_embeddings,
)
from knowledge_engine.project_context.embeddings.providers import EmbeddingProvider

#: Default Ollama base URL when none is configured.
_DEFAULT_URL = DEFAULT_BASE_URL
#: Network timeout (seconds) for a single embed request.
_TIMEOUT = 30.0


class OllamaProvider(EmbeddingProvider):
    """Embedding provider backed by a local Ollama ``/api/embed`` endpoint."""

    def __init__(self, model: str, url: str = _DEFAULT_URL) -> None:
        self._model = (model or "").strip()
        self._client = OllamaClient(url or _DEFAULT_URL, _TIMEOUT)
        self._url = self._client.base_url
        # Lazily discovered from the first successful response.
        self._dim = 0

    @property
    def name(self) -> str:
        return f"ollama:{self._model}" if self._model else "ollama"

    @property
    def dim(self) -> int:
        return self._dim

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
            vectors = self._client.embed(texts, self._model)
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
