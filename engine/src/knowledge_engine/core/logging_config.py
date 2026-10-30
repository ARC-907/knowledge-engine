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

``KE_LOG_FORMAT=json`` switches every handler wired here (uvicorn's own
included) to :class:`JsonFormatter`: one JSON object per line, ready for a
log shipper. ``KE_LOG_FILE`` adds a size-rotated file handler with the same
formatter next to stderr; ``deploy/observability`` tails that file into Loki.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

LOG_LEVEL_ENV = "KE_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
PACKAGE_LOGGER = "knowledge_engine"
#: Level of one-shot CLI commands when neither -v nor -q is given.
CLI_DEFAULT_LEVEL = "WARNING"
CLI_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FORMAT_ENV = "KE_LOG_FORMAT"
LOG_FILE_ENV = "KE_LOG_FILE"
#: Size at which the ``KE_LOG_FILE`` handler rotates, and how many backups it keeps.
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5

# Attributes every LogRecord carries; anything else was passed via ``extra=``.
_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON object on a single line.

    Keys: ``ts`` (UTC, ISO 8601 with milliseconds), ``level``, ``logger``,
    ``message``, ``process``, ``thread``, ``module``, ``line``; ``exc`` and
    ``stack`` when present; and every ``extra=`` attribute, converted with
    ``str()`` when it is not JSON-serialisable. Never raises: a record that
    cannot be rendered is reported inside the line instead of being lost.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                    timespec="milliseconds"
                ),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "process": record.process,
                "thread": record.threadName,
                "module": record.module,
                "line": record.lineno,
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            if record.stack_info:
                payload["stack"] = self.formatStack(record.stack_info)
            for key, value in vars(record).items():
                if key not in _RECORD_ATTRS and not key.startswith("_") and key not in payload:
                    payload[key] = value
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {
                    "level": "ERROR",
                    "logger": __name__,
                    "message": f"unformattable log record from {record.name!r}: {exc!r}",
                    "msg_template": str(record.msg),
                }
            )


def resolve_log_format(env: Mapping[str, str] | None = None) -> str:
    """Return ``text`` or ``json`` from ``KE_LOG_FORMAT`` (unknown values: ``text``)."""
    if env is None:
        from .settings import Settings  # noqa: PLC0415 - keep this module import-light

        return Settings().log_format
    raw = env.get(LOG_FORMAT_ENV, "").strip().lower()
    if raw in ("", "text", "json"):
        return raw or "text"
    _logger.warning("ignoring invalid %s=%r; using text", LOG_FORMAT_ENV, raw)
    return "text"


def resolve_log_file(env: Mapping[str, str] | None = None) -> str:
    """Return the ``KE_LOG_FILE`` path, or ``""`` when file logging is off."""
    if env is None:
        from .settings import Settings  # noqa: PLC0415 - keep this module import-light

        return Settings().log_file
    return env.get(LOG_FILE_ENV, "").strip()


def _formatter(fmt: str) -> logging.Formatter:
    return JsonFormatter() if fmt == "json" else logging.Formatter(CLI_FORMAT)


def _open_log_file(path: str, fmt: str) -> logging.Handler | None:
    """A rotating file handler for ``path``, or ``None`` (logged) if it cannot be opened."""
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        _logger.warning("cannot open %s=%r (%s); logging to stderr only", LOG_FILE_ENV, path, exc)
        return None
    handler.setFormatter(_formatter(fmt))
    return handler


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


