"""The ``modules`` MCP tool group: every ready module's tools, frozen at server start.

One loader entry in :data:`knowledge_engine.mcp.server.GROUP_LOADERS` mounts
all modules; adding a module needs a manifest plus operator configuration,
never an edit here or in the server. Each granted tool of an enabled, ready
module is advertised as ``<module_id>.<tool>`` with its manifest input
schema. The list and the schemas are frozen for the server process (ADR
0009): a module re-registered with another manifest answers ``unavailable``
(``restart_required``) until the server restarts, and a disabled module
answers ``unavailable`` at once.

Calls run against the default workspace (``KE_KNOWLEDGE_DEFAULT_WORKSPACE``)
and never ingest. A long-running tool answers immediately with a job id;
``module_job_status`` / ``module_job_cancel`` (advertised only when such a
tool exists) follow it up, so no MCP request is held open for the job.
Failures are ``isError`` results carrying ``{"error": {...}}``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..knowledge.errors import InternalError, KnowledgeError
from .dispatcher import JOB_TOOLS, ModuleHost

_logger = logging.getLogger(__name__)

JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"job_id": {"type": "string", "pattern": "^job_[0-9a-f]{32}$"}},
    "required": ["job_id"],
    "additionalProperties": False,
}


@dataclass
class ModulesToolContext:
    """The host plus the frozen ``public name -> (module, tool, manifest digest)`` map."""

    host: ModuleHost
    tools: dict[str, tuple[str, str, str]] = field(default_factory=dict)


def _envelope(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
    }
    if is_error:
        envelope["isError"] = True
    return envelope


def dispatch(name: str, arguments: dict[str, Any], context: ModulesToolContext) -> dict[str, Any]:
    """Run one module tool or job tool; typed failures become ``isError`` results."""
    try:
        if name == "module_job_status":
            return _envelope(context.host.job_status(str(arguments["job_id"])))
        if name == "module_job_cancel":
            return _envelope(context.host.job_cancel(str(arguments["job_id"])))
        module_id, tool, digest = context.tools[name]
        result = context.host.invoke(
            module_id,
            tool,
            arguments,
            surface="mcp",
            principal="mcp",
            expected_digest=digest,
        )
        return _envelope(result)
    except KnowledgeError as exc:
        _logger.info("MCP %s failed: %s", name, exc.code)
        return _envelope({"error": exc.to_dict()}, is_error=True)
    except Exception:
        _logger.exception("MCP %s raised an unexpected exception", name)
        return _envelope({"error": InternalError("internal error").to_dict()}, is_error=True)


def collect_tools(
    host: ModuleHost,
) -> tuple[list[dict[str, Any]], dict[str, Any], ModulesToolContext]:
    """``(tool definitions, dispatch map, context)`` for :func:`mcp.toolkit.register_group`."""
    context = ModulesToolContext(host)
    defs: list[dict[str, Any]] = []
    long_running = False
    for public, entry, tool_name in host.advertised_tools():
        tool = entry.manifest.tool(tool_name)
        if tool is None:  # pragma: no cover - advertised_tools only yields declared tools
            continue
        long_running = long_running or tool.long_running
        suffix = (
            " Long-running: returns a job id; poll module_job_status." if tool.long_running else ""
        )
        defs.append(
            {
                "name": public,
                "description": f"[module {entry.module_id} {entry.manifest.version}] "
                f"{tool.description}{suffix}",
                "inputSchema": tool.input_schema,
            }
        )
        context.tools[public] = (entry.module_id, tool_name, entry.manifest_digest)
    if long_running:
        defs.append(
            {
                "name": JOB_TOOLS[0],
                "description": "Status (and result when finished) of a module job.",
                "inputSchema": JOB_ID_SCHEMA,
            }
        )
        defs.append(
            {
                "name": JOB_TOOLS[1],
                "description": "Cancel a running module job (stops its processes).",
                "inputSchema": JOB_ID_SCHEMA,
            }
        )
    return defs, {d["name"]: dispatch for d in defs}, context


__all__ = ["ModulesToolContext", "collect_tools", "dispatch"]
