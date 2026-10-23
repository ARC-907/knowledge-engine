"""The engine's one HTTP client for an Ollama server.

:class:`OllamaClient` wraps the three endpoints the engine uses --
``GET /api/tags`` (availability probe), ``POST /api/generate`` and
``POST /api/embed`` (batched) -- with bounded timeouts, defensive response
validation and one error type, :class:`OllamaError`. It is used by:

* ``generation.ollama_provider`` (the ``ollama`` text-generation provider),
* ``retrieval.embeddings.build`` / ``.search`` (the corpus embedding index),
* ``project_context.embeddings.local`` (project-context embeddings),
* ``runtime.pipeline.task_classifier`` (``KE_CLASSIFIER_ENDPOINT``, a full
  ``.../api/generate`` URL, and ``KE_CLASSIFIER_MODEL``).

Configuration is unchanged; each consumer keeps reading the variables it has
always read, and an explicit argument always wins over the environment:

==============================  =========================================  =====================
Consumer                        Base URL (first set wins)                  Model / timeout
==============================  =========================================  =====================
generation provider ``ollama``  constructor arg → ``OLLAMA_BASE_URL``      ``OLLAMA_MODEL``,
                                → ``http://127.0.0.1:11434``               ``OLLAMA_TIMEOUT`` (30 s)
corpus embeddings               ``KE_OLLAMA_URL`` → ``http://127.0.0.1:11434``  ``KE_EMBED_MODEL`` (60 s)
project-context embeddings      ``[project_context.embeddings] url``       ``model`` (30 s)
                                → ``http://127.0.0.1:11434``
==============================  =========================================  =====================

No request is made at import or construction time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
#: Timeout of one generate/embed request when none is given (seconds).
DEFAULT_TIMEOUT = 30.0
#: Timeout of the ``/api/tags`` availability probe (seconds).
PROBE_TIMEOUT = 2.0
#: Texts sent per ``/api/embed`` request.
DEFAULT_EMBED_BATCH = 32

#: Everything ``httpx`` raises for a request that never got a usable answer.
#: ``httpx.InvalidURL`` (a malformed base URL such as ``http://host:notaport``)
#: is deliberately not an ``HTTPError`` subclass, so it is listed explicitly.
_REQUEST_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, httpx.InvalidURL, OSError)


class OllamaError(RuntimeError):
    """An Ollama request failed (network, timeout, HTTP status or malformed body).

    ``status_code`` is the upstream HTTP status when there was one;
    ``timed_out`` is true when the request hit its timeout.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.timed_out = timed_out


def normalize_base_url(url: str | None, default: str = DEFAULT_BASE_URL) -> str:
    """``url`` stripped of whitespace and trailing ``/``; blank → ``default``."""
    text = (url or "").strip().rstrip("/")
    return text or default.rstrip("/")


def parse_embeddings(data: Any, expected: int) -> list[list[float]]:
    """Validate an ``/api/embed`` response body and return ``expected`` vectors.

    Accepts the current shape ``{"embeddings": [[...], ...]}`` and, for a
    single text, the legacy ``{"embedding": [...]}``. Raises
    :class:`OllamaError` on any other shape, a count mismatch or a
    non-numeric component.
    """
    if not isinstance(data, Mapping):
        raise OllamaError("Ollama returned a non-object JSON body")
    vectors: Any = data.get("embeddings")
    if not (isinstance(vectors, list) and vectors):
        legacy = data.get("embedding")
        vectors = [legacy] if expected == 1 and isinstance(legacy, list) else None
    if not isinstance(vectors, list) or not vectors:
        raise OllamaError("Ollama response contained no embedding vector")
    if len(vectors) != expected:
        raise OllamaError(f"Ollama returned {len(vectors)} embeddings for {expected} input(s)")
    out: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise OllamaError("Ollama response contained an empty or malformed embedding vector")
        try:
            out.append([float(value) for value in vector])
        except (TypeError, ValueError) as exc:
            raise OllamaError("Ollama embedding vector has a non-numeric component") from exc
    return out


class OllamaClient:
    """Synchronous client for one Ollama server.

    ``transport`` (an ``httpx`` transport, e.g. ``httpx.MockTransport``) is for
    tests and embedding code; without it each call uses the module-level
    ``httpx`` functions.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        probe_timeout: float = PROBE_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout <= 0 or probe_timeout <= 0:
            raise ValueError("timeouts must be positive")
        self.base_url = normalize_base_url(base_url)
        self.timeout = float(timeout)
        self.probe_timeout = float(probe_timeout)
        self._transport = transport

    def __repr__(self) -> str:
        return f"OllamaClient(base_url={self.base_url!r}, timeout={self.timeout:g})"

    # ── transport ────────────────────────────────────────────────

    def _send(
        self, method: str, path: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        limit = self.timeout if timeout is None else timeout
        if self._transport is not None:
            with httpx.Client(transport=self._transport, timeout=limit) as client:
                return client.request(method, url, json=json)
        if method == "GET":
            return httpx.get(url, timeout=limit)
        return httpx.post(url, json=json, timeout=limit)

    def _post_json(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._send("POST", path, json=dict(payload))
        except httpx.TimeoutException as exc:
            raise OllamaError(f"timed out after {self.timeout:g}s", timed_out=True) from exc
        except httpx.InvalidURL as exc:
            raise OllamaError(f"invalid base URL {self.base_url!r}: {exc}") from exc
        except _REQUEST_ERRORS as exc:
            raise OllamaError(f"request failed: {type(exc).__name__}") from exc
        if not 200 <= response.status_code < 300:
            _logger.warning("ollama: HTTP %d from %s%s", response.status_code, self.base_url, path)
            raise OllamaError(
                f"upstream returned HTTP {response.status_code}", status_code=response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaError("upstream returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OllamaError("upstream returned a non-object JSON body")
        return data

    # ── endpoints ────────────────────────────────────────────────

    def available(self) -> bool:
        """``True`` when ``GET /api/tags`` answers 200 within the probe timeout. Never raises."""
        try:
            response = self._send("GET", "/api/tags", timeout=self.probe_timeout)
        except _REQUEST_ERRORS as exc:
            _logger.debug("ollama at %s unavailable: %s", self.base_url, type(exc).__name__)
            return False
        return response.status_code == 200

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/generate`` (non-streaming) and return the response object."""
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if system is not None:
            payload["system"] = system
        if options is not None:
            payload["options"] = dict(options)
        return self._post_json("/api/generate", payload)

    def embed(
        self, texts: Sequence[str], model: str, *, batch_size: int = DEFAULT_EMBED_BATCH
    ) -> list[list[float]]:
        """Embed ``texts`` with ``model`` via batched ``POST /api/embed`` calls.

        Returns one vector per text, in order. Raises :class:`OllamaError`
        when a request fails or a response is malformed.
        """
        if not model or not model.strip():
            raise OllamaError("an embedding model name is required")
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        items = list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            data = self._post_json("/api/embed", {"model": model, "input": batch})
            vectors.extend(parse_embeddings(data, len(batch)))
        return vectors


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_EMBED_BATCH",
    "DEFAULT_TIMEOUT",
    "PROBE_TIMEOUT",
    "OllamaClient",
    "OllamaError",
    "normalize_base_url",
    "parse_embeddings",
]
