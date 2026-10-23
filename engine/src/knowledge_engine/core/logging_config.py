"""Process-level logging setup for the CLI and the long-running servers.

Library modules only ever call ``logging.getLogger(__name__)``; nothing in
the package attaches handlers at import time. Handlers are wired in exactly
two places:

* :func:`configure_cli_logging` -- called once by ``knowledge-engine``'s
  ``main()``: one stderr handler on the root logger. One-shot commands log at
  WARNING; long-running ones (``serve``, ``board-serve``, ``mcp``, ``watch``)
  at ``KE_LOG_LEVEL``; ``-v``/``-vv``/``-q``/``-qq`` override both.
* :func:`uvicorn_log_config` -- the uvicorn entry points (``serve`` and
  ``board-serve``) pass it to uvicorn so the engine's own ``INFO`` lines (the
  startup ``CORS: allow_origins=...`` and ``board trust: ...`` policy lines
  operators are told to watch) reach stderr next to uvicorn's.

``KE_LOG_LEVEL`` (default ``INFO``) is the level the long-running commands run
at; :func:`configure_cli_logging` applies it to the root logger, so third-party
libraries follow it too, while :func:`uvicorn_log_config` keeps the root at
``WARNING`` and applies it to the ``knowledge_engine`` hierarchy only. uvicorn
keeps its own ``log_level``.
"""

from __future__ import annotations

import copy
import logging
import sys
from collections.abc import Callable, Mapping
from typing import Any

_logger = logging.getLogger(__name__)

LOG_LEVEL_ENV = "KE_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
PACKAGE_LOGGER = "knowledge_engine"
#: Level of one-shot CLI commands when neither -v nor -q is given.
CLI_DEFAULT_LEVEL = "WARNING"
CLI_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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


def cli_log_level(verbosity: int) -> str:
    """Map ``-v`` (positive) / ``-q`` (negative) counts to a level name.

    ``0`` → ``WARNING``; ``1`` → ``INFO``; ``2+`` → ``DEBUG``; ``-1`` →
    ``ERROR``; ``-2`` or less → ``CRITICAL``.
    """
    if verbosity >= 2:
        return "DEBUG"
    if verbosity == 1:
        return "INFO"
    if verbosity == 0:
        return CLI_DEFAULT_LEVEL
    if verbosity == -1:
        return "ERROR"
    return "CRITICAL"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A stream handler bound to *the current* ``sys.stderr`` at emit time.

    Test harnesses and embedding hosts swap ``sys.stderr``; holding the
    stream object captured at configuration time would write to a stale
    (possibly closed) file.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.setStream(sys.stderr)
        super().emit(record)


def configure_cli_logging(
    override: str | None = None, *, long_running: bool = False
) -> Callable[[], None]:
    """Install the CLI's single stderr handler and return a function undoing it.

    ``override`` (from ``-v``/``-q``) wins; otherwise long-running commands use
    ``KE_LOG_LEVEL`` and one-shot commands ``WARNING``. The level is applied
    to the root logger, so it covers third-party libraries too. Calling it
    again replaces the previous CLI handler instead of adding another.
    """
    level_name = override or (resolve_log_level() if long_running else CLI_DEFAULT_LEVEL)
    level = logging.getLevelNamesMapping().get(level_name, logging.WARNING)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _StderrHandler):
            root.removeHandler(existing)
    handler = _StderrHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(CLI_FORMAT))
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(level)

    def restore() -> None:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    return restore


__all__ = [
    "CLI_DEFAULT_LEVEL",
    "CLI_FORMAT",
    "DEFAULT_LOG_LEVEL",
    "LOG_LEVEL_ENV",
    "PACKAGE_LOGGER",
    "cli_log_level",
    "configure_cli_logging",
    "resolve_log_level",
    "uvicorn_log_config",
]
