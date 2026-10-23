"""FastAPI dependency providers for the engine routes.

The engine objects are built by the app lifespan (``server.lifespan``) and
stored on ``app.state``; routes receive them through these providers instead
of reaching into ``request.app.state`` themselves. A request that arrives
when the lifespan has not run (or has shut down) gets ``503``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from ..core.config import Config
from ..core.registry import Registry
from ..core.settings import Settings
from ..generation import ProviderRegistry
from ..retrieval.fts5 import Indexer


def _state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(503, f"engine not started ({name} unavailable)")
    return value


def get_settings(request: Request) -> Settings:
    settings: Settings = _state(request, "settings")
    return settings


def get_config(request: Request) -> Config:
    config: Config = _state(request, "config")
    return config


def get_registry(request: Request) -> Registry:
    registry: Registry = _state(request, "registry")
    return registry


def get_indexer(request: Request) -> Indexer:
    indexer: Indexer = _state(request, "indexer")
    return indexer


def get_providers(request: Request) -> ProviderRegistry:
    providers: ProviderRegistry = _state(request, "providers")
    return providers


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConfigDep = Annotated[Config, Depends(get_config)]
RegistryDep = Annotated[Registry, Depends(get_registry)]
IndexerDep = Annotated[Indexer, Depends(get_indexer)]
ProvidersDep = Annotated[ProviderRegistry, Depends(get_providers)]

__all__ = [
    "ConfigDep",
    "IndexerDep",
    "ProvidersDep",
    "RegistryDep",
    "SettingsDep",
    "get_config",
    "get_indexer",
    "get_providers",
    "get_registry",
    "get_settings",
]
