"""Optional Ollama provider. Requires `ollama` package and a reachable server.

Not imported by default. To enable:

    pip install knowledge-engine[local]

Then in your own bootstrapping code:

    from routing_local.ollama_provider import OllamaProvider
    app.state.providers.register(OllamaProvider())
"""

from __future__ import annotations

import os

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from knowledge_engine.routing import GenerationRequest, GenerationResponse, Provider


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self) -> None:
        self.url = os.environ.get("KE_LOCAL_OLLAMA_URL", "http://127.0.0.1:11434")
        self.model = os.environ.get("KE_LOCAL_OLLAMA_MODEL", "llama3.2:3b")

    def available(self) -> bool:
        if httpx is None:
            return False
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body = {
            "model": request.model or self.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        r = httpx.post(f"{self.url}/api/generate", json=body, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        return GenerationResponse(
            text=data.get("response", ""),
            provider=self.name,
            model=body["model"],
            usage={"eval_count": data.get("eval_count")},
        )
