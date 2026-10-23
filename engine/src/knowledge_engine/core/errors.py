"""Global HTTP exception handlers shared by the engine app and the standalone board.

Every handler answers with the JSON body ``{"detail": ..., "type": ...}``:

* ``ValueError`` -> ``400`` (``type="value_error"``), the message as detail.
* ``sqlite3.OperationalError`` -> ``503`` (``type="database_unavailable"``),
  logged at ERROR with the traceback; the SQLite message is not echoed.
* Request validation errors raised by a body model with the
  :data:`PAYLOAD_INVALID` error type -> ``400`` (``type="payload_invalid"``)
  with the joined messages as detail, so routes whose rules live in a domain
  validator keep their historical ``400`` answers. Every other validation
  error keeps FastAPI's standard ``422`` response.

The engine app adds its provider handler (``ProviderError`` -> ``502``) on top.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import PydanticCustomError

from .logsafe import log_safe

_logger = logging.getLogger(__name__)

#: Pydantic error type a body validator raises to get a ``400`` instead of ``422``.
PAYLOAD_INVALID = "payload_invalid"


def error_response(status_code: int, detail: Any, error_type: str) -> JSONResponse:
    """The project's JSON error body."""
    return JSONResponse(status_code=status_code, content={"detail": detail, "type": error_type})


def payload_invalid(message: str) -> PydanticCustomError:
    """Build the validator error that :func:`install_exception_handlers` maps to 400."""
    return PydanticCustomError(PAYLOAD_INVALID, "{detail}", {"detail": message})


async def _value_error(request: Request, exc: Exception) -> JSONResponse:
    _logger.warning(
        "400 %s %s: %s", log_safe(request.method), log_safe(request.url.path), log_safe(exc)
    )
    return error_response(400, str(exc), "value_error")


async def _sqlite_operational(request: Request, exc: Exception) -> JSONResponse:
    # Equivalent of ``logger.exception``: the handler runs after the except
    # block has unwound, so the traceback is attached explicitly.
    _logger.error(
        "database error on %s %s",
        log_safe(request.method),
        log_safe(request.url.path),
        exc_info=exc,
    )
    return error_response(503, "database unavailable; retry later", "database_unavailable")


async def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    errors = exc.errors()
    if errors and all(err.get("type") == PAYLOAD_INVALID for err in errors):
        detail = "; ".join(str(err.get("msg", "")) for err in errors)
        return error_response(400, detail, PAYLOAD_INVALID)
    return await request_validation_exception_handler(request, exc)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the shared handlers on ``app``."""
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(ValueError, _value_error)
    app.add_exception_handler(sqlite3.OperationalError, _sqlite_operational)


__all__ = [
    "PAYLOAD_INVALID",
    "error_response",
    "install_exception_handlers",
    "payload_invalid",
]
