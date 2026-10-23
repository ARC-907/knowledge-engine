"""FastAPI application factory."""

from __future__ import annotations

import atexit
import contextlib
import logging
from importlib import resources
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.config import Config
from ..core.http import BodySizeLimitMiddleware, cors_origins, install_cors
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

# Browser origins allowed to call the API cross-origin. Defaults to loopback;
# the dashboard at /ui is same-origin and needs no entry. ``*`` only when
# written explicitly.
CORS_ORIGINS_ENV = "KE_CORS_ORIGINS"


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
            # Streamed-body cap for /board: ``Content-Length`` is checked in the
            # routes too, but only this middleware stops a chunked upload.
            app.add_middleware(
                BodySizeLimitMiddleware,
                max_bytes=board_routes.MAX_REQUEST_BODY_BYTES,
                path_prefix="/board",
            )
        except Exception:
            _logger.exception("agent board failed to mount; continuing without it")

    # ── MCP over streamable HTTP at /mcp (opt-in via KE_MCP_HTTP=1) ──
    # Shares this app's registry/indexer; the session manager runs inside
    # `lifespan` (see lifespan.py).
    if env_truthy("KE_MCP_HTTP", default=False):
        try:
            # Optional surface, imported lazily: the MCP SDK is only loaded when enabled.
            from ..mcp.http import StreamableHTTPMount  # noqa: PLC0415
            from ..mcp.server import build_server  # noqa: PLC0415

            mcp_http = StreamableHTTPMount(
                build_server(app.state.config, app.state.registry, app.state.indexer)
            )
            app.router.routes.append(mcp_http.route())
            app.state.mcp_http = mcp_http
        except Exception:
            _logger.exception("MCP streamable-HTTP endpoint failed to mount; continuing without it")

    # Registered last so CORSMiddleware is the outermost layer (``add_middleware``
    # is LIFO): a 413 written by the body cap then still carries the CORS grant
    # for an allowed browser origin instead of surfacing as an opaque error.
    install_cors(app, cors_origins(CORS_ORIGINS_ENV), env_var=CORS_ORIGINS_ENV)

    # Static dashboard (served at /ui)
    _mount_dashboard(app)

    return app
