"""Process-level logging setup for the long-running servers.

Library modules only ever call ``logging.getLogger(__name__)``; nothing in
the package attaches handlers at import time. The two uvicorn entry points
(``knowledge-engine serve`` and ``knowledge-engine board-serve``) are the
only places a handler is wired, and they do it through :func:`uvicorn_log_config`
so the engine's own ``INFO`` lines (the startup ``CORS: allow_origins=...``
and ``board trust: ...`` policy lines operators are told to watch) reach
stderr next to uvicorn's.

``KE_LOG_LEVEL`` (default ``INFO``) sets the level of the ``knowledge_engine``
logger hierarchy. Other libraries stay at ``WARNING``; uvicorn keeps its own
``log_level``.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)

LOG_LEVEL_ENV = "KE_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
PACKAGE_LOGGER = "knowledge_engine"


def resolve_log_level(env: Mapping[str, str] | None = None) -> str:
    """Return the level name from ``KE_LOG_LEVEL``, falling back to ``INFO``.

    Unknown names are reported at WARNING and replaced by the default rather
    than aborting startup over a typo.
    """
    if env is None:
        # The typed settings layer validates the level (and warns) itself.
        from .settings import Settings  # noqa: PLC0415 - keep this module import-light

        return Settings().log_level
    raw = env.get(LOG_LEVEL_ENV, "").strip().upper()
    if not raw:
        return DEFAULT_LOG_LEVEL
    if raw in logging.getLevelNamesMapping():
        return raw
    _logger.warning("ignoring invalid %s=%r; using %s", LOG_LEVEL_ENV, raw, DEFAULT_LOG_LEVEL)
    return DEFAULT_LOG_LEVEL


def uvicorn_log_config(level: str | None = None) -> dict[str, Any]:
    """uvicorn's default logging dict plus a root handler for the engine.

    uvicorn's own ``LOGGING_CONFIG`` only wires the ``uvicorn.*`` loggers, so
    with it alone every ``knowledge_engine.*`` record below WARNING is dropped
    on the floor. This copy adds a stderr handler on the root logger (root
    stays at WARNING so third-party chatter is unchanged) and sets the
    ``knowledge_engine`` logger to ``level`` (default: :func:`resolve_log_level`).
    Pass the result as ``log_config=`` to ``uvicorn.run``.
    """
    # Local import: uvicorn is a runtime dependency of the servers only.
    from uvicorn.config import LOGGING_CONFIG  # noqa: PLC0415

    resolved = level or resolve_log_level()
    cfg: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("formatters", {})[PACKAGE_LOGGER] = {
        "()": "uvicorn.logging.DefaultFormatter",
        "fmt": "%(levelprefix)s %(name)s: %(message)s",
        "use_colors": None,
    }
    cfg.setdefault("handlers", {})[PACKAGE_LOGGER] = {
        "formatter": PACKAGE_LOGGER,
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stderr",
    }
    cfg["root"] = {"handlers": [PACKAGE_LOGGER], "level": "WARNING"}
    cfg.setdefault("loggers", {})[PACKAGE_LOGGER] = {"level": resolved, "propagate": True}
    return cfg


__all__ = [
    "DEFAULT_LOG_LEVEL",
    "LOG_LEVEL_ENV",
    "PACKAGE_LOGGER",
    "resolve_log_level",
    "uvicorn_log_config",
]
