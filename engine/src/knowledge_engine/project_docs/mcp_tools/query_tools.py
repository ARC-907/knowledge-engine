"""Read-only query tools for the project-docs subsystem.

These MCP tools expose the FTS5 search surface (``project_docs.search`` module)
to agents. They are compact-by-default: every tool returns summaries plus
metadata, never document bodies, unless the caller invokes
``project_docs.get_full_content`` *and* the ``mcp.allow_full_content`` gate is
enabled. When that gate is off the full-content tool returns a structured
``{"status": "not_permitted"}`` envelope rather than raising, so an agent can
discover the capability and learn it is gated.

Every tool accepts an optional ``project_fp`` selecting which per-project content
DB to read; when omitted the current project (resolved from the active root) is
used. If a given ``project_fp`` is unknown the tool returns
``{"status": "unknown_project"}``.
"""

from __future__ import annotations

from typing import Any

from .. import search as pdsearch
from ..schema import RESULT_FULL, RESULT_SUMMARY
from .base import ToolContext, status_result, text_result

GROUP = "query"


def tools(cfg) -> list[dict]:
    """Return MCP tool definitions for the query group."""
    return [
        {
            "name": "project_docs.search",
            "description": "Full-text search the project documentation store; "
                           "returns ranked summaries (compact, no bodies).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "FTS5 MATCH expression."},
                    "project_fp": {"type": "string"},
                    "limit": {"type": "integer"},
                    "branch_fp": {"type": "string"},
                    "category": {"type": "string"},
                    "source_path": {"type": "string"},
                    "git_commit": {"type": "string"},
                    "since": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "project_docs.get_record",
            "description": "Fetch a single documentation record by id "
                           "(summary + metadata only).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "project_fp": {"type": "string"},
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "project_docs.get_summary",
            "description": "Return the stored summary for a record without its body.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "project_fp": {"type": "string"},
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "project_docs.get_full_content",
            "description": "Return a record including its full body. Gated: requires "
                           "mcp.allow_full_content, else returns not_permitted.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "project_fp": {"type": "string"},
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "project_docs.search_by_path",
            "description": "List records for an exact source path (newest first).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_path": {"type": "string"},
                    "project_fp": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["source_path"],
            },
        },
        {
            "name": "project_docs.search_by_type",
            "description": "List records for a single document category (newest first).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "project_fp": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["category"],
            },
        },
        {
            "name": "project_docs.search_by_branch",
            "description": "List records for a single branch fingerprint (newest first).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "branch_fp": {"type": "string"},
                    "project_fp": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["branch_fp"],
            },
        },
        {
            "name": "project_docs.search_recent",
            "description": "List the most recently created records (newest first).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    ]


def _limit(args: dict[str, Any], default: int = 10) -> int:
    """Read an integer ``limit`` from ``args`` with a safe default."""
    try:
        return int(args.get("limit", default))
    except (TypeError, ValueError):
        return default


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Route a query tool call to the search module on the right project DB."""
    conn = ctx.project_conn(args.get("project_fp"))
    if conn is None:
        return status_result("unknown_project", project_fp=args.get("project_fp"))

    if name == "project_docs.search":
        rows = pdsearch.search(
            conn,
            args.get("query", ""),
            limit=_limit(args),
            branch_fp=args.get("branch_fp"),
            category=args.get("category"),
            source_path=args.get("source_path"),
            git_commit=args.get("git_commit"),
            since=args.get("since"),
            mode=RESULT_SUMMARY,
        )
        return text_result({"results": rows, "count": len(rows)})

    if name in ("project_docs.get_record", "project_docs.get_summary"):
        record = pdsearch.get_record(conn, args.get("record_id", ""), mode=RESULT_SUMMARY)
        if record is None:
            return status_result("not_found", record_id=args.get("record_id"))
        return text_result(record)

    if name == "project_docs.get_full_content":
        if not ctx.cfg.mcp.allow_full_content:
            return status_result("not_permitted", capability="mcp.allow_full_content")
        record = pdsearch.get_record(
            conn, args.get("record_id", ""), mode=RESULT_FULL, cfg=ctx.cfg
        )
        if record is None:
            return status_result("not_found", record_id=args.get("record_id"))
        return text_result(record)

    if name == "project_docs.search_by_path":
        rows = pdsearch.search_by_path(
            conn, args.get("source_path", ""), limit=_limit(args)
        )
        return text_result({"results": rows, "count": len(rows)})

    if name == "project_docs.search_by_type":
        rows = pdsearch.search_by_type(
            conn, args.get("category", ""), limit=_limit(args)
        )
        return text_result({"results": rows, "count": len(rows)})

    if name == "project_docs.search_by_branch":
        rows = pdsearch.search_by_branch(
            conn, args.get("branch_fp", ""), limit=_limit(args)
        )
        return text_result({"results": rows, "count": len(rows)})

    if name == "project_docs.search_recent":
        rows = pdsearch.search_recent(conn, limit=_limit(args))
        return text_result({"results": rows, "count": len(rows)})

    return status_result("unknown_tool", name=name)
