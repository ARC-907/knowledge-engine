"""Remote (off-machine) embedding provider for the project-docs subsystem.

:class:`RemoteProvider` is gated behind ``cfg.embeddings.allow_remote_provider``.
When that gate is False the provider raises immediately, ensuring the
conservative default never silently ships document text to a third party. When
permitted, it POSTs to a configured HTTP endpoint using the standard library
only. No network access occurs at import or construction time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from knowledge_engine.project_docs.embeddings.providers import EmbeddingProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowledge_engine.project_docs.config import ProjectDocsConfig

#: Network timeout (seconds) for a single remote embed request.
_TIMEOUT = 30.0


class RemoteProvider(EmbeddingProvider):
    """Embedding provider that calls a configured remote HTTP endpoint.

    Construction succeeds only when ``cfg.embeddings.allow_remote_provider`` is
    True; otherwise a :class:`RuntimeError` is raised so the capability cannot
    be used by accident.
    """

    def __init__(self, cfg: "ProjectDocsConfig") -> None:
        emb = getattr(cfg, "embeddings", None)
        if emb is None or not getattr(emb, "allow_remote_provider", False):
            raise RuntimeError(
                "RemoteProvider is disabled: set embeddings.allow_remote_provider"
            )
        self._endpoint = str(getattr(emb, "url", "") or "").strip().rstrip("/")
        self._model = str(getattr(emb, "model", "") or "").strip()
        self._dim = int(getattr(emb, "dim", 0) or 0)

    @property
    def name(self) -> str:
        return f"remote:{self._model}" if self._model else "remote"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text via the configured remote endpoint.

        Raises:
            RuntimeError: if no endpoint is configured or the request fails.
        """
        if not self._endpoint:
            raise RuntimeError("RemoteProvider requires a configured endpoint URL")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """POST a single embed request to the remote endpoint."""
        payload = json.dumps({"model": self._model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Remote embedding request failed: {exc}") from exc

        vector = self._parse_vector(body)
        self._dim = len(vector)
        return vector

    @staticmethod
    def _parse_vector(body: str) -> list[float]:
        """Extract a single float vector from a remote embed response body."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Remote endpoint returned non-JSON: {exc}") from exc

        embeddings = data.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            first = embeddings[0]
            if isinstance(first, list):
                return [float(value) for value in first]

        embedding = data.get("embedding")
        if isinstance(embedding, list):
            return [float(value) for value in embedding]

        raise RuntimeError("Remote response contained no embedding vector")
