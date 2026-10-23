"""Dynamic discovery and merging of project-context MCP tool modules.

``collect_tools(cfg)`` imports every ``*_tools.py`` module in this package and
returns a flat list of MCP tool definitions plus a ``name -> dispatch`` map
(through :func:`knowledge_engine.mcp.tool_groups.collect_group`, shared with
the agent board). Adding a tool group means adding a file — nothing here
needs editing.
"""

from __future__ import annotations

from ...mcp.tool_groups import DispatchFn, collect_group
from ..config import ProjectContextConfig


def collect_tools(cfg: ProjectContextConfig) -> tuple[list[dict], dict[str, DispatchFn]]:
    """Return ``(tool_defs, dispatch_map)`` for all enabled tool groups.

    Tool groups are exposed whenever ``cfg.mcp.enabled`` is true; individual
    safety gates (mutation, raw logs, embeddings, full content) are enforced at
    call time by the modules themselves, so agents can always discover a tool
    and learn it is disabled rather than have it vanish.
    """
    if not cfg.mcp.enabled:
        return [], {}
    return collect_group(__name__, __path__, cfg)


__all__ = ["DispatchFn", "collect_tools"]
