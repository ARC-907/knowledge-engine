"""The ``knowledge.search`` / ``knowledge.open`` MCP tools (the retrieval broker).

Mounted by :mod:`knowledge_engine.mcp.server` as the ``knowledge_broker``
group (legacy ``schemas + dispatch`` shape, no MCP SDK import here). The
input schemas are those of :class:`~.broker_api.SearchRequest` and
:class:`~.broker_api.OpenRequest`; results are the same JSON as
``POST /knowledge/search`` / ``/knowledge/open``. The broker (registry, FTS
index, embedding index, ``knowledge.db``) is opened lazily on the first call,
never while listing tools; failures are ``isError`` results carrying
``{"error": {"code", "message", "details", "retryable"}}``. These tools do
not change the legacy ``search`` tool.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from .broker import KnowledgeBroker
from .broker_api import BrokerEngine, OpenRequest, SearchRequest, run_open, run_search
from .errors import InternalError, KnowledgeError, UnavailableError

_logger = logging.getLogger(__name__)

SEARCH_TOOL = "knowledge.search"
OPEN_TOOL = "knowledge.open"

_CONTEXTS: list[BrokerToolContext] = []
_CONTEXTS_LOCK = threading.Lock()


class BrokerToolContext:
    """Opens a :class:`BrokerEngine` on first use; :meth:`close` releases it."""

    def __init__(self, opener: Callable[[], BrokerEngine] = BrokerEngine.open) -> None:
        self._opener = opener
        self._lock = threading.Lock()
        self._engine: BrokerEngine | None = None
        with _CONTEXTS_LOCK:
            _CONTEXTS.append(self)

    def broker(self) -> KnowledgeBroker:
        with self._lock:
            if self._engine is None or self._engine.broker.closed:
                try:
                    self._engine = self._opener()
                except KnowledgeError:
                    raise
                except Exception as exc:
                    _logger.exception("cannot open the retrieval broker")
                    raise UnavailableError("the retrieval broker cannot be opened") from exc
            return self._engine.broker

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None


def close_all() -> int:
    """Close every broker a tool context opened (server shutdown, tests)."""
    with _CONTEXTS_LOCK:
        contexts = list(_CONTEXTS)
        _CONTEXTS.clear()
    for context in contexts:
        context.close()
    return len(contexts)


def _envelope(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
    }
    if is_error:
        envelope["isError"] = True
    return envelope


def tool_definitions() -> list[dict[str, Any]]:
    """``{"name", "description", "inputSchema"}`` of both broker tools."""
    return [
        {
            "name": SEARCH_TOOL,
            "description": "Scoped search across the knowledge sources of a workspace "
            "(corpus, linked project context). context.workspace_id is required; omitted "
            "selections use the profile; unknown ids are errors. Returns hits with "
            "source/record/revision identity, the effective scope and per-source status; "
            "partial=true when a source failed. Does not change the legacy `search` tool.",
            "inputSchema": SearchRequest.model_json_schema(),
        },
        {
            "name": OPEN_TOOL,
            "description": "Open one record of the scope at an exact revision (from a search "
            "hit; omit revision_id for the current one). Reports verification: verified, "
            "unsupported (revision not stored), unavailable.",
            "inputSchema": OpenRequest.model_json_schema(),
        },
    ]


_RUNNERS = {SEARCH_TOOL: run_search, OPEN_TOOL: run_open}


def dispatch(name: str, arguments: dict[str, Any], context: BrokerToolContext) -> dict[str, Any]:
    """Run tool ``name``; typed failures become ``isError`` results."""
    runner = _RUNNERS.get(name)
    try:
        if runner is None:
            raise InternalError(f"unknown broker tool {name!r}")
        return _envelope(runner(context.broker(), arguments))
    except KnowledgeError as exc:
        _logger.info("MCP %s failed: %s (%s)", name, exc.message, exc.code)
        return _envelope({"error": exc.to_dict()}, is_error=True)
    except Exception:
        _logger.exception("MCP %s raised an unexpected exception", name)
        return _envelope({"error": InternalError("internal error").to_dict()}, is_error=True)


def collect_tools() -> tuple[list[dict[str, Any]], dict[str, Any], BrokerToolContext]:
    """``(tool definitions, dispatch map, context)`` for :func:`mcp.toolkit.register_group`."""
    defs = tool_definitions()
    return defs, {tool["name"]: dispatch for tool in defs}, BrokerToolContext()


__all__ = [
    "OPEN_TOOL",
    "SEARCH_TOOL",
    "BrokerToolContext",
    "close_all",
    "collect_tools",
    "dispatch",
    "tool_definitions",
]
