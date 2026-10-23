"""Agent Board — optional standalone service.

For headless deployments where the operator only needs the coordination
surface (no /search, no /registry, no dashboard). Reuses the same
`api/board_routes.py` router on a separate FastAPI app + port.

Default port 11437 was picked to avoid colliding with the engine's 9210.
Override with `KE_BOARD_STANDALONE_PORT` or `--port`.

CORS is restricted by default to loopback origins (the same list the
engine app uses). Extend or override via the `KE_BOARD_CORS_ORIGINS` env
var (comma-separated list, or `*` to allow any origin — not recommended).

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
from ..core.http import BodySizeLimitMiddleware, cors_origins, install_cors
from . import sweeper as kb_sweeper

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context: start the sweeper on boot, stop it on drain.

    The sweeper coordinates with any embedded sweeper running in the
    main engine via the `board.sweeper_lease` row in `kv_store` so the
    two never both run a pass. `KE_BOARD_SWEEPER=0` skips it, matching the
    embedded engine's lifespan.
    """
    try:
        from ..server.api import board_routes  # noqa: PLC0415

        board_routes.log_trust_policy()
    except Exception:
        _logger.exception("could not log board trust policy")
    try:
        if kb_sweeper.env_override() is False:
            _logger.info("standalone sweeper disabled by %s", kb_sweeper.SWEEPER_ENV)
        else:
            kb_sweeper.start()
    except Exception:
        _logger.exception("standalone sweeper failed to start")
    try:
        yield
    finally:
        try:
            kb_sweeper.stop()
        except Exception:
            _logger.exception("standalone sweeper failed to stop cleanly")


CORS_ORIGINS_ENV = "KE_BOARD_CORS_ORIGINS"


def _resolve_cors_origins() -> list[str]:
    """Default to loopback origins. `KE_BOARD_CORS_ORIGINS=*` opts into
    permissive CORS for users who need it (and accept the implications);
    a comma-separated list overrides the default explicitly. Shares its
    parser and defaults with the engine app (``core.http.cors_origins``).
    """
    return cors_origins(CORS_ORIGINS_ENV)


def create_standalone_app() -> FastAPI:
    """Build a FastAPI app exposing only the /board/* routes."""
    # Local import: keeps Settings cold-import safe and breaks the
    # agent_board -> server.api.board_routes -> agent_board import cycle.
    from ..server.api import board_routes  # noqa: PLC0415

    app = FastAPI(
        title="Knowledge Engine — Agent Board (standalone)",
        version=__version__,
        lifespan=_lifespan,
    )
    # CORS is locked to loopback by default. Operators on a private mesh
    # who want browser access from a non-loopback origin set
    # KE_BOARD_CORS_ORIGINS explicitly; the peer-trust gate still applies
    # at the route layer.
    install_cors(app, _resolve_cors_origins(), env_var=CORS_ORIGINS_ENV)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=board_routes.MAX_REQUEST_BODY_BYTES,
        path_prefix="/board",
    )
    app.include_router(board_routes.router, prefix="/board", tags=["board"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ke-agent-board-standalone", "version": __version__}

    return app


def serve(host: str = "127.0.0.1", port: int = 11437) -> int:
    """Run the standalone service via uvicorn."""
    try:
        # Only needed when actually serving; keeps `import agent_board` light.
        import uvicorn  # noqa: PLC0415
    except ImportError:
        sys.stderr.write("uvicorn not installed; pip install knowledge-engine\n")
        return 1
    uvicorn.run(
        "knowledge_engine.agent_board.service:create_standalone_app",
        host=host,
        port=port,
        factory=True,
    )
    return 0


def main() -> int:
    """`python -m knowledge_engine.agent_board.service` entry point."""
    p = argparse.ArgumentParser(prog="agent-board-service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11437)
    args = p.parse_args()
    return serve(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
