"""Minimal MCP-compatible stdio server.

Exposes the engine's search + registry toggle as MCP tools over JSON-RPC 2.0
on stdin/stdout (newline-delimited messages). Suitable for Claude Desktop,
Continue, Cursor, and other MCP clients.

Run via: `knowledge-engine mcp` or `python -m knowledge_engine.mcp_server`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import Config
from .registry import Registry
from .indexer import Indexer


PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "knowledge-engine", "version": __version__}


TOOLS = [
    {
        "name": "search",
        "description": "Full-text search across enabled libraries/skills/tools in the corpus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 query string"},
                "limit": {"type": "integer", "default": 10},
                "kind": {"type": "string", "enum": ["library", "skill", "tool"], "description": "Optional kind filter"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "registry_list",
        "description": "List entries in the registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["library", "skill", "tool"]},
                "enabled_only": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "registry_toggle",
        "description": "Enable or disable a registry entry by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["entry_id", "enabled"],
        },
    },
    {
        "name": "registry_get",
        "description": "Fetch a single registry entry by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"entry_id": {"type": "string"}},
            "required": ["entry_id"],
        },
    },
]


class Server:
    def __init__(self):
        self.config = Config.from_env()
        self.registry = Registry(self.config.registry_path, self.config.data_dir / "registry.db")
        self.indexer = Indexer(self.config, self.registry)

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search":
            results = self.indexer.search(args["query"], limit=int(args.get("limit", 10)))
            if args.get("kind"):
                results = [r for r in results if r.get("entry_kind") == args["kind"]]
            return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}
        if name == "registry_list":
            entries = self.registry.list(kind=args.get("kind"), enabled_only=bool(args.get("enabled_only", False)))
            return {"content": [{"type": "text", "text": json.dumps(entries, indent=2)}]}
        if name == "registry_toggle":
            row = self.registry.set_enabled(args["entry_id"], bool(args["enabled"]))
            return {"content": [{"type": "text", "text": json.dumps({"updated": row is not None, "entry": row})}]}
        if name == "registry_get":
            entry = self.registry.get(args["entry_id"])
            return {"content": [{"type": "text", "text": json.dumps(entry, indent=2)}]}
        raise ValueError(f"unknown tool: {name}")

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params", {})
            try:
                result = self.call_tool(params["name"], params.get("arguments", {}))
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}
            except (KeyError, ValueError, TypeError) as e:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(e)}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None


def main() -> int:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
