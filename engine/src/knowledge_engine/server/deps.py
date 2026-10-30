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
from ..knowledge.service import KnowledgeService
from ..retrieval.embeddings.index import CorpusEmbeddingIndex
from ..retrieval.fts5 import Indexer
from ..retrieval.hybrid import HybridSearcher


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


def get_searcher(request: Request) -> HybridSearcher:
    """FTS + (optional) embedding index of this app, for ``GET /search``.

    The embedding index is optional: when the lifespan could not configure it,
    ``app.state.embeddings`` is ``None`` and ``app.state.embeddings_error``
    says why; only a ``vector``/``hybrid`` search then fails.
    """
    indexer: Indexer = _state(request, "indexer")
    registry: Registry = _state(request, "registry")
    embeddings: CorpusEmbeddingIndex | None = getattr(request.app.state, "embeddings", None)
    error: str | None = getattr(request.app.state, "embeddings_error", None)
    return HybridSearcher(indexer, registry, embeddings, embeddings_error=error)


def get_knowledge(request: Request) -> KnowledgeService:
    """The scoped-knowledge admin service (``/knowledge``); 503 when the store is not open."""
    service: KnowledgeService | None = getattr(request.app.state, "knowledge", None)
    if service is None:
        raise HTTPException(503, "knowledge store unavailable")
    return service


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConfigDep = Annotated[Config, Depends(get_config)]
RegistryDep = Annotated[Registry, Depends(get_registry)]
IndexerDep = Annotated[Indexer, Depends(get_indexer)]
ProvidersDep = Annotated[ProviderRegistry, Depends(get_providers)]
SearcherDep = Annotated[HybridSearcher, Depends(get_searcher)]
KnowledgeDep = Annotated[KnowledgeService, Depends(get_knowledge)]

__all__ = [
    "ConfigDep",
    "IndexerDep",
    "KnowledgeDep",
    "ProvidersDep",
    "RegistryDep",
    "SearcherDep",
    "SettingsDep",
    "get_config",
    "get_indexer",
    "get_knowledge",
    "get_providers",
    "get_registry",
    "get_searcher",
    "get_settings",
]
