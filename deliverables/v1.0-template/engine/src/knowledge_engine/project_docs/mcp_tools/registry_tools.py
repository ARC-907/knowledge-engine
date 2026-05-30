"""Registry MCP tools — project & branch identity discovery and registration.

This tool group wires the conservative registry surface
(:mod:`knowledge_engine.project_docs.registry`) and the fingerprint derivation
helpers (:mod:`knowledge_engine.project_docs.fingerprints`) into the MCP server.

All tools operate against the shared fingerprint registry DB obtained from
``ctx.registry_conn()``; none touch per-project content DBs. Registering a
project is a mutation, but recording project/branch *identity* is a safe,
idempotent operation (no user content is written), so ``register_project`` is
always permitted regardless of the ``mcp.allow_mutating_tools`` gate.

Every handler returns a :func:`~..mcp_tools.base.text_result` envelope; unknown
fingerprints degrade to a structured ``status`` payload rather than raising.
"""

from __future__ import annotations

from typing import Any

from .. import fingerprints
from .. import registry
from .base import ToolContext, status_result, text_result

GROUP = "registry"


def tools(cfg) -> list[dict]:
    """Return the MCP tool definitions for the registry group."""
    return [
        {
            "name": "project_docs.list_projects",
            "description": "List every project registered in the fingerprint "
                           "registry (fingerprint, name, root path, created time).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_docs.register_project",
            "description": "Register (or idempotently look up) the project at the "
                           "current root in the registry. Records identity only; "
                           "writes no user content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional human-facing project name "
                                       "(defaults to the root directory name).",
                    },
                    "fingerprint": {
                        "type": "string",
                        "description": "Optional manual fingerprint override.",
                    },
                },
            },
        },
        {
            "name": "project_docs.validate_project",
            "description": "Validate that a project fingerprint exists and report "
                           "its name and branch count.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {
                        "type": "string",
                        "description": "The project fingerprint to validate.",
                    }
                },
                "required": ["project_fp"],
            },
        },
        {
            "name": "project_docs.list_branches",
            "description": "List the branches registered for a project fingerprint.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {
                        "type": "string",
                        "description": "The project fingerprint whose branches to list.",
                    }
                },
                "required": ["project_fp"],
            },
        },
        {
            "name": "project_docs.resolve_fingerprint",
            "description": "Derive (without registering) the deterministic project "
                           "and branch fingerprints for the current root and an "
                           "optional branch name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Optional branch name to derive a branch "
                                       "fingerprint for.",
                    }
                },
            },
        },
        {
            "name": "project_docs.current_context",
            "description": "Resolve and register the current project + branch "
                           "context for the active root (project_fp, branch, "
                           "branch_fp, git availability).",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Handle a registry tool call and return an MCP content envelope."""
    conn = ctx.registry_conn()
    root = str(ctx.root)
    cfg = ctx.cfg

    if name == "project_docs.list_projects":
        return text_result({"projects": registry.list_projects(conn)})

    if name == "project_docs.register_project":
        result = registry.register_project(
            conn,
            root,
            cfg,
            name=args.get("name"),
            fingerprint=args.get("fingerprint"),
        )
        return text_result(result)

    if name == "project_docs.validate_project":
        project_fp = args.get("project_fp")
        if not project_fp:
            return status_result("invalid_arguments", detail="project_fp is required")
        return text_result(registry.validate_project(conn, project_fp))

    if name == "project_docs.list_branches":
        project_fp = args.get("project_fp")
        if not project_fp:
            return status_result("invalid_arguments", detail="project_fp is required")
        return text_result({"project_fp": project_fp,
                            "branches": registry.list_branches(conn, project_fp)})

    if name == "project_docs.resolve_fingerprint":
        from ..paths import canonical_root
        croot = canonical_root(root)
        project_fp = fingerprints.project_fp(croot)
        payload: dict[str, Any] = {"project_fp": project_fp}
        branch = args.get("branch")
        if branch:
            payload["branch"] = branch
            payload["branch_fp"] = fingerprints.branch_fp(project_fp, branch)
        return text_result(payload)

    if name == "project_docs.current_context":
        return text_result(registry.current_context(root, cfg, conn))

    return status_result("unknown_tool", name=name)
