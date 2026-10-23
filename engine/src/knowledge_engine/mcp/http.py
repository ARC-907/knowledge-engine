"""Streamable-HTTP plumbing: Host/Origin policy and the FastAPI mount for the MCP server."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "KE_MCP_ALLOWED_HOSTS"
DEFAULT_BIND_HOST = "127.0.0.1"
#: ``Host`` header names that are always accepted (with or without a port).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
#: Bind addresses the server treats as loopback (uvicorn ``--host`` values).
LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1", "[::1]")


def _allowed_hosts_from_env() -> list[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return [host.strip() for host in raw.split(",") if host.strip()]


def _protect(hosts: Iterable[str]) -> TransportSecuritySettings:
    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    for host in hosts:
        allowed_hosts += [host, f"{host}:*"]
        allowed_origins += [
            f"{scheme}://{host}{port}" for scheme in ("http", "https") for port in ("", ":*")
        ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def transport_security(host: str = DEFAULT_BIND_HOST) -> TransportSecuritySettings:
    """DNS-rebinding protection for the streamable-HTTP transport bound to ``host``.

    The policy is decided here, for the standalone ``--http`` server and the
    FastAPI mount alike, so both behave the same for a given bind address:

    * ``KE_MCP_ALLOWED_HOSTS`` unset and a loopback bind (``127.0.0.1``,
      ``localhost``, ``::1``): only ``127.0.0.1``, ``localhost`` or ``[::1]``
      (with any port) are accepted in the ``Host``/``Origin`` headers; anything
      else gets ``421``. This mirrors the SDK default for loopback servers.
    * ``KE_MCP_ALLOWED_HOSTS`` unset and any other bind (``0.0.0.0``, a LAN
      IP): the headers are not checked, as the SDK does for non-loopback
      servers, because a reverse proxy usually sets its own ``Host``. A warning
      is logged so operators know the check is off.
    * ``KE_MCP_ALLOWED_HOSTS=name1,name2``: the listed names are accepted on any
      port in addition to the loopback names, whatever the bind address.
    * ``KE_MCP_ALLOWED_HOSTS=*``: the check is turned off (logged as a warning).
    """
    extra = _allowed_hosts_from_env()
    if "*" in extra:
        logger.warning("%s=* disables MCP DNS-rebinding protection", ALLOWED_HOSTS_ENV)
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if extra:
        logger.info("MCP HTTP also accepts Host headers for: %s", ", ".join(extra))
        return _protect((*LOOPBACK_HOSTS, *extra))
    if host in LOOPBACK_BINDS:
        logger.info(
            "MCP HTTP bound to %s accepts loopback Host headers only; set %s to add names",
            host,
            ALLOWED_HOSTS_ENV,
        )
        return _protect(LOOPBACK_HOSTS)
    logger.warning(
        "MCP HTTP bound to %s accepts any Host header (DNS-rebinding protection off); "
        "set %s to the names clients use, or * to silence this",
        host,
        ALLOWED_HOSTS_ENV,
    )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


class StreamableHTTPMount:
    """Serve an ``MCPServer``'s streamable-HTTP transport as one route of a host app.

    ``host`` is the address the host app is bound to; it selects the
    ``Host``-header policy (see :func:`transport_security`). The SDK session
    manager is single-use, so every :meth:`lifespan` builds a fresh one;
    requests that arrive outside the lifespan get ``503``.

    ``server`` may be ``None`` at construction so a host app can mount the
    route before its engine objects exist; :meth:`attach` supplies the
    server (the engine app does that in its lifespan, per lifespan).
    """

    def __init__(
        self,
        server: MCPServer[Any] | None = None,
        *,
        path: str = "/mcp",
        host: str = DEFAULT_BIND_HOST,
    ) -> None:
        self.server = server
        self.path = path
        self.host = host
        self._running = False

    def route(self) -> Route:
        """A Starlette route (all methods) that forwards to the session manager."""
        return Route(self.path, endpoint=self)

    def attach(self, server: MCPServer[Any]) -> None:
        """Serve ``server`` from the next :meth:`lifespan` on."""
        if self._running:
            raise RuntimeError("cannot replace the MCP server while the transport is running")
        self.server = server

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the session manager; enter this inside the host app's lifespan."""
        if self.server is None:
            raise RuntimeError("no MCP server attached; call attach() first")
        self.server.streamable_http_app(
            streamable_http_path=self.path,
            transport_security=transport_security(self.host),
            host=self.host,
        )
        async with self.server.session_manager.run():
            self._running = True
            logger.info("MCP streamable-HTTP endpoint serving at %s", self.path)
            try:
                yield
            finally:
                self._running = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._running or self.server is None:
            response = PlainTextResponse("MCP transport is not running", status_code=503)
            await response(scope, receive, send)
            return
        await self.server.session_manager.handle_request(scope, receive, send)


__all__ = [
    "ALLOWED_HOSTS_ENV",
    "DEFAULT_BIND_HOST",
    "LOOPBACK_BINDS",
    "LOOPBACK_HOSTS",
    "StreamableHTTPMount",
    "transport_security",
]
