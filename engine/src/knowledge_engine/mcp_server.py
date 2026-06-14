"""Compatibility shim. Canonical MCP server lives in knowledge_engine.mcp.server."""
from .mcp.server import main, TOOLS, Server

__all__ = ["main", "TOOLS", "Server"]
