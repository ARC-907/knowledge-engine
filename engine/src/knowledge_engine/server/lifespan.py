"""Application lifespan + board-boot helpers, factored out of the app factory.

Owns the FastAPI lifespan context (board sweeper start/stop, the MCP
streamable-HTTP session manager when ``KE_MCP_HTTP`` is on) and the env-var
gating used by both the lifespan and the factory's optional mounts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import ModuleType

from fastapi import FastAPI

_logger = logging.getLogger(__name__)

#: The address the host app is bound to. ``knowledge-engine serve`` sets it
#: from ``--host``; set it yourself when running uvicorn directly. The app
#: factory takes no arguments, so this is how optional mounts (the MCP endpoint)
#: learn the bind address for their Host-header policy.
BIND_HOST_ENV = "KE_BIND_HOST"
DEFAULT_BIND_HOST = "127.0.0.1"


def env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def bind_host() -> str:
    """The address the app is bound to (``KE_BIND_HOST``, default loopback)."""
    return os.environ.get(BIND_HOST_ENV, "").strip() or DEFAULT_BIND_HOST


def _board_sweeper_should_run(board_sweeper: ModuleType) -> bool:
    """Read the runtime decision for starting the sweeper at app boot.

    `KE_BOARD_SWEEPER` env var, when set, overrides the persisted
    `board_config.sweeper_enabled` flag — useful for ops who want to
    skip the sweeper in a specific deployment without touching the DB
    (and the test suite, which turns it off by default). Unset, the
    persisted flag decides; it defaults to on.
    """
    override: bool | None = board_sweeper.env_override()
    if override is not None:
        if not override:
            _logger.info("board sweeper disabled by %s", board_sweeper.SWEEPER_ENV)
        return override
    try:
        # Optional subsystem, imported lazily so a broken board never blocks boot.
        from ..agent_board import store as board_store  # noqa: PLC0415

        cfg = board_store.load_config()
    except Exception:
        cfg = {"sweeper_enabled": True}
    return bool(cfg.get("sweeper_enabled", True))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan — board sweeper start/stop and the MCP HTTP session manager.

    Replaces the deprecated `@app.on_event("startup"/"shutdown")`
    hooks (FastAPI ≥ 0.93 prefers the lifespan context). Wrapped in
    try/except so a board or MCP failure can never block the lean core's
    boot or shutdown.
    """
    board_sweeper: ModuleType | None = None
    if env_truthy("KE_BOARD_ENABLED", default=True):
        try:
            from .api import board_routes  # noqa: PLC0415

            board_routes.log_trust_policy()
        except Exception:
            _logger.exception("could not log board trust policy; continuing")
        try:
            # Optional subsystem, imported lazily so a broken board never blocks boot.
            from ..agent_board import sweeper as _sweeper  # noqa: PLC0415

            board_sweeper = _sweeper
            if _board_sweeper_should_run(_sweeper):
                _sweeper.start()
        except Exception:
            _logger.exception("board sweeper failed to start; continuing")
            board_sweeper = None
    async with AsyncExitStack() as optional_services:
        mcp_http = getattr(app.state, "mcp_http", None)
        if mcp_http is not None:
            try:
                await optional_services.enter_async_context(mcp_http.lifespan())
            except Exception:
                _logger.exception("MCP session manager failed to start; /mcp will answer 503")
        try:
            yield
        finally:
            if board_sweeper is not None:
                try:
                    board_sweeper.stop()
                except Exception:
                    _logger.exception("board sweeper failed to stop cleanly")
