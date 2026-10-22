"""Compatibility shim. Canonical MCP server lives in knowledge_engine.mcp.server."""

from .mcp.server import TOOLS, Server, main

__all__ = ["TOOLS", "Server", "main"]