def uvicorn_log_config(
    level: str | None = None, *, log_format: str | None = None, log_file: str | None = None
) -> dict[str, Any]:
    """uvicorn's default logging dict plus a root handler for the engine.

    uvicorn's own ``LOGGING_CONFIG`` only wires the ``uvicorn.*`` loggers, so
    with it alone every ``knowledge_engine.*`` record below WARNING is dropped
    on the floor. This copy adds a stderr handler on the root logger (root
    stays at WARNING so third-party chatter is unchanged) and sets the
    ``knowledge_engine`` logger to ``level`` (default: :func:`resolve_log_level`).
    Pass the result as ``log_config=`` to ``uvicorn.run``.

    ``log_format`` (default ``KE_LOG_FORMAT``) ``json`` switches uvicorn's own
    ``default``/``access`` formatters to :class:`JsonFormatter` as well;
    ``log_file`` (default ``KE_LOG_FILE``) adds a rotating file handler to the
    root and to uvicorn's loggers. An unwritable file is reported and skipped.
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

    fmt = log_format or resolve_log_format()
    if fmt == "json":
        json_formatter = {"()": f"{__name__}.JsonFormatter"}
        for name in list(cfg["formatters"]):
            cfg["formatters"][name] = dict(json_formatter)
    path = resolve_log_file() if log_file is None else log_file
    if path and _log_file_writable(path):
        cfg["handlers"]["log_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": PACKAGE_LOGGER,
            "filename": str(Path(path).expanduser()),
            "maxBytes": LOG_FILE_MAX_BYTES,
            "backupCount": LOG_FILE_BACKUPS,
            "encoding": "utf-8",
        }
        cfg["root"]["handlers"].append("log_file")
        for logger_cfg in cfg["loggers"].values():
            if logger_cfg.get("handlers"):
                logger_cfg["handlers"] = [*logger_cfg["handlers"], "log_file"]
    return cfg


def _log_file_writable(path: str) -> bool:
    """Probe ``path`` once so a bad ``KE_LOG_FILE`` cannot abort ``dictConfig``."""
    handler = _open_log_file(path, "text")
    if handler is None:
        return False
    handler.close()
    return True


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


class _CliFileHandler(logging.Handler):
    """Marks the file handler :func:`configure_cli_logging` installed (so a
    second call replaces it) and delegates to the rotating handler inside."""

    def __init__(self, inner: logging.Handler) -> None:
        super().__init__()
        self.inner = inner

    def emit(self, record: logging.LogRecord) -> None:
        self.inner.handle(record)

    def close(self) -> None:
        self.inner.close()
        super().close()


def configure_cli_logging(
    override: str | None = None,
    *,
    long_running: bool = False,
    log_format: str | None = None,
    log_file: str | None = None,
) -> Callable[[], None]:
    """Install the CLI's single stderr handler and return a function undoing it.

    ``override`` (from ``-v``/``-q``) wins; otherwise long-running commands use
    ``KE_LOG_LEVEL`` and one-shot commands ``WARNING``. The level is applied
    to the root logger, so it covers third-party libraries too. Calling it
    again replaces the previous CLI handler instead of adding another.

    ``log_format`` (default ``KE_LOG_FORMAT``) picks text or JSON lines;
    ``log_file`` (default ``KE_LOG_FILE``) adds a rotating file handler.
    """
    level_name = override or (resolve_log_level() if long_running else CLI_DEFAULT_LEVEL)
    level = logging.getLevelNamesMapping().get(level_name, logging.WARNING)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _StderrHandler | _CliFileHandler):
            root.removeHandler(existing)
            existing.close()
    fmt = log_format or resolve_log_format()
    handler = _StderrHandler(sys.stderr)
    handler.setFormatter(_formatter(fmt))
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(level)
    added: list[logging.Handler] = [handler]
    path = resolve_log_file() if log_file is None else log_file
    if path:
        file_handler = _open_log_file(path, fmt)
        if file_handler is not None:
            wrapped = _CliFileHandler(file_handler)
            root.addHandler(wrapped)
            added.append(wrapped)

    def restore() -> None:
        for item in added:
            root.removeHandler(item)
            item.close()
        root.setLevel(previous_level)

    return restore


__all__ = [
    "CLI_DEFAULT_LEVEL",
    "CLI_FORMAT",
    "DEFAULT_LOG_LEVEL",
    "LOG_FILE_BACKUPS",
    "LOG_FILE_ENV",
    "LOG_FILE_MAX_BYTES",
    "LOG_FORMAT_ENV",
    "LOG_LEVEL_ENV",
    "PACKAGE_LOGGER",
    "JsonFormatter",
    "cli_log_level",
    "configure_cli_logging",
    "resolve_log_file",
    "resolve_log_format",
    "resolve_log_level",
    "uvicorn_log_config",
]
