"""MCP tools for posting to the board.

Five tools — one generic poster (`board_post`) plus four shorthand convenience
tools matching the typical agent-coordination verbs.
"""

from __future__ import annotations

from typing import Any

from .base import BoardContext, error_result, kb_store, text_result

GROUP = "board.post"


def tools() -> list[dict[str, Any]]:
    msg_input_schema = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel slug — see board_channels for valid values.",
                "default": "ops",
            },
            "message_type": {
                "type": "string",
                "description": "Canonical message type (e.g. claim, status_update, blocker).",
            },
            "sender_node_id": {
                "type": "string",
                "description": "Identity of the sender (branch name, worktree slug, agent id).",
            },
            "subject": {"type": "string", "description": "Short subject line (optional)."},
            "body": {"type": "string", "description": "Free-text body (markdown ok)."},
            "sender_role": {"type": "string"},
            "task_id": {"type": "string"},
            "product_id": {"type": "string"},
            "visibility_scope": {
                "type": "string",
                "enum": ["all", "task", "product", "role", "node"],
                "default": "all",
            },
            "target_node_id": {"type": "string"},
            "target_role": {"type": "string"},
            "requires_ack": {"type": "boolean", "default": False},
            "reply_to": {"type": "string", "description": "message_id this is a reply to."},
            "correlation_id": {"type": "string", "description": "Thread/correlation id."},
            "thread_id": {"type": "string", "description": "Persistent thread id."},
            "ttl_hours": {"type": "integer", "default": 168, "minimum": 0},
            "model_id": {"type": "string"},
        },
        "required": ["message_type", "sender_node_id", "body"],
    }

    return [
        {
            "name": "board_post",
            "description": (
                "Post a schema-validated message to the agent board. Channels: "
                "ops, research, project, worktree, branch, library, planning, "
                "execution, testing, chatter (extend via /board/config)."
            ),
            "inputSchema": msg_input_schema,
        },
        {
            "name": "board_claim",
            "description": "Shorthand for posting a task claim (message_type='claim').",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender_node_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "sender_role": {"type": "string"},
                    "channel": {"type": "string", "default": "ops"},
                },
                "required": ["sender_node_id", "task_id"],
            },
        },
        {
            "name": "board_release",
            "description": "Shorthand for releasing a claim (message_type='release').",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender_node_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "channel": {"type": "string", "default": "ops"},
                },
                "required": ["sender_node_id", "task_id"],
            },
        },
        {
            "name": "board_blocker",
            "description": (
                "Shorthand for a blocker (requires_ack=true). The sweeper will "
                "emit reminders if unacked past the stale threshold."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender_node_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "description": {"type": "string"},
                    "channel": {"type": "string", "default": "ops"},
                    "subject": {"type": "string"},
                },
                "required": ["sender_node_id", "description"],
            },
        },
        {
            "name": "board_ack",
            "description": "Acknowledge a message by id. Appends caller to ack_by list.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "acker": {"type": "string", "description": "Caller identity (e.g. node id)."},
                },
                "required": ["message_id", "acker"],
            },
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: BoardContext | None) -> dict[str, Any]:
    try:
        if name == "board_post":
            msg = kb_store.post_with_validation(args)
            return text_result(msg)
        if name == "board_claim":
            return _claim(args)
        if name == "board_release":
            return _release(args)
        if name == "board_blocker":
            return _blocker(args)
        if name == "board_ack":
            return _ack(args)
        return error_result(f"unknown tool: {name}")
    except ValueError as exc:
        return error_result(str(exc))


def _claim(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "channel": args.get("channel") or "ops",
        "message_type": "claim",
        "sender_node_id": args.get("sender_node_id"),
        "sender_role": args.get("sender_role"),
        "task_id": args.get("task_id"),
        "subject": f"Task {str(args.get('task_id') or '')[:8]}… claimed by {args.get('sender_node_id')}",
        "body": f"Claim by {args.get('sender_node_id')} on task {args.get('task_id')}.",
        "visibility_scope": "all",
    }
    return text_result(kb_store.post_with_validation(payload))


def _release(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "channel": args.get("channel") or "ops",
        "message_type": "release",
        "sender_node_id": args.get("sender_node_id"),
        "task_id": args.get("task_id"),
        "subject": f"Task {str(args.get('task_id') or '')[:8]}… released",
        "body": str(args.get("reason") or "released"),
        "visibility_scope": "all",
    }
    return text_result(kb_store.post_with_validation(payload))


def _blocker(args: dict[str, Any]) -> dict[str, Any]:
    description = str(args.get("description") or "").strip()
    if not description:
        return error_result("description is required for blocker")
    payload = {
        "channel": args.get("channel") or "ops",
        "message_type": "blocker",
        "sender_node_id": args.get("sender_node_id"),
        "task_id": args.get("task_id"),
        "subject": args.get("subject") or f"BLOCKED: {description[:60]}",
        "body": description,
        "visibility_scope": "all",
        "requires_ack": True,
    }
    return text_result(kb_store.post_with_validation(payload))


def _ack(args: dict[str, Any]) -> dict[str, Any]:
    message_id = str(args.get("message_id") or "").strip()
    acker = str(args.get("acker") or "").strip()
    if not message_id or not acker:
        return error_result("message_id and acker are required")
    msg = kb_store.ack_message(message_id, acker)
    if msg is None:
        return error_result(f"message not found: {message_id}")
    return text_result(msg)
