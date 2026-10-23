"""FastAPI application factory."""

from __future__ import annotations

import atexit
import contextlib
import logging
from importlib import resources
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

# Dashboard assets ship inside the package (``knowledge_engine/server/static``)
# so they are present in wheels and editable installs alike.
DASHBOARD_PACKAGE = "knowledge_engine.server"
DASHBOARD_DIRNAME = "static"


def dashboard_dir() -> Path | None:
    """Locate the packaged dashboard directory, or None when it is missing.

    Regular installs expose package data as real files. For non-filesystem
    installs (e.g. zip imports) the directory is extracted once with
    ``importlib.resources.as_file`` and cleaned up at interpreter exit.
    """
    resource = resources.files(DASHBOARD_PACKAGE).joinpath(DASHBOARD_DIRNAME)
    if not resource.is_dir():
        _logger.warning("dashboard assets not found at %s; /ui will not be served", resource)
        return None
    if isinstance(resource, Path):
        return resource
    stack = contextlib.ExitStack()
    extracted = stack.enter_context(resources.as_file(resource))
    atexit.register(stack.close)
    return extracted


def _mount_dashboard(app: FastAPI) -> None:
    ui_dir = dashboard_dir()
    if ui_dir is None:
        return
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")
    _logger.info("dashboard mounted at /ui from %s", ui_dir)


def create_app() -> FastAPI:
    config = Config.from_env()
    # The registry mirror and FTS index below open SQLite files in data_dir.
    config.ensure_dirs()
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
    _mount_dashboard(app)

    return app
