"""Test fixture: a tiny external MCP server over stdio."""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("fixture-echo")


@server.tool()
def shout(text: str) -> dict:
    """Upper-case the text."""
    return {"text": text.upper(), "has_token": bool(os.environ.get("ECHO_TOKEN"))}


if __name__ == "__main__":
    server.run(transport="stdio")
