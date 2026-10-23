"""Streamable-HTTP plumbing: Host/Origin policy and the FastAPI mount for the MCP server."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _allowed_hosts_from_env() -> list[str]:
    raw = os.environ.get("KE_MCP_ALLOWED_HOSTS", "")
    return [host.strip() for host in raw.split(",") if host.strip()]


def transport_security() -> TransportSecuritySettings | None:
    """DNS-rebinding protection for the streamable-HTTP transport.

    With ``KE_MCP_ALLOWED_HOSTS`` unset this returns ``None`` and the SDK
    default applies: a server bound to a loopback address only accepts
    ``localhost``, ``127.0.0.1`` or ``[::1]`` (with a port) in the
    ``Host``/``Origin`` headers; other bind addresses are not checked.
    Hostnames listed (comma-separated) in ``KE_MCP_ALLOWED_HOSTS`` are accepted
    on any port in addition to the loopback names; ``*`` turns the check off.
    """
    extra = _allowed_hosts_from_env()
    if not extra:
        return None
    if "*" in extra:
        logger.warning("KE_MCP_ALLOWED_HOSTS=* disables MCP DNS-rebinding protection")
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts: list[str] = []
    origins: list[str] = []
    for host in (*LOOPBACK_HOSTS, *extra):
        hosts += [host, f"{host}:*"]
        origins += [
            f"{scheme}://{host}{port}" for scheme in ("http", "https") for port in ("", ":*")
        ]
    logger.info("MCP HTTP also accepts Host headers for: %s", ", ".join(extra))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


class StreamableHTTPMount:
    """Serve an ``MCPServer``'s streamable-HTTP transport as one route of a host app.

    The SDK session manager is single-use, so every :meth:`lifespan` builds a
    fresh one; requests that arrive outside the lifespan get ``503``.
    """

    def __init__(self, server: MCPServer[Any], *, path: str = "/mcp") -> None:
        self.server = server
        self.path = path
        self._running = False

    def route(self) -> Route:
        """A Starlette route (all methods) that forwards to the session manager."""
        return Route(self.path, endpoint=self)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the session manager; enter this inside the host app's lifespan."""
        self.server.streamable_http_app(
            streamable_http_path=self.path, transport_security=transport_security()
        )
        async with self.server.session_manager.run():
            self._running = True
            logger.info("MCP streamable-HTTP endpoint serving at %s", self.path)
            try:
                yield
            finally:
                self._running = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._running:
            response = PlainTextResponse("MCP transport is not running", status_code=503)
            await response(scope, receive, send)
            return
        await self.server.session_manager.handle_request(scope, receive, send)


__all__ = ["LOOPBACK_HOSTS", "StreamableHTTPMount", "transport_security"]
