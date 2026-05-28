"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__
from .config import Config
from .registry import Registry
from .indexer import Indexer
from .routing import ProviderRegistry
from .routing.cloud import CloudHTTPProvider, EchoProvider
from .routing.ollama_provider import OllamaProvider
from .api import registry_routes, search_routes, health_routes, generate_routes


def create_app() -> FastAPI:
    config = Config.from_env()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    indexer = Indexer(config, registry)
    providers = ProviderRegistry()
    cloud = CloudHTTPProvider()
    if cloud.available():
        providers.register(cloud)
    ollama = OllamaProvider()
    if ollama.available():
        providers.register(ollama)
    providers.register(EchoProvider())

    app = FastAPI(title="Knowledge Engine", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.config = config
    app.state.registry = registry
    app.state.indexer = indexer
    app.state.providers = providers

    app.include_router(health_routes.router)
    app.include_router(registry_routes.router, prefix="/registry", tags=["registry"])
    app.include_router(search_routes.router, prefix="/search", tags=["search"])
    app.include_router(generate_routes.router, prefix="/generate", tags=["generate"])

    # Static dashboard (served at /ui)
    ui_dir = Path(__file__).resolve().parents[2].parent / "ui"
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app
