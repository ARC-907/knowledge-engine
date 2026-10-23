"""Local Ollama provider - optional adjunct.

HTTP goes through :class:`knowledge_engine.core.ollama.OllamaClient`.
Environment variables read (through :class:`~knowledge_engine.core.settings.Settings`):
``OLLAMA_BASE_URL`` (default ``http://127.0.0.1:11434``), ``OLLAMA_MODEL``
(default ``llama3.2``) and ``OLLAMA_TIMEOUT`` (seconds, default 30).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.ollama import DEFAULT_BASE_URL, PROBE_TIMEOUT, OllamaClient, OllamaError
from .base import GenerationRequest, GenerationResponse, Provider, ProviderError

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

__all__ = ["PROBE_TIMEOUT", "OllamaProvider"]


class OllamaProvider(Provider):
    """Text generation through :class:`knowledge_engine.core.ollama.OllamaClient`."""

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
            base_url or (settings.ollama_base_url if settings else DEFAULT_BASE_URL)
        ).rstrip("/")
        self.model = model or (settings.ollama_model if settings else "llama3.2")
        self._timeout = float(
            timeout if timeout is not None else settings.ollama_timeout if settings else 30.0
        )
        self.client = OllamaClient(self.base_url, self._timeout, probe_timeout=PROBE_TIMEOUT)

    def available(self) -> bool:
        return self.client.available()

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = request.model or self.model
        try:
            data = self.client.generate(
                model,
                request.prompt,
                options={"temperature": request.temperature, "num_predict": request.max_tokens},
            )
        except OllamaError as exc:
            raise ProviderError(self.name, str(exc), status_code=exc.status_code) from exc
        return GenerationResponse(
            text=str(data.get("response", "")),
            provider=self.name,
            model=model,
            usage={
                "eval_count": data.get("eval_count"),
                "prompt_eval_count": data.get("prompt_eval_count"),
            },
        )
