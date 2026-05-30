"""FTS5 search MCP tools for the board."""

from __future__ import annotations

from typing import Any

from .base import BoardContext, error_result, kb_store, text_result

GROUP = "board.search"


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "board_search",
            "description": (
                "Full-text search over message subject + body with bm25 ranking "
                "and snippet highlighting. Falls back to LIKE scan if FTS5 is "
                "unavailable in the SQLite build."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "FTS5 query expression."},
                    "channel": {"type": "string"},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
            },
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: BoardContext | None) -> dict[str, Any]:
    if name == "board_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return error_result("query is required")
        results = kb_store.search_messages(
            query=query,
            channel=args.get("channel"),
            limit=int(args.get("limit", 25)),
        )
        return text_result(results)
    return error_result(f"unknown tool: {name}")
