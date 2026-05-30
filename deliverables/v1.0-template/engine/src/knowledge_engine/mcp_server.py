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
from .project_docs.config import load_config as load_pd_config
from .project_docs.mcp_tools import collect_tools as collect_pd_tools
from .project_docs.mcp_tools.base import ToolContext
from .project_docs.paths import resolve_project_root

# Agent board MCP tool group — discovered via the parallel auto-collect pattern.
try:
    from .agent_board.mcp_tools import collect_tools as collect_board_tools
    from .agent_board.mcp_tools.base import BoardContext
except ImportError:  # noqa: PERF203 — first-boot defensive
    collect_board_tools = None  # type: ignore[assignment]
    BoardContext = None  # type: ignore[assignment]


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
        # ── Project-docs subsystem (optional, off unless configured) ──
        self.pd_tools: list[dict[str, Any]] = []
        self.pd_dispatch: dict[str, Any] = {}
        self.pd_ctx: ToolContext | None = None
        try:
            pd_cfg = load_pd_config()
            if pd_cfg.mcp.enabled:
                self.pd_ctx = ToolContext(cfg=pd_cfg, root=resolve_project_root())
                self.pd_tools, self.pd_dispatch = collect_pd_tools(pd_cfg)
        except Exception:  # noqa: BLE001 — project-docs must never break the base server
            self.pd_tools, self.pd_dispatch, self.pd_ctx = [], {}, None

        # ── Agent board subsystem (optional, off unless module imports) ──
        self.board_tools: list[dict[str, Any]] = []
        self.board_dispatch: dict[str, Any] = {}
        self.board_ctx: "BoardContext | None" = None
        if collect_board_tools is not None:
            try:
                self.board_tools, self.board_dispatch = collect_board_tools()
                if BoardContext is not None:
                    self.board_ctx = BoardContext.from_config()
            except Exception:  # noqa: BLE001 — board must never break the base server
                self.board_tools, self.board_dispatch, self.board_ctx = [], {}, None

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
        if name in self.pd_dispatch:
            return self.pd_dispatch[name](name, args, self.pd_ctx)
        if name in self.board_dispatch:
            return self.board_dispatch[name](name, args, self.board_ctx)
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
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": TOOLS + self.pd_tools + self.board_tools},
            }
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
