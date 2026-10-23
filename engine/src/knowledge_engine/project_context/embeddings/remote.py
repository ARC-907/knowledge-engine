"""Remote (off-machine) embedding provider.

:class:`RemoteProvider` is gated behind an explicit opt-in: for project-context
``cfg.embeddings.allow_remote_provider``; for the corpus embedding index
``KE_EMBED_ALLOW_REMOTE`` (see :meth:`RemoteProvider.create`). When the gate is
off the provider raises immediately, ensuring the conservative default never
silently ships document text to a third party. When permitted, it POSTs one
request per text to a configured HTTP endpoint with ``httpx``. No network
access occurs at import or construction time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from knowledge_engine.project_context.embeddings.providers import EmbeddingProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowledge_engine.project_context.config import ProjectContextConfig

#: Network timeout (seconds) for a single remote embed request.
_TIMEOUT = 30.0


class RemoteProvider(EmbeddingProvider):
    """Embedding provider that calls a configured remote HTTP endpoint.

    Construction succeeds only when ``cfg.embeddings.allow_remote_provider`` is
    True; otherwise a :class:`RuntimeError` is raised so the capability cannot
    be used by accident.
    """

    def __init__(self, cfg: ProjectContextConfig) -> None:
        emb = getattr(cfg, "embeddings", None)
        if emb is None or not getattr(emb, "allow_remote_provider", False):
            raise RuntimeError("RemoteProvider is disabled: set embeddings.allow_remote_provider")
        self._configure(
            endpoint=str(getattr(emb, "url", "") or ""),
            model=str(getattr(emb, "model", "") or ""),
            dim=int(getattr(emb, "dim", 0) or 0),
        )

    @classmethod
    def create(
        cls, *, endpoint: str, model: str, allow_remote: bool, dim: int = 0
    ) -> RemoteProvider:
        """Build a provider from explicit values instead of a project-context config.

        ``allow_remote`` is the opt-in gate; when it is false this raises
        :class:`RuntimeError` exactly like the config-driven constructor.
        """
        if not allow_remote:
            raise RuntimeError("RemoteProvider is disabled: remote embedding is not allowed")
        provider = cls.__new__(cls)
        provider._configure(endpoint=endpoint, model=model, dim=dim)
        return provider

    def _configure(self, *, endpoint: str, model: str, dim: int) -> None:
        self._endpoint = endpoint.strip().rstrip("/")
        self._model = model.strip()
        self._dim = max(int(dim), 0)

    def __repr__(self) -> str:
        return f"RemoteProvider(model={self._model!r})"

    @property
    def name(self) -> str:
        return f"remote:{self._model}" if self._model else "remote"

    @property
    def model_id(self) -> str:
        return self._model or "remote"

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
        try:
            response = httpx.post(
                self._endpoint,
                json={"model": self._model, "input": text},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise RuntimeError(f"Remote embedding request failed: {exc}") from exc
        body = response.text

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
