"""Application lifespan + board-boot helpers, factored out of the app factory.

Owns the FastAPI lifespan context (board sweeper start/stop) and the env-var
gating used by both the lifespan and the factory's board-router mount.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

_logger = logging.getLogger(__name__)


def env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _board_sweeper_should_run() -> bool:
    """Read the runtime decision for starting the sweeper at app boot.

    `KE_BOARD_SWEEPER` env var, when set, overrides the persisted
    `board_config.sweeper_enabled` flag — useful for ops who want to
    skip the sweeper in a specific deployment without touching the DB.
    """
    try:
        from ..agent_board import store as board_store

        cfg = board_store.load_config()
    except Exception:
        cfg = {"sweeper_enabled": True}
    sweeper_env = os.environ.get("KE_BOARD_SWEEPER")
    if sweeper_env is not None:
        return sweeper_env.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(cfg.get("sweeper_enabled", True))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan — board sweeper start/stop lives here.

    Replaces the deprecated `@app.on_event("startup"/"shutdown")`
    hooks (FastAPI ≥ 0.93 prefers the lifespan context). Wrapped in
    try/except so a board failure can never block the lean core's
    boot or shutdown.
    """
    board_sweeper = None
    if env_truthy("KE_BOARD_ENABLED", default=True):
        try:
            from ..agent_board import sweeper as board_sweeper

            if _board_sweeper_should_run():
                board_sweeper.start()
        except Exception:
            _logger.exception("board sweeper failed to start; continuing")
            board_sweeper = None
    try:
        yield
    finally:
        if board_sweeper is not None:
            try:
                board_sweeper.stop()
            except Exception:
                _logger.exception("board sweeper failed to stop cleanly")
