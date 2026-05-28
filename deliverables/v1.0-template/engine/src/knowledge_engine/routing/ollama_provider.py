"""Local Ollama provider - optional adjunct."""

from __future__ import annotations

import os
import httpx

from . import Provider, GenerationRequest, GenerationResponse


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self._timeout = float(os.getenv("OLLAMA_TIMEOUT", "30"))

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
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
        r = httpx.post(f"{self.base_url}/api/generate", json=payload, timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return GenerationResponse(
            text=data.get("response", ""),
            provider=self.name,
            model=model,
            usage={"eval_count": data.get("eval_count"), "prompt_eval_count": data.get("prompt_eval_count")},
        )

