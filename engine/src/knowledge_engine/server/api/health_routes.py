"""Health and metadata routes."""

from __future__ import annotations

from fastapi import APIRouter

from ... import __version__
from ..deps import ConfigDep, ProvidersDep, RegistryDep
from ..schemas import HealthResponse, InfoResponse, LifecycleState, RegistryCounts

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/info", response_model=InfoResponse, response_model_exclude_unset=True)
def info(config: ConfigDep, registry: RegistryDep, providers: ProvidersDep) -> InfoResponse:
    return InfoResponse(
        version=__version__,
        corpus_root=str(config.corpus_root),
        data_dir=str(config.data_dir),
        counts=RegistryCounts(
            libraries=len(registry.list("library")),
            skills=len(registry.list("skill")),
            kits=len(registry.list("kit")),
            tools=len(registry.list("tool")),
        ),
        providers=providers.list(),
        lifecycle=LifecycleState.model_validate(registry.lifecycle()),
    )
