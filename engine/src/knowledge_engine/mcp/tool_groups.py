"""SDK-free building blocks of the legacy JSON-schema MCP tool groups.

The project-context and agent-board subsystems each ship a ``mcp_tools``
package whose ``*_tools.py`` modules export ``GROUP``, ``tools(...)`` and
``dispatch(name, args, ctx)``. This module holds what those packages share --
the MCP result envelopes and the module discovery / merge loop -- so neither
re-implements them. :mod:`knowledge_engine.mcp.toolkit` re-exports these names
and mounts the collected groups on the SDK server.

Nothing here imports the MCP SDK, so the substrates can build their tool
lists (``knowledge-engine capabilities``) without loading it.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from collections.abc import Callable, Iterable, Iterator
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

DispatchFn = Callable[[str, dict[str, Any], Any], Any]
"""``dispatch(name, arguments, context) -> {"content": [...]}`` (an MCP result envelope)."""

ToolDefs = list[dict[str, Any]]


# ── Result envelopes ─────────────────────────────────────────────────


def text_result(obj: Any) -> dict[str, Any]:
    """Wrap a JSON-serialisable object as an MCP text content envelope."""
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, default=str)}]}


def status_result(status: str, **extra: Any) -> dict[str, Any]:
    """Envelope carrying a machine-readable ``status`` field (plus ``extra``).

    Used for the disabled / not-configured / not-permitted answers that let an
    agent discover a capability instead of hitting an error.
    """
    payload: dict[str, Any] = {"status": status}
    payload.update(extra)
    return text_result(payload)


def error_result(message: str, **extra: Any) -> dict[str, Any]:
    """Envelope carrying an ``error`` message (plus ``extra``)."""
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return text_result(payload)


# ── Discovery ────────────────────────────────────────────────────────


def iter_tool_modules(package: str, path: Iterable[str]) -> Iterator[ModuleType]:
    """Import and yield every ``*_tools`` module of ``package`` (``path`` = its ``__path__``)."""
    for info in sorted(pkgutil.iter_modules(path), key=lambda item: item.name):
        name = info.name
        if name.endswith("_tools") and name != "base":
            yield importlib.import_module(f"{package}.{name}")


def collect_group(
    package: str, path: Iterable[str], *tools_args: Any
) -> tuple[ToolDefs, dict[str, DispatchFn]]:
    """Merge the tool modules of ``package`` into ``(tool_defs, dispatch_map)``.

    Each module's ``tools(*tools_args)`` supplies definitions and its
    ``dispatch`` handles every tool it defines. A module missing either is
    logged and skipped so one malformed file cannot take the server down.
    """
    defs: ToolDefs = []
    dispatch: dict[str, DispatchFn] = {}
    for module in iter_tool_modules(package, path):
        try:
            group_tools = module.tools(*tools_args)
            handler: DispatchFn = module.dispatch
        except AttributeError as exc:
            logger.warning("skipping tool module %s: %s", module.__name__, exc)
            continue
        for tool in group_tools:
            defs.append(tool)
            dispatch[tool["name"]] = handler
    return defs, dispatch


__all__ = [
    "DispatchFn",
    "ToolDefs",
    "collect_group",
    "error_result",
    "iter_tool_modules",
    "status_result",
    "text_result",
]
