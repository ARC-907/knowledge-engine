"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config
from .registry import Registry
from .indexer import Indexer
from .routing import ProviderRegistry
from .routing.cloud import CloudHTTPProvider, EchoProvider
from .routing.ollama_provider import OllamaProvider
from .api import registry_routes, search_routes, health_routes, generate_routes

_logger = logging.getLogger(__name__)


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


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

    # ── Agent Board (opt-out via KE_BOARD_ENABLED=0) ─────────────
    if _env_truthy("KE_BOARD_ENABLED", default=True):
        try:
            from .api import board_routes
            from .agent_board import store as board_store
            from .agent_board import sweeper as board_sweeper

            app.include_router(board_routes.router, prefix="/board", tags=["board"])

            # Read singleton config to decide whether to start the sweeper.
            try:
                cfg = board_store.load_config()
            except Exception:  # noqa: BLE001 — defensive on first boot
                cfg = {"sweeper_enabled": True}

            sweeper_env = os.environ.get("KE_BOARD_SWEEPER")
            sweeper_on = bool(cfg.get("sweeper_enabled", True))
            if sweeper_env is not None:
                sweeper_on = sweeper_env.strip().lower() not in ("0", "false", "no", "off", "")
            if sweeper_on:
                board_sweeper.start()

            # Clean shutdown on uvicorn reload / SIGTERM so the sweeper
            # daemon thread exits and releases its `kv_store` lease for
            # peer sweepers to pick up immediately.
            @app.on_event("shutdown")
            def _board_shutdown() -> None:
                try:
                    board_sweeper.stop()
                except Exception:  # noqa: BLE001
                    _logger.exception("board sweeper failed to stop cleanly")
        except Exception:  # noqa: BLE001 — board must never break core boot
            _logger.exception("agent board failed to mount; continuing without it")

    # Static dashboard (served at /ui)
    ui_dir = Path(__file__).resolve().parents[2].parent / "ui"
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app
