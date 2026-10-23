"""HTTP hardening shared by the engine app and the standalone board app.

* :func:`cors_origins` parses a CORS allow-list from an environment variable
  with a loopback-only default, so both FastAPI apps apply one policy.
* :func:`install_cors` mounts Starlette's ``CORSMiddleware`` with an explicit
  method/header list and logs the effective policy once.
* :class:`BodySizeLimitMiddleware` caps a streamed request body. The
  ``Content-Length`` header is a fast path only; chunked uploads are counted
  as they arrive and aborted with ``413`` the moment they exceed the cap.
* :func:`trust_proxy_enabled` / :func:`allowed_proxy_networks` read the
  ``X-Forwarded-For`` policy (``KE_TRUST_PROXY``, ``KE_TRUSTED_PROXIES``) that
  the board's trust gate applies, and :func:`uvicorn_proxy_kwargs` turns the
  same policy into uvicorn's ``proxy_headers`` / ``forwarded_allow_ips`` so the
  server never rewrites the peer address behind the app's back.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from .settings import env_flag

if TYPE_CHECKING:
    from .settings import Settings

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
    return parse_cors_origins(source.get(env_var, ""))


def parse_cors_origins(raw: str | None) -> list[str]:
    """Parse a raw CORS allow-list value (see :func:`cors_origins`)."""
    raw = (raw or "").strip()
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


# ── X-Forwarded-For policy ─────────────────────────────────────

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

LOOPBACK_NETS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)

TRUST_PROXY_ENV = "KE_TRUST_PROXY"
TRUSTED_PROXIES_ENV = "KE_TRUSTED_PROXIES"


def parse_cidrs(raw: str | None) -> list[IPNetwork]:
    """Parse a comma-separated list of CIDRs. Bad entries are dropped with a
    warning — the trust gate is a defence in depth on top of key auth, so a
    typo costs nothing more than an over-eager 403."""
    if raw is None or not raw.strip():
        return []
    out: list[IPNetwork] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(ipaddress.ip_network(chunk, strict=False))
        except (ValueError, TypeError):
            _logger.warning("ignoring invalid CIDR %r in trust configuration", chunk)
            continue
    return out


def trust_proxy_enabled(
    env: Mapping[str, str] | None = None, *, settings: Settings | None = None
) -> bool:
    """``KE_TRUST_PROXY`` is truthy (``1``/``true``/``yes``/``on``).

    Reads ``env`` when given, else ``settings`` (default: the current
    environment through :class:`~knowledge_engine.core.settings.Settings`).
    """
    if env is not None:
        return env_flag(env.get(TRUST_PROXY_ENV), default=False)
    return _settings(settings).trust_proxy


def _settings(settings: Settings | None) -> Settings:
    if settings is not None:
        return settings
    from .settings import Settings  # noqa: PLC0415 - avoid a module-level cycle

    return Settings()


def allowed_proxy_networks(
    env: Mapping[str, str] | None = None, *, settings: Settings | None = None
) -> list[IPNetwork]:
    """Addresses allowed to speak for their clients via `X-Forwarded-For`.

    Defaults to loopback: the common case is a reverse proxy on the same
    box. `KE_TRUSTED_PROXIES` (comma-separated CIDRs) REPLACES the default
    for proxies elsewhere on the mesh. Every address listed here is trusted
    to append `X-Forwarded-For` honestly.
    """
    raw = env.get(TRUSTED_PROXIES_ENV) if env is not None else _settings(settings).proxy_allowlist
    if raw is not None:
        return parse_cidrs(raw)
    return list(LOOPBACK_NETS)


def uvicorn_proxy_kwargs(
    env: Mapping[str, str] | None = None, *, settings: Settings | None = None
) -> dict[str, Any]:
    """``uvicorn.run`` keyword arguments matching the app's forwarding policy.

    uvicorn defaults to ``proxy_headers=True`` with loopback as the trusted
    proxy set (or ``$FORWARDED_ALLOW_IPS``), and its ``ProxyHeadersMiddleware``
    rewrites ``scope["client"]`` from ``X-Forwarded-For`` before the app sees
    the request. Left alone that contradicts the documented ``KE_TRUST_PROXY``
    semantics, so both ``serve`` entry points pass these instead: proxy
    headers are honoured only when ``KE_TRUST_PROXY`` is on, and then only
    from ``KE_TRUSTED_PROXIES``. ``FORWARDED_ALLOW_IPS`` is not consulted.
    """
    if env is None:
        settings = _settings(settings)
    enabled = trust_proxy_enabled(env, settings=settings)
    allow = [str(net) for net in allowed_proxy_networks(env, settings=settings)] if enabled else []
    return {"proxy_headers": enabled, "forwarded_allow_ips": allow}


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
    "LOOPBACK_NETS",
    "TRUSTED_PROXIES_ENV",
    "TRUST_PROXY_ENV",
    "BodySizeLimitMiddleware",
    "BodyTooLargeError",
    "IPAddress",
    "IPNetwork",
    "allowed_proxy_networks",
    "cors_origins",
    "install_cors",
    "parse_cidrs",
    "parse_cors_origins",
    "trust_proxy_enabled",
    "uvicorn_proxy_kwargs",
]
