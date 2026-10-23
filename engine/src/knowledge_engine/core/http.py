"""HTTP hardening shared by the engine app and the standalone board app.

* :func:`cors_origins` parses a CORS allow-list from an environment variable
  with a loopback-only default, so both FastAPI apps apply one policy.
* :func:`install_cors` mounts Starlette's ``CORSMiddleware`` with an explicit
  method/header list and logs the effective policy once.
* :class:`BodySizeLimitMiddleware` caps a streamed request body. The
  ``Content-Length`` header is a fast path only; chunked uploads are counted
  as they arrive and aborted with ``413`` the moment they exceed the cap.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

_logger = logging.getLogger(__name__)

# Browser origins a local operator plausibly loads the dashboard or a dev
# UI from. Ports 9210 (engine) and 11437 (standalone board) are the defaults.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1",
    "http://localhost",
    "http://127.0.0.1:9210",
    "http://localhost:9210",
    "http://127.0.0.1:11437",
    "http://localhost:11437",
)

# Only what the dashboard, CLI and board clients actually send. ``*`` would
# let any page probe the API with arbitrary headers.
CORS_ALLOW_HEADERS: tuple[str, ...] = ("Content-Type", "X-Board-Key", "Accept")
CORS_ALLOW_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def cors_origins(env_var: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Resolve the CORS allow-list from ``env_var``.

    Unset or blank → the loopback defaults. ``*`` → fully permissive, but
    only when written out explicitly. Anything else is a comma-separated
    list of origins, trimmed, blanks dropped.
    """
    source = os.environ if env is None else env
    raw = source.get(env_var, "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def install_cors(app: FastAPI, origins: list[str], *, env_var: str) -> list[str]:
    """Mount ``CORSMiddleware`` with the project's explicit policy and log it."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=list(CORS_ALLOW_METHODS),
        allow_headers=list(CORS_ALLOW_HEADERS),
    )
    if origins == ["*"]:
        _logger.warning("CORS: allow_origins=* (set explicitly via %s)", env_var)
    else:
        _logger.info(
            "CORS: allow_origins=%s allow_methods=%s allow_headers=%s (override via %s)",
            origins,
            list(CORS_ALLOW_METHODS),
            list(CORS_ALLOW_HEADERS),
            env_var,
        )
    return origins


# ── Request body cap ───────────────────────────────────────────

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BodyTooLargeError(HTTPException):
    """Raised from the ASGI ``receive`` channel once a body exceeds the cap.

    Subclasses Starlette's ``HTTPException`` so FastAPI's own exception
    handler turns it into a ``413 {"detail": ...}`` response when the body is
    consumed inside a route. :class:`BodySizeLimitMiddleware` also catches it
    as a fallback for bodies read outside the exception-handling layer.
    """

    def __init__(self, max_bytes: int, received: int | None = None) -> None:
        seen = f"; got {received}" if received is not None else ""
        super().__init__(413, f"request body too large (max {max_bytes} bytes{seen})")


class BodySizeLimitMiddleware:
    """Pure-ASGI middleware that rejects request bodies over ``max_bytes``.

    Applies to HTTP requests whose path starts with ``path_prefix`` (every
    request when the prefix is empty). A declared ``Content-Length`` above
    the cap is rejected before the body is read; otherwise the body is
    counted chunk by chunk and aborted as soon as the cap is crossed.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, path_prefix: str = "") -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.path_prefix = path_prefix

    def _applies(self, scope: Scope) -> bool:
        if scope.get("type") != "http":
            return False
        path = str(scope.get("path", ""))
        return path.startswith(self.path_prefix) if self.path_prefix else True

    def _declared_length(self, scope: Scope) -> int | None:
        for name, value in scope.get("headers", ()):
            if bytes(name).lower() == b"content-length":
                try:
                    return int(bytes(value).decode("latin-1").strip())
                except ValueError:
                    return None
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            _logger.warning(
                "rejecting %s %s: declared body %d bytes exceeds cap %d",
                scope.get("method"),
                scope.get("path"),
                declared,
                self.max_bytes,
            )
            await self._reject(send, BodyTooLargeError(self.max_bytes, declared))
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    _logger.warning(
                        "aborting %s %s: streamed body exceeded cap %d",
                        scope.get("method"),
                        scope.get("path"),
                        self.max_bytes,
                    )
                    raise BodyTooLargeError(self.max_bytes)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLargeError as exc:
            if response_started:
                raise
            await self._reject(send, exc)

    @staticmethod
    async def _reject(send: Send, exc: BodyTooLargeError) -> None:
        body = json.dumps({"detail": exc.detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": exc.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = [
    "CORS_ALLOW_HEADERS",
    "CORS_ALLOW_METHODS",
    "DEFAULT_CORS_ORIGINS",
    "BodySizeLimitMiddleware",
    "BodyTooLargeError",
    "cors_origins",
    "install_cors",
]
