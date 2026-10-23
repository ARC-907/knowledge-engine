"""Generation routes (provider-routed)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...generation import GenerationRequest, ProviderError
from ..deps import ProvidersDep
from ..schemas import ErrorResponse, GenerateResponse, GenIn

router = APIRouter()

__all__ = ["GenIn", "router"]


@router.post(
    "",
    response_model=GenerateResponse,
    responses={502: {"model": ErrorResponse, "description": "The selected provider failed."}},
)
def generate(payload: GenIn, providers: ProvidersDep) -> GenerateResponse:
    provider = providers.select(preferred=payload.provider)
    if provider is None:
        raise HTTPException(status_code=503, detail="no provider available")
    req = GenerationRequest(
        prompt=payload.prompt,
        model=payload.model,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
    )
    try:
        res = provider.generate(req)
    except ProviderError:
        # Re-probe before the next request instead of trusting a stale "up".
        providers.invalidate(provider.name)
        raise
    return GenerateResponse(text=res.text, provider=res.provider, model=res.model, usage=res.usage)


@router.get("/providers", response_model=list[str])
def list_providers(providers: ProvidersDep) -> list[str]:
    return providers.list()
