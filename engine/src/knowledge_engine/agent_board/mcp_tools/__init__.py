"""Auto-discovered MCP tool group for the agent board.

Mirrors the `project_context.mcp_tools` pattern: every file in this package named
``*_tools.py`` exports three module-level attributes:

* ``GROUP: str``
* ``def tools() -> list[dict]``
* ``def dispatch(name: str, args: dict, ctx) -> dict``

``collect_tools()`` walks this package (through
:func:`knowledge_engine.mcp.tool_groups.collect_group`) and returns the merged
list + dispatch map for `mcp/server.py` to mount alongside the engine's base
tools and the project-context tools.
"""

from __future__ import annotations

from ...mcp.tool_groups import DispatchFn, collect_group


def collect_tools() -> tuple[list[dict], dict[str, DispatchFn]]:
    """Return (tool_defs, dispatch_map) for all board MCP tool groups."""
    return collect_group(__name__, __path__)


__all__ = ["DispatchFn", "collect_tools"]
