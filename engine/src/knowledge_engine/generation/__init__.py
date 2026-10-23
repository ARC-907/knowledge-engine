"""Provider abstraction for LLM generation routing.

Core types live in :mod:`.base`; concrete providers in :mod:`.cloud_http`
(EchoProvider, CloudHTTPProvider) and :mod:`.ollama_provider` (OllamaProvider);
:mod:`.factory` builds the engine's registry from the typed settings.
"""

from __future__ import annotations

from .base import (
    GenerationRequest,
    GenerationResponse,
    Provider,
    ProviderError,
    ProviderRegistry,
)

__all__ = [
    "GenerationRequest",
    "GenerationResponse",
    "Provider",
    "ProviderError",
    "ProviderRegistry",
]
