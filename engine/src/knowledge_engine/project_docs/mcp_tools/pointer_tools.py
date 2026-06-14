"""Pointer MCP tools.

Compact-by-default tools for resolving and inspecting project-docs pointers. A
*pointer* is a stable URI that names a stored record (see
:mod:`knowledge_engine.project_docs.pointers`). All tools operate against a
project content DB selected by ``project_fp`` (defaulting to the current root's
project). An unknown ``project_fp`` resolves to no connection and yields a
structured ``{"status": "unknown_project"}`` result rather than an error, so an
agent can discover state safely.

``resolve_pointer`` returns a summary envelope by default; the full record body
is included only when the caller requests ``mode="full"`` *and* the
``mcp.allow_full_content`` gate is enabled. When the gate is off, a
``{"status": "not_permitted"}`` result is returned instead — never an error.
"""

from __future__ import annotations

from typing import Any

from .. import pointers
from .base import ToolContext, status_result, text_result

GROUP = "pointer"

#: Result mode that requests the full record body (gated).
_FULL_MODE = "full"


def tools(cfg) -> list[dict]:
    """Return the pointer tool definitions for this installation."""
    return [
        {
            "name": "project_docs.resolve_pointer",
            "description": "Resolve a ke-doc:// (or KE-DOCSTRING://) pointer URI to a "
                           "compact record envelope. Full content is gated by "
                           "mcp.allow_full_content; defaults to summary mode.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The pointer URI to resolve.",
                    },
                    "project_fp": {
                        "type": "string",
                        "description": "Project fingerprint selecting the DB "
                                       "(defaults to the current root's project).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "description": "Result mode; 'full' requires the "
                                       "allow_full_content gate.",
                    },
                },
                "required": ["uri"],
            },
        },
        {
            "name": "project_docs.list_pointers",
            "description": "List allocated pointers, optionally filtered to a single "
                           "record_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {
                        "type": "string",
                        "description": "Project fingerprint selecting the DB.",
                    },
                    "record_id": {
                        "type": "string",
                        "description": "Restrict the listing to this record's pointers.",
                    },
                },
            },
        },
        {
            "name": "project_docs.validate_pointer",
            "description": "Validate a pointer URI's grammar and existence; returns "
                           "{valid, exists, content_hash_match}.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The pointer URI to validate.",
                    },
                    "project_fp": {
                        "type": "string",
                        "description": "Project fingerprint selecting the DB.",
                    },
                },
                "required": ["uri"],
            },
        },
        {
            "name": "project_docs.pointer_backrefs",
            "description": "Return recorded back-references (source locations that cite "
                           "a pointer) for a pointer id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pointer_id": {
                        "type": "string",
                        "description": "The pointer URI whose back-references to fetch.",
                    },
                    "project_fp": {
                        "type": "string",
                        "description": "Project fingerprint selecting the DB.",
                    },
                },
                "required": ["pointer_id"],
            },
        },
    ]


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Handle a pointer tool call and return an MCP content envelope."""
    project_fp = args.get("project_fp")
    conn = ctx.project_conn(project_fp)
    if conn is None:
        return status_result("unknown_project", project_fp=project_fp)

    if name == "project_docs.resolve_pointer":
        return _resolve_pointer(args, ctx, conn)

    if name == "project_docs.list_pointers":
        record_id = args.get("record_id")
        return text_result(pointers.list_pointers(conn, record_id=record_id))

    if name == "project_docs.validate_pointer":
        uri = args.get("uri", "")
        return text_result(pointers.validate_pointer(conn, uri))

    if name == "project_docs.pointer_backrefs":
        pointer_id = args.get("pointer_id", "")
        return text_result(pointers.pointer_backrefs(conn, pointer_id))

    return status_result("unknown_tool", name=name)


def _resolve_pointer(
    args: dict[str, Any], ctx: ToolContext, conn: Any
) -> dict[str, Any]:
    """Resolve a pointer, enforcing the full-content gate before returning."""
    uri = args.get("uri", "")
    mode = args.get("mode", ctx.cfg.mcp.default_result_mode)

    if mode == _FULL_MODE and not ctx.cfg.mcp.allow_full_content:
        return status_result("not_permitted", reason="full content disabled", uri=uri)

    try:
        envelope = pointers.resolve(conn, uri, mode=mode, cfg=ctx.cfg)
    except ValueError as exc:
        return status_result("invalid_uri", uri=uri, detail=str(exc))

    if envelope is None:
        return status_result("not_found", uri=uri)
    return text_result(envelope)
