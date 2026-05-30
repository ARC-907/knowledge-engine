"""Agent Board — optional standalone service.

For headless deployments where the operator only needs the coordination
surface (no /search, no /registry, no dashboard). Reuses the same
`api/board_routes.py` router on a separate FastAPI app + port.

Default port 11437 mirrors the caprock convention so two boards never
collide on the same machine. Override with `KE_BOARD_STANDALONE_PORT` or
`--port`.

Use `python -m knowledge_engine.agent_board.service` or
`knowledge-engine board-serve --port N` to start it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from . import sweeper as kb_sweeper

_logger = logging.getLogger(__name__)


def create_standalone_app() -> FastAPI:
    """Build a FastAPI app exposing only the /board/* routes."""
    from ..api import board_routes  # local import — keeps Settings cold-import safe

    app = FastAPI(
        title="Knowledge Engine — Agent Board (standalone)",
        version=__version__,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(board_routes.router, prefix="/board", tags=["board"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ke-agent-board-standalone", "version": __version__}

    # Start the sweeper alongside the standalone process.
    try:
        kb_sweeper.start()
    except Exception:  # noqa: BLE001
        _logger.exception("standalone sweeper failed to start")

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
