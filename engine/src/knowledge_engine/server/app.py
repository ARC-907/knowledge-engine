"""FastAPI application factory.

``create_app`` only *describes* the app: routes, middleware, exception
handlers and the optional mounts. It performs no I/O -- no directory is
created, no database opened, no provider probed. Everything stateful is
built by the lifespan (:mod:`knowledge_engine.server.lifespan`) and reached
by the routes through the dependency providers in
:mod:`knowledge_engine.server.deps`.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.errors import error_response, install_exception_handlers
from ..core.http import install_cors, parse_cors_origins
from ..core.logsafe import log_safe
from ..core.settings import Settings
from ..generation import ProviderError
from ..retrieval.embeddings.errors import EmbeddingError
from .api import generate_routes, health_routes, registry_routes, search_routes
from .lifespan import lifespan

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


async def _provider_error(request: Request, exc: Exception) -> JSONResponse:
    _logger.warning(
        "502 %s %s: %s", log_safe(request.method), log_safe(request.url.path), log_safe(exc)
    )
    return error_response(502, str(exc), "provider_error")


async def _embedding_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, EmbeddingError)  # noqa: S101 - registered for this type only
    _logger.warning(
        "%d %s %s: %s",
        exc.status_code,
        log_safe(request.method),
        log_safe(request.url.path),
        log_safe(exc),
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(public=True))


def _mount_board(app: FastAPI) -> None:
    try:
        # Optional subsystem: a broken board must never block the core app.
        from ..agent_board import http as board_http  # noqa: PLC0415

        board_http.mount_board(app)
    except Exception:
        _logger.exception("agent board failed to mount; continuing without it")


def _mount_mcp(app: FastAPI, settings: Settings) -> None:
    try:
        # Optional surface, imported lazily: the MCP SDK is only loaded when enabled.
        from ..mcp.http import StreamableHTTPMount  # noqa: PLC0415

        # The server itself is attached per lifespan, around that lifespan's
        # registry and index; until then the endpoint answers 503.
        mcp_http = StreamableHTTPMount(host=settings.bind_host)
        app.router.routes.append(mcp_http.route())
        app.state.mcp_http = mcp_http
    except Exception:
        _logger.exception("MCP streamable-HTTP endpoint failed to mount; continuing without it")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the engine app. ``settings`` defaults to the current environment.

    No I/O happens here; see :mod:`knowledge_engine.server.lifespan`.
    """
    settings = settings if settings is not None else Settings()
    app = FastAPI(
        title="Knowledge Engine",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    install_exception_handlers(app)
    app.add_exception_handler(ProviderError, _provider_error)
    app.add_exception_handler(EmbeddingError, _embedding_error)

    app.include_router(health_routes.router)
    app.include_router(registry_routes.router, prefix="/registry", tags=["registry"])
    app.include_router(search_routes.router, prefix="/search", tags=["search"])
    app.include_router(generate_routes.router, prefix="/generate", tags=["generate"])

    # ── Agent Board (opt-out via KE_BOARD_ENABLED=0) ─────────────
    # Router + body cap are mounted here (cheap, no I/O); the sweeper is
    # created, started and stopped by the lifespan.
    if settings.board_enabled:
        _mount_board(app)

    # ── MCP over streamable HTTP at /mcp (opt-in via KE_MCP_HTTP=1) ──
    # The bind address (KE_BIND_HOST, set by `knowledge-engine serve --host`)
    # decides the endpoint's Host-header policy.
    if settings.mcp_http:
        _mount_mcp(app, settings)

    # Registered last so CORSMiddleware is the outermost layer (``add_middleware``
    # is LIFO): a 413 written by the body cap then still carries the CORS grant
    # for an allowed browser origin instead of surfacing as an opaque error.
    install_cors(app, parse_cors_origins(settings.cors_origins), env_var=CORS_ORIGINS_ENV)

    # Static dashboard (served at /ui)
    _mount_dashboard(app)

    return app
