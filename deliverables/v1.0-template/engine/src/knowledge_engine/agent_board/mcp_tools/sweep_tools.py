"""Manual sweeper trigger MCP tool."""

from __future__ import annotations

from typing import Any

from .base import BoardContext, error_result, kb_sweeper, text_result

GROUP = "board.sweep"


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "board_sweep_now",
            "description": (
                "Run one sweeper pass immediately — TTL prune, stale-blocker "
                "reminders, per-channel digests. Returns the resulting counts. "
                "Useful for tests and on-demand maintenance."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: BoardContext | None) -> dict[str, Any]:
    if name == "board_sweep_now":
        return text_result(kb_sweeper.sweep_once())
    return error_result(f"unknown tool: {name}")
