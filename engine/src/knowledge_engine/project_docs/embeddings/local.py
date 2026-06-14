"""Local (Ollama) embedding provider for the project-docs subsystem.

:class:`OllamaProvider` talks to an Ollama server's ``/api/embed`` endpoint
using the standard library only (``urllib``), mirroring
``knowledge_engine.embeddings.build.get_embedding``. No network access happens
at import time or at construction time -- the server is only contacted when
:meth:`OllamaProvider.embed` is called, and failures degrade to a clear
:class:`RuntimeError` rather than crashing the import graph.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from knowledge_engine.project_docs.embeddings.providers import EmbeddingProvider

#: Default Ollama base URL when none is configured.
_DEFAULT_URL = "http://localhost:11434"
#: Network timeout (seconds) for a single embed request.
_TIMEOUT = 30.0


class OllamaProvider(EmbeddingProvider):
    """Embedding provider backed by a local Ollama ``/api/embed`` endpoint."""

    def __init__(self, model: str, url: str = _DEFAULT_URL) -> None:
        self._model = (model or "").strip()
        self._url = (url or _DEFAULT_URL).strip().rstrip("/")
        # Lazily discovered from the first successful response.
        self._dim = 0

    @property
    def name(self) -> str:
        return f"ollama:{self._model}" if self._model else "ollama"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text via the Ollama ``/api/embed`` endpoint.

        Raises:
            RuntimeError: if no model is configured or the server is
                unreachable / returns an unexpected payload.
        """
        if not self._model:
            raise RuntimeError("OllamaProvider requires a model name")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """Send a single ``/api/embed`` request and return its float vector."""
        endpoint = f"{self._url}/api/embed"
        payload = json.dumps({"model": self._model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Ollama embedding request failed: {exc}") from exc

        vector = self._parse_vector(body)
        self._dim = len(vector)
        return vector

    @staticmethod
    def _parse_vector(body: str) -> list[float]:
        """Extract a single float vector from an Ollama embed response body."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama returned non-JSON response: {exc}") from exc

        # Current /api/embed shape: {"embeddings": [[...]]}.
        embeddings = data.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            first = embeddings[0]
            if isinstance(first, list):
                return [float(value) for value in first]

        # Legacy /api/embeddings shape: {"embedding": [...]}.
        embedding = data.get("embedding")
        if isinstance(embedding, list):
            return [float(value) for value in embedding]

        raise RuntimeError("Ollama response contained no embedding vector")
