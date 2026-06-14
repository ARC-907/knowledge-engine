"""Cloud provider stubs. Bring your own key; no defaults shipped.

Configure via env vars (e.g. KE_OPENAI_API_KEY, KE_ANTHROPIC_API_KEY). Implementations
are intentionally minimal — replace with the SDK of your choice.
"""

from __future__ import annotations

import os

from . import GenerationRequest, GenerationResponse, Provider


class EchoProvider(Provider):
    """Default fallback. Echoes the prompt back. Always available. Useful for tests."""

    name = "echo"

    def available(self) -> bool:
        return True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return GenerationResponse(
            text=request.prompt,
            provider=self.name,
            model=request.model or "echo-1",
        )


class CloudHTTPProvider(Provider):
    """Generic HTTP provider. Configure with env vars. Disabled until configured."""

    name = "cloud-http"

    def __init__(self) -> None:
        self.endpoint = os.environ.get("KE_CLOUD_ENDPOINT", "")
        self.api_key = os.environ.get("KE_CLOUD_API_KEY", "")
        self.model = os.environ.get("KE_CLOUD_MODEL", "")

    def available(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        # Stub: real implementation would POST to self.endpoint.
        # Kept dependency-free; uncomment httpx import when wiring.
        return GenerationResponse(
            text="[cloud-http stub] " + request.prompt[:200],
            provider=self.name,
            model=request.model or self.model,
        )
