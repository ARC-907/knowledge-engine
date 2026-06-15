"""Provider abstraction for LLM generation routing.

Core types live in :mod:`.base`; concrete providers in :mod:`.cloud_http`
(EchoProvider, CloudHTTPProvider) and :mod:`.ollama_provider` (OllamaProvider).
"""

from __future__ import annotations

from .base import GenerationRequest, GenerationResponse, Provider, ProviderRegistry

__all__ = ["GenerationRequest", "GenerationResponse", "Provider", "ProviderRegistry"]
