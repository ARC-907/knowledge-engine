"""Generation routes (provider-routed)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..routing import GenerationRequest

router = APIRouter()


class GenIn(BaseModel):
    prompt: str
    provider: str | None = None
    model: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.2


@router.post("")
def generate(payload: GenIn, request: Request) -> dict:
    providers = request.app.state.providers
    provider = providers.select(preferred=payload.provider)
    if provider is None:
        raise HTTPException(status_code=503, detail="no provider available")
    req = GenerationRequest(
        prompt=payload.prompt,
        model=payload.model,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
    )
    res = provider.generate(req)
    return {
        "text": res.text,
        "provider": res.provider,
        "model": res.model,
        "usage": res.usage,
    }


@router.get("/providers")
def list_providers(request: Request) -> list[str]:
    return request.app.state.providers.list()
