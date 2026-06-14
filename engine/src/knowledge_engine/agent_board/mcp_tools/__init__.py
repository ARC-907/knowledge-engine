"""Auto-discovered MCP tool group for the agent board.

Mirrors the `project_docs.mcp_tools` pattern: every file in this package named
``*_tools.py`` exports three module-level attributes:

* ``GROUP: str``
* ``def tools() -> list[dict]``
* ``def dispatch(name: str, args: dict, ctx) -> dict``

``collect_tools()`` walks this package and returns the merged list + dispatch
map for `mcp_server.py` to mount alongside the engine's base tools and the
project-docs tools.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable

_logger = logging.getLogger(__name__)

DispatchFn = Callable[[str, dict[str, Any], Any], dict[str, Any]]


def _iter_tool_modules():
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.endswith("_tools") and name != "base":
            yield importlib.import_module(f"{__name__}.{name}")


def collect_tools() -> tuple[list[dict], dict[str, DispatchFn]]:
    """Return (tool_defs, dispatch_map) for all board MCP tool groups."""
    defs: list[dict] = []
    dispatch: dict[str, DispatchFn] = {}
    for mod in _iter_tool_modules():
        try:
            group_defs = mod.tools()  # type: ignore[attr-defined]
            group_dispatch = mod.dispatch  # type: ignore[attr-defined]
        except AttributeError:
            _logger.warning("skipping malformed tool module: %s", mod.__name__)
            continue
        defs.extend(group_defs)
        for d in group_defs:
            dispatch[d["name"]] = group_dispatch
    return defs, dispatch


__all__ = ["collect_tools", "DispatchFn"]
