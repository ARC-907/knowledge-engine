"""The ``knowledge_*`` MCP tool group, generated from the admin operation catalog.

Each :data:`~knowledge_engine.knowledge.service.OPERATIONS` entry becomes one
tool named ``knowledge_<operation>`` whose ``inputSchema`` is the operation's
input model schema. The group is mounted by :mod:`knowledge_engine.mcp.server`
like the project-context and board groups (legacy ``schemas + dispatch``
shape), so it needs no MCP SDK import here.

The store is opened lazily on the first call (never while listing tools), so
an MCP server starts even when ``knowledge.db`` cannot be opened; such a call
returns an ``unavailable`` error result. Failures are ``isError`` results
carrying ``{"error": {"code", "message", "details", "retryable"}}``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import KnowledgeConfig
from .errors import InternalError, KnowledgeError, UnavailableError
from .evidence.policy import EvidencePolicy
from .service import OPERATIONS, KnowledgeService, invoke
from .store import KnowledgeStore

_logger = logging.getLogger(__name__)

TOOL_PREFIX = "knowledge_"

_CONTEXTS: list[KnowledgeToolContext] = []
_CONTEXTS_LOCK = threading.Lock()


class KnowledgeToolContext:
    """Opens the configured store on first use; :meth:`close` releases it."""

    def __init__(
        self, db_path: Path, *, profile: str, evidence_policy: EvidencePolicy | None = None
    ) -> None:
        self.db_path = db_path
        self.profile = profile
        self.evidence_policy = evidence_policy
        self._lock = threading.Lock()
        self._service: KnowledgeService | None = None
        with _CONTEXTS_LOCK:
            _CONTEXTS.append(self)

    @classmethod
    def from_config(cls, config: KnowledgeConfig) -> KnowledgeToolContext:
        return cls(config.db_path, profile=config.profile, evidence_policy=config.evidence)

    def service(self) -> KnowledgeService:
        with self._lock:
            if self._service is None or self._service.store.closed:
                try:
                    store = KnowledgeStore.open(self.db_path)
                except KnowledgeError:
                    raise
                except Exception as exc:
                    _logger.exception("cannot open the knowledge store %s", self.db_path)
                    raise UnavailableError("the knowledge store cannot be opened") from exc
                self._service = KnowledgeService(
                    store, profile=self.profile, evidence_policy=self.evidence_policy
                )
            return self._service

    def close(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.store.close()
                self._service = None


def close_all() -> int:
    """Close every store opened by a tool context (server shutdown, tests)."""
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
    """``{"name", "description", "inputSchema"}`` for every knowledge operation."""
    return [
        {
            "name": TOOL_PREFIX + op.name,
            "description": op.description,
            "inputSchema": op.input_model.model_json_schema(),
        }
        for op in OPERATIONS
    ]


def dispatch(name: str, arguments: dict[str, Any], context: KnowledgeToolContext) -> dict[str, Any]:
    """Run tool ``name``; typed failures become ``isError`` results."""
    operation = name.removeprefix(TOOL_PREFIX)
    try:
        return _envelope(invoke(context.service(), operation, arguments))
    except KnowledgeError as exc:
        _logger.info("MCP %s failed: %s (%s)", name, exc.message, exc.code)
        return _envelope({"error": exc.to_dict()}, is_error=True)
    except Exception:
        _logger.exception("MCP %s raised an unexpected exception", name)
        return _envelope({"error": InternalError("internal error").to_dict()}, is_error=True)


def collect_tools(
    config: KnowledgeConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any], KnowledgeToolContext]:
    """``(tool definitions, dispatch map, context)`` for :func:`mcp.toolkit.register_group`."""
    defs = tool_definitions()
    return defs, {tool["name"]: dispatch for tool in defs}, KnowledgeToolContext.from_config(config)


__all__ = [
    "TOOL_PREFIX",
    "KnowledgeToolContext",
    "close_all",
    "collect_tools",
    "dispatch",
    "tool_definitions",
]
