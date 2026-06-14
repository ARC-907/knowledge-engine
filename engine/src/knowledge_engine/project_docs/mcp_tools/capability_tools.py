"""Capability-discovery tools.

These let an agent introspect what the local installation can do — config gate
states, DB presence, and the catalog of available tools — before attempting
heavier scanner / embedding / git / mutation workflows. They never error when a
feature is off; they report it.
"""

from __future__ import annotations

from typing import Any

from ..config import _as_dict
from .base import ToolContext, text_result

GROUP = "capability"


def tools(cfg) -> list[dict]:
    return [
        {
            "name": "project_docs.capabilities",
            "description": "Report which project-docs capabilities are enabled "
                           "(scanner, pointers, git, embeddings, mutation, raw logs).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_docs.config_status",
            "description": "Return the effective project-docs configuration "
                           "(safe, non-secret fields only).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_docs.healthcheck",
            "description": "Check that the fingerprint registry and project DBs "
                           "are reachable and report basic counts.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_docs.explain_available_tools",
            "description": "List all project-docs MCP tools grouped by capability, "
                           "with each group's enablement state.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _capabilities(cfg) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "scanner_enabled": cfg.scanner.enabled,
        "scanner_mode": cfg.scanner.mode,
        "pointer_replacement_enabled": cfg.scanner.pointer_replacement.enabled,
        "source_mutation_allowed": cfg.scanner.pointer_replacement.allow_source_mutation,
        "git_enabled": cfg.git.enabled,
        "git_diff_summaries": cfg.git.include_diff_summaries,
        "git_full_diffs": cfg.git.include_full_diffs,
        "embeddings_enabled": cfg.embeddings.enabled,
        "embeddings_remote_allowed": cfg.embeddings.allow_remote_provider,
        "raw_log_retention": cfg.ingestion.retain_raw_content,
        "mcp_full_content": cfg.mcp.allow_full_content,
        "mcp_raw_logs": cfg.mcp.allow_raw_logs,
        "mcp_embedding_search": cfg.mcp.allow_embedding_search,
        "mcp_mutating_tools": cfg.mcp.allow_mutating_tools,
        "default_result_mode": cfg.mcp.default_result_mode,
    }


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    cfg = ctx.cfg
    if name == "project_docs.capabilities":
        return text_result(_capabilities(cfg))

    if name == "project_docs.config_status":
        return text_result(_as_dict(cfg))

    if name == "project_docs.healthcheck":
        health: dict[str, Any] = {"root": str(ctx.root), "registry": "unknown"}
        try:
            reg = ctx.registry_conn()
            row = reg.execute("SELECT COUNT(*) AS c FROM projects").fetchone()
            health["registry"] = "ok"
            health["project_count"] = int(row["c"]) if row else 0
        except Exception as exc:  # noqa: BLE001 - health must never raise
            health["registry"] = "error"
            health["error"] = str(exc)
        return text_result(health)

    if name == "project_docs.explain_available_tools":
        from . import collect_tools
        defs, _ = collect_tools(cfg)
        groups: dict[str, list[str]] = {}
        for tool in defs:
            grp = tool["name"].split(".")[-1].split("_")[0] if "." in tool["name"] else "other"
            groups.setdefault(grp, []).append(tool["name"])
        return text_result(
            {"capabilities": _capabilities(cfg), "groups": groups, "tools": defs, "count": len(defs)}
        )

    return text_result({"status": "unknown_tool", "name": name})
