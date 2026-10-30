"""The ``serena.*`` MCP tools (the ``serena`` group; only when ``KE_SERENA_ENABLED=1``).

``serena.find_symbol``, ``serena.get_symbols_overview`` and
``serena.find_referencing_symbols`` take a configured ``project_id`` plus the
engine's typed arguments and return normalized results with code locators
(:func:`~knowledge_engine.serena.manager.run_tool`); ``serena.status``
reports the configuration and the sessions without starting any process.
Listing the tools never starts Serena; the first call to a project does.
Failures are ``isError`` results carrying ``{"error": {...}}``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..knowledge.errors import InternalError, KnowledgeError
from .manager import SerenaManager, default_manager, run_tool
from .tools import tool_definitions

_logger = logging.getLogger(__name__)

STATUS_TOOL = "serena.status"


class SerenaToolContext:
    """Resolves the process's Serena manager on each call."""

    def __init__(self, manager: Callable[[], SerenaManager] = default_manager) -> None:
        self.manager = manager


def _envelope(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
    }
    if is_error:
        envelope["isError"] = True
    return envelope


def definitions() -> list[dict[str, Any]]:
    """The group's tool definitions (the three read tools and ``serena.status``)."""
    return [
        *tool_definitions(),
        {
            "name": STATUS_TOOL,
            "description": "Configured Serena projects, the pinned version and each "
            "project's session state (never starts Serena).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def dispatch(name: str, arguments: dict[str, Any], context: SerenaToolContext) -> dict[str, Any]:
    """Run tool ``name``; typed failures become ``isError`` results."""
    try:
        manager = context.manager()
        if name == STATUS_TOOL:
            return _envelope(manager.status())
        return _envelope(run_tool(manager, name, arguments))
    except KnowledgeError as exc:
        _logger.info("MCP %s failed: %s (%s)", name, exc.message, exc.code)
        return _envelope({"error": exc.to_dict()}, is_error=True)
    except Exception:
        _logger.exception("MCP %s raised an unexpected exception", name)
        return _envelope({"error": InternalError("internal error").to_dict()}, is_error=True)


def collect_tools(
    manager: Callable[[], SerenaManager] = default_manager,
) -> tuple[list[dict[str, Any]], dict[str, Any], SerenaToolContext]:
    """``(tool definitions, dispatch map, context)`` for :func:`mcp.toolkit.register_group`."""
    defs = definitions()
    return defs, {tool["name"]: dispatch for tool in defs}, SerenaToolContext(manager)


__all__ = ["STATUS_TOOL", "SerenaToolContext", "collect_tools", "definitions", "dispatch"]
