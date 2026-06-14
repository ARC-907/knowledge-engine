"""Agent Board — optional standalone service.

For headless deployments where the operator only needs the coordination
surface (no /search, no /registry, no dashboard). Reuses the same
`api/board_routes.py` router on a separate FastAPI app + port.

Default port 11437 was picked to avoid colliding with the engine's 9210.
Override with `KE_BOARD_STANDALONE_PORT` or `--port`.

CORS is restricted by default to the same origins that the peer-trust
gate allows (localhost + Tailscale CGNAT). Extend or override via the
`KE_BOARD_CORS_ORIGINS` env var (comma-separated list, or `*` to allow
any origin — not recommended).

Use `python -m knowledge_engine.agent_board.service` or
`knowledge-engine board-serve --port N` to start it.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from . import sweeper as kb_sweeper

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: "FastAPI") -> AsyncIterator[None]:
    """Lifespan context: start the sweeper on boot, stop it on drain.

    The sweeper coordinates with any embedded sweeper running in the
    main engine via the `board.sweeper_lease` row in `kv_store` so the
    two never both run a pass.
    """
    try:
        kb_sweeper.start()
    except Exception:  # noqa: BLE001
        _logger.exception("standalone sweeper failed to start")
    try:
        yield
    finally:
        try:
            kb_sweeper.stop()
        except Exception:  # noqa: BLE001
            _logger.exception("standalone sweeper failed to stop cleanly")


def _resolve_cors_origins() -> list[str]:
    """Default to loopback origins. `KE_BOARD_CORS_ORIGINS=*` opts into
    permissive CORS for users who need it (and accept the implications);
    a comma-separated list overrides the default explicitly.
    """
    import os
    raw = os.environ.get("KE_BOARD_CORS_ORIGINS", "").strip()
    if not raw:
        return [
            "http://127.0.0.1",
            "http://localhost",
            # Cover the common dev/preview ports without committing to *.
            "http://127.0.0.1:9210",
            "http://localhost:9210",
            "http://127.0.0.1:11437",
            "http://localhost:11437",
        ]
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_standalone_app() -> FastAPI:
    """Build a FastAPI app exposing only the /board/* routes."""
    from ..api import board_routes  # local import — keeps Settings cold-import safe

    app = FastAPI(
        title="Knowledge Engine — Agent Board (standalone)",
        version=__version__,
        lifespan=_lifespan,
    )
    # CORS is locked to loopback by default. Operators on a private mesh
    # who want browser access from a non-loopback origin set
    # KE_BOARD_CORS_ORIGINS explicitly; the peer-trust gate still applies
    # at the route layer.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Board-Key"],
    )
    app.include_router(board_routes.router, prefix="/board", tags=["board"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ke-agent-board-standalone", "version": __version__}

    return app


def serve(host: str = "127.0.0.1", port: int = 11437) -> int:
    """Run the standalone service via uvicorn."""
    try:
        import uvicorn  # type: ignore
    except ImportError:
        print("uvicorn not installed; pip install knowledge-engine", file=sys.stderr)
        return 1
    uvicorn.run(
        "knowledge_engine.agent_board.service:create_standalone_app",
        host=host, port=port, factory=True,
    )
    return 0


def main() -> int:
    """`python -m knowledge_engine.agent_board.service` entry point."""
    import argparse
    p = argparse.ArgumentParser(prog="agent-board-service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11437)
    args = p.parse_args()
    return serve(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
