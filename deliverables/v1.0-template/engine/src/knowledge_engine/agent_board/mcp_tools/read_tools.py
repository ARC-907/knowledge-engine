"""MCP tools for reading the board.

`board_read` is the workhorse — paginated, channel/type/since filterable. The
defaults are deliberately small (`limit=20`) so an LLM caller doesn't flood
its own context.

`board_digest` is the context-saver — collapses a window into counts + recent
subjects rather than full bodies.

`board_relevant` honors the existing visibility-scope rules and is the tool an
agent should call when it just woke up and wants "what's relevant to me?"
"""

from __future__ import annotations

from typing import Any

from .base import BoardContext, error_result, store, text_result

GROUP = "board.read"


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "board_read",
            "description": (
                "Poll messages from the board with optional filters. "
                "Default limit is 20 — set higher only when you need it. "
                "Pair with board_digest for catch-up summaries on busy channels."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO timestamp; only messages newer than this are returned.",
                    },
                    "channel": {"type": "string"},
                    "message_type": {"type": "string"},
                    "task_id": {"type": "string"},
                    "product_id": {"type": "string"},
                    "sender_node_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500},
                },
            },
        },
        {
            "name": "board_relevant",
            "description": (
                "Messages relevant to the calling agent — uses visibility-scope "
                "rules (same task, same product, addressed to role/node, or global). "
                "Use this when waking up to a new branch or worktree."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Caller's identity."},
                    "role": {"type": "string"},
                    "current_task_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Tasks the caller is currently working on.",
                    },
                    "since": {"type": "string"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                },
                "required": ["node_id"],
            },
        },
        {
            "name": "board_thread",
            "description": "Fetch all messages in a thread, oldest-first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "correlation_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                },
            },
        },
        {
            "name": "board_digest",
            "description": (
                "Context-compressed summary of recent activity on a channel "
                "(or all channels). Returns counts by type, top senders, open "
                "blockers, and busy threads — no full bodies. Use this for "
                "catch-up reads to avoid flooding your context window."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "since": {"type": "string"},
                    "max_messages": {
                        "type": "integer", "default": 200, "minimum": 10, "maximum": 2000,
                        "description": "Cap on messages scanned for the summary.",
                    },
                },
            },
        },
        {
            "name": "board_status",
            "description": "Service health, channel stats, last-sweep timestamp.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "board_channels",
            "description": "List configured channels + their canonical defaults.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "board_message_types",
            "description": "List canonical message types + visibility scopes.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: BoardContext | None) -> dict[str, Any]:
    if name == "board_read":
        msgs = store.poll(
            since=args.get("since"),
            channel=args.get("channel"),
            message_type=args.get("message_type"),
            task_id=args.get("task_id"),
            product_id=args.get("product_id"),
            sender_node_id=args.get("sender_node_id"),
            limit=int(args.get("limit", 20)),
        )
        return text_result(msgs)
    if name == "board_relevant":
        node_id = str(args.get("node_id") or "").strip()
        if not node_id:
            return error_result("node_id is required")
        msgs = store.relevant_for(
            node_id=node_id,
            role=args.get("role"),
            current_task_ids=list(args.get("current_task_ids") or []),
            since=args.get("since"),
            limit=int(args.get("limit", 20)),
        )
        return text_result(msgs)
    if name == "board_thread":
        corr = args.get("correlation_id")
        thr = args.get("thread_id")
        if not corr and not thr:
            return error_result("either correlation_id or thread_id is required")
        msgs = store.thread_messages(
            correlation_id=corr, thread_id=thr,
            limit=int(args.get("limit", 100)),
        )
        return text_result(msgs)
    if name == "board_digest":
        summary = store.digest(
            channel=args.get("channel"),
            since=args.get("since"),
            max_messages=int(args.get("max_messages", 200)),
        )
        return text_result(summary)
    if name == "board_status":
        cfg = store.load_config()
        return text_result({
            "messages_total": store.total_count(),
            "channel_stats": store.channel_stats(),
            "last_sweep": store.last_sweep(),
            "config": {
                "engine_port": cfg["engine_port"],
                "sweep_interval_s": cfg["sweep_interval_s"],
                "sweeper_enabled": bool(cfg["sweeper_enabled"]),
                "channels": cfg["channels"],
            },
        })
    if name == "board_channels":
        cfg = store.load_config()
        from .. import schemas as schemas
        return text_result({
            "channels": cfg["channels"],
            "defaults": list(schemas.DEFAULT_CHANNELS),
        })
    if name == "board_message_types":
        from .. import schemas as schemas
        return text_result({
            "message_types": list(schemas.MESSAGE_TYPES),
            "visibility_scopes": list(schemas.VISIBILITY_SCOPES),
        })
    return error_result(f"unknown tool: {name}")
