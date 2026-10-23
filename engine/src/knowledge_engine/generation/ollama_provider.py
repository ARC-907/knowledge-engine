"""Local Ollama provider - optional adjunct.

Environment variables read (through :class:`~knowledge_engine.core.settings.Settings`):
``OLLAMA_BASE_URL`` (default ``http://127.0.0.1:11434``), ``OLLAMA_MODEL``
(default ``llama3.2``) and ``OLLAMA_TIMEOUT`` (seconds, default 30).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from .base import GenerationRequest, GenerationResponse, Provider, ProviderError

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

#: Timeout of the ``/api/tags`` availability probe.
PROBE_TIMEOUT = 2.0


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        if settings is None and None in (base_url, model, timeout):
            from ..core.settings import Settings  # noqa: PLC0415 - optional env fallback

            settings = Settings()
        self.base_url = (
            base_url or (settings.ollama_base_url if settings else "http://127.0.0.1:11434")
        ).rstrip("/")
        self.model = model or (settings.ollama_model if settings else "llama3.2")
        self._timeout = float(
            timeout if timeout is not None else settings.ollama_timeout if settings else 30.0
        )

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=PROBE_TIMEOUT)
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = request.model or self.model
        payload = {
            "model": model,
            "prompt": request.prompt,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        try:
            r = httpx.post(f"{self.base_url}/api/generate", json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"timed out after {self._timeout:g}s") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderError(self.name, f"request failed: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            _logger.warning("ollama: HTTP %d from %s/api/generate", r.status_code, self.base_url)
            raise ProviderError(
                self.name, f"upstream returned HTTP {r.status_code}", status_code=r.status_code
            )
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError(self.name, "upstream returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(self.name, "upstream returned a non-object JSON body")
        return GenerationResponse(
            text=str(data.get("response", "")),
            provider=self.name,
            model=model,
            usage={
                "eval_count": data.get("eval_count"),
                "prompt_eval_count": data.get("prompt_eval_count"),
            },
        )
