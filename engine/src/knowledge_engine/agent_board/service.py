"""Agent Board — optional standalone service.

For headless deployments where the operator only needs the coordination
surface (no /search, no /registry, no dashboard). Reuses the same
`agent_board/http.py` router on a separate FastAPI app + port.

Default port 11437 was picked to avoid colliding with the engine's 9210.
Override with `KE_BOARD_STANDALONE_PORT` or `--port`.

CORS is restricted by default to loopback origins (the same list the
engine app uses). Extend or override via the `KE_BOARD_CORS_ORIGINS` env
var (comma-separated list, or `*` to allow any origin — not recommended).

Construction is free of I/O: the sweeper is built and started by the
lifespan, which also logs the effective trust policy.

Use `python -m knowledge_engine.agent_board.service` or
`knowledge-engine board-serve --port N` to start it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .. import __version__
from ..core.errors import install_exception_handlers
from ..core.http import install_cors, parse_cors_origins, uvicorn_proxy_kwargs
from ..core.logging_config import uvicorn_log_config
from ..core.settings import Settings
from ..runtime.foundation import db
from ..runtime.foundation.config import load_dotenv_file
from . import http as board_http, sweeper as kb_sweeper

_logger = logging.getLogger(__name__)

CORS_ORIGINS_ENV = "KE_BOARD_CORS_ORIGINS"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context: start the sweeper on boot, stop it on drain.

    The sweeper coordinates with any embedded sweeper running in the
    main engine via the `board.sweeper_lease` row in `kv_store` so the
    two never both run a pass. `KE_BOARD_SWEEPER=0` skips it, matching the
    embedded engine's lifespan. The sweeper object is owned by this app
    (``app.state.sweeper``) and installed as the process default while the
    app runs.
    """
    settings: Settings = app.state.settings
    try:
        board_http.log_trust_policy(settings)
    except Exception:
        _logger.exception("could not log board trust policy")
    owned = kb_sweeper.Sweeper(settings=settings)
    app.state.sweeper = owned
    previous = kb_sweeper.install_default(owned)
    try:
        if owned.env_override() is False:
            _logger.info("standalone sweeper disabled by %s", kb_sweeper.SWEEPER_ENV)
        else:
            owned.start()
    except Exception:
        _logger.exception("standalone sweeper failed to start")
    try:
        yield
    finally:
        try:
            owned.stop()
        except Exception:
            _logger.exception("standalone sweeper failed to stop cleanly")
        kb_sweeper.install_default(previous)
        try:
            db.close_all()
        except Exception:
            _logger.exception("closing the board DB connections failed")


def _resolve_cors_origins(settings: Settings | None = None) -> list[str]:
    """Default to loopback origins. `KE_BOARD_CORS_ORIGINS=*` opts into
    permissive CORS for users who need it (and accept the implications);
    a comma-separated list overrides the default explicitly. Shares its
    parser and defaults with the engine app (``core.http.parse_cors_origins``).
    """
    settings = settings if settings is not None else Settings()
    return parse_cors_origins(settings.board_cors_origins)


def create_standalone_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app exposing only the /board/* routes.

    Construction does no I/O; the sweeper is created and started by the
    lifespan. ``settings`` defaults to the current environment.
    """
    settings = settings if settings is not None else Settings()
    app = FastAPI(
        title="Knowledge Engine — Agent Board (standalone)",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    install_exception_handlers(app)
    # CORS is locked to loopback by default. Operators on a private mesh
    # who want browser access from a non-loopback origin set
    # KE_BOARD_CORS_ORIGINS explicitly; the peer-trust gate still applies
    # at the route layer.
    board_http.mount_board(app)
    # CORS is registered after the body cap so it ends up outermost
    # (``add_middleware`` is LIFO) and a 413 still carries the CORS grant.
    install_cors(app, _resolve_cors_origins(settings), env_var=CORS_ORIGINS_ENV)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ke-agent-board-standalone", "version": __version__}

    return app


def serve(host: str = "127.0.0.1", port: int | None = None, log_level: str | None = None) -> int:
    """Run the standalone service via uvicorn.

    ``port`` defaults to ``KE_BOARD_STANDALONE_PORT``; ``log_level`` (the
    ``knowledge-engine -v/-q`` override) to ``KE_LOG_LEVEL``.
    """
    try:
        # Only needed when actually serving; keeps `import agent_board` light.
        import uvicorn  # noqa: PLC0415
    except ImportError:
        sys.stderr.write("uvicorn not installed; pip install knowledge-engine\n")
        return 1
    # Same wiring as `knowledge-engine serve`: a root handler so the board's
    # INFO policy lines reach stderr, and uvicorn's proxy-header handling
    # bound to KE_TRUST_PROXY / KE_TRUSTED_PROXIES instead of its own default.
    settings = Settings()
    uvicorn.run(
        "knowledge_engine.agent_board.service:create_standalone_app",
        host=host,
        port=port if port is not None else settings.board_standalone_port,
        factory=True,
        log_config=uvicorn_log_config(log_level or settings.log_level),
        **uvicorn_proxy_kwargs(settings=settings),
    )
    return 0


def main() -> int:
    """`python -m knowledge_engine.agent_board.service` entry point."""
    p = argparse.ArgumentParser(prog="agent-board-service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port", type=int, default=None, help="Port (default: KE_BOARD_STANDALONE_PORT or 11437)."
    )
    args = p.parse_args()
    load_dotenv_file()
    return serve(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
