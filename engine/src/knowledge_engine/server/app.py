"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.config import Config
from ..core.registry import Registry
from ..generation import ProviderRegistry
from ..generation.cloud_http import CloudHTTPProvider, EchoProvider
from ..generation.ollama_provider import OllamaProvider
from ..retrieval.fts5 import Indexer
from .api import generate_routes, health_routes, registry_routes, search_routes
from .lifespan import env_truthy, lifespan

_logger = logging.getLogger(__name__)


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

    app = FastAPI(
        title="Knowledge Engine",
        version=__version__,
        lifespan=lifespan,
    )
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

    # ── Agent Board (opt-out via KE_BOARD_ENABLED=0) ─────────────
    # Router mounting happens at app-creation time (cheap, no I/O).
    # Sweeper start/stop is owned by the `lifespan` context (see lifespan.py)
    # so uvicorn reload / SIGTERM cleanly drains the daemon thread.
    if env_truthy("KE_BOARD_ENABLED", default=True):
        try:
            # Optional subsystem: a broken board must never block the core app.
            from .api import board_routes  # noqa: PLC0415

            app.include_router(board_routes.router, prefix="/board", tags=["board"])
        except Exception:
            _logger.exception("agent board failed to mount; continuing without it")

    # Static dashboard (served at /ui)
    ui_dir = Path(__file__).resolve().parents[2].parent / "ui"
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app
