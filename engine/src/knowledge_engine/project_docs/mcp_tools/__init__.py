"""Dynamic discovery and merging of project-docs MCP tool modules.

``collect_tools(cfg)`` imports every ``*_tools.py`` module in this package and
returns a flat list of MCP tool definitions plus a ``name -> dispatch`` map.
Adding a tool group means adding a file — nothing here needs editing.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable

from ..config import ProjectDocsConfig

_logger = logging.getLogger(__name__)

DispatchFn = Callable[[str, dict[str, Any], Any], dict[str, Any]]


def _iter_tool_modules():
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.endswith("_tools") and name != "base":
            yield importlib.import_module(f"{__name__}.{name}")


def collect_tools(cfg: ProjectDocsConfig) -> tuple[list[dict], dict[str, DispatchFn]]:
    """Return ``(tool_defs, dispatch_map)`` for all enabled tool groups.

    Tool groups are exposed whenever ``cfg.mcp.enabled`` is true; individual
    safety gates (mutation, raw logs, embeddings, full content) are enforced at
    call time by the modules themselves, so agents can always discover a tool
    and learn it is disabled rather than have it vanish.
    """
    defs: list[dict] = []
    dispatch: dict[str, DispatchFn] = {}
    if not cfg.mcp.enabled:
        return defs, dispatch

    for module in _iter_tool_modules():
        try:
            group_tools = module.tools(cfg)
            handler: DispatchFn = module.dispatch
        except AttributeError as exc:  # malformed module — skip, don't crash the server
            _logger.warning("skipping tool module %s: %s", module.__name__, exc)
            continue
        for tool in group_tools:
            defs.append(tool)
            dispatch[tool["name"]] = handler
    return defs, dispatch
