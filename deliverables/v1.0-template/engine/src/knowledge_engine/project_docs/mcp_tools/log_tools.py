"""MCP tool module for test/build/runtime logs.

Exposes compact-by-default query tools over the log/test records stored by
:mod:`knowledge_engine.project_docs.logs`. Every tool mirrors the project-wide
safety posture:

* runtime-log tools are dark unless ``scanner.discovery.include_runtime_logs``
  is enabled -- they return a structured ``{"status": "disabled"}`` result
  rather than raising, so an agent can still discover the capability;
* raw (un-sanitized) log bodies are never returned unless ``mcp.allow_raw_logs``
  is set -- otherwise raw-log fields are stripped from every payload before it
  leaves the process;
* a query whose backing store function is not present returns
  ``{"status": "not_configured"}`` instead of raising.

Dispatch routes ``project_docs.<verb>`` names to functions in
:mod:`knowledge_engine.project_docs.logs`.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .. import logs
from . import base

GROUP = "log"

#: Tool names handled by this module that operate on runtime logs. These are
#: gated behind ``scanner.discovery.include_runtime_logs``.
_RUNTIME_TOOLS = frozenset({"project_docs.search_runtime_logs"})

#: Keys whose values may contain raw (un-sanitized) log text. Stripped unless
#: ``mcp.allow_raw_logs`` is enabled.
_RAW_KEYS = ("raw_log", "raw_output", "raw_body")


def tools(cfg: Any) -> list[dict]:
    """Return the MCP tool definitions for the log/test group.

    The definitions are always advertised so an agent can introspect the
    capability surface; gating happens at dispatch time (disabled / unconfigured
    features return a structured status instead of an error).
    """

    project_prop = {
        "project_fp": {
            "type": "string",
            "description": "Project fingerprint; defaults to the active project.",
        },
        "branch_fp": {
            "type": "string",
            "description": "Optional branch fingerprint filter.",
        },
    }
    query_prop = {
        "query": {"type": "string", "description": "Full-text query string."},
        "limit": {
            "type": "integer",
            "description": "Maximum rows to return (default 10).",
        },
        "mode": {
            "type": "string",
            "enum": ["summary", "full"],
            "description": "Result detail level; defaults to 'summary'.",
        },
    }
    return [
        {
            "name": "project_docs.search_test_logs",
            "description": "Search sanitized test-run logs by full-text query.",
            "inputSchema": {
                "type": "object",
                "properties": {**query_prop, **project_prop},
            },
        },
        {
            "name": "project_docs.search_build_logs",
            "description": "Search sanitized build logs by full-text query.",
            "inputSchema": {
                "type": "object",
                "properties": {**query_prop, **project_prop},
            },
        },
        {
            "name": "project_docs.search_runtime_logs",
            "description": (
                "Search sanitized runtime logs (disabled unless "
                "scanner.discovery.include_runtime_logs is enabled)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {**query_prop, **project_prop},
            },
        },
        {
            "name": "project_docs.get_test_history",
            "description": "Return recent test runs, newest first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "limit": {
                        "type": "integer",
                        "description": "Maximum test runs to return (default 10).",
                    },
                },
            },
        },
        {
            "name": "project_docs.get_failure_context",
            "description": "Return the failure summary/context for a test run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "test_run_id": {
                        "type": "string",
                        "description": "Test run id to inspect (required).",
                    },
                },
                "required": ["test_run_id"],
            },
        },
        {
            "name": "project_docs.get_latest_test_summary",
            "description": "Return the most recent test-run summary.",
            "inputSchema": {
                "type": "object",
                "properties": {**project_prop},
            },
        },
    ]


def dispatch(name: str, args: dict, ctx: Any) -> dict:
    """Route a ``project_docs.<verb>`` log tool call to its handler.

    Runtime-log tools short-circuit to ``status_result("disabled")`` when the
    runtime-log gate is off. A project DB that cannot be resolved returns
    ``status_result("not_configured")``. A query whose backing function is not
    available in the logs store also returns ``not_configured`` rather than
    raising. Unknown names return ``status_result("unknown_tool")``.
    """

    if name in _RUNTIME_TOOLS and not _runtime_logs_enabled(ctx.cfg):
        return base.status_result("disabled", tool=name)

    conn = ctx.project_conn(project_fp=args.get("project_fp"))
    if conn is None:
        return base.status_result("not_configured", tool=name)

    branch_fp = args.get("branch_fp")
    allow_raw = _raw_logs_allowed(ctx.cfg)

    if name in (
        "project_docs.search_test_logs",
        "project_docs.search_build_logs",
        "project_docs.search_runtime_logs",
    ):
        return _search(name, conn, args, branch_fp, allow_raw)

    if name == "project_docs.get_test_history":
        fn = _logs_fn("get_test_history")
        if fn is None:
            return base.status_result("not_configured", tool=name)
        rows = fn(conn, branch_fp=branch_fp, limit=_limit(args))
        return base.text_result({"results": _rows(rows, allow_raw)})

    if name == "project_docs.get_failure_context":
        fn = _logs_fn("get_failure_context")
        if fn is None:
            return base.status_result("not_configured", tool=name)
        test_run_id = args.get("test_run_id")
        if not test_run_id:
            return base.status_result("invalid_args", tool=name, detail="test_run_id required")
        row = fn(conn, test_run_id)
        if not row:
            return base.status_result("not_found", tool=name)
        return base.text_result(_scrub_one(_to_dict(row), allow_raw))

    if name == "project_docs.get_latest_test_summary":
        fn = _logs_fn("get_latest_test_summary")
        if fn is None:
            return base.status_result("not_configured", tool=name)
        row = fn(conn, branch_fp=branch_fp)
        if row is None:
            return base.status_result("not_found", tool=name)
        return base.text_result(_scrub_one(_to_dict(row), allow_raw))

    return base.status_result("unknown_tool", tool=name)


def _search(name: str, conn: Any, args: dict, branch_fp: str | None, allow_raw: bool) -> dict:
    """Dispatch a log full-text search by verb.

    The backing store may not yet expose per-stream search helpers; when a helper
    is absent the call degrades to ``not_configured`` rather than raising.
    """

    verb = name.rsplit(".", 1)[-1]
    fn = _logs_fn(verb)
    if fn is None:
        return base.status_result("not_configured", tool=name)
    rows = fn(
        conn,
        args.get("query", ""),
        limit=_limit(args),
        branch_fp=branch_fp,
        mode=_mode(args),
    )
    return base.text_result({"results": _rows(rows, allow_raw)})


def _logs_fn(verb: str):
    """Resolve a query function from the logs module, or ``None`` if absent.

    Keeps dispatch resilient to incremental availability of the underlying log
    store: an as-yet-unimplemented query surfaces as ``not_configured`` rather
    than raising ``AttributeError``.
    """

    return getattr(logs, verb, None)


def _runtime_logs_enabled(cfg: Any) -> bool:
    """Return True when runtime-log capability is configured on."""

    discovery = getattr(getattr(cfg, "scanner", None), "discovery", None)
    return bool(getattr(discovery, "include_runtime_logs", False))


def _raw_logs_allowed(cfg: Any) -> bool:
    """Return True when raw (un-sanitized) log bodies may be returned."""

    return bool(getattr(getattr(cfg, "mcp", None), "allow_raw_logs", False))


def _limit(args: dict) -> int:
    """Coerce the ``limit`` argument to a sane positive integer."""

    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        return 10
    return limit if limit > 0 else 10


def _mode(args: dict) -> str:
    """Return the requested result mode, defaulting to 'summary'."""

    mode = args.get("mode")
    return mode if mode in ("summary", "full") else "summary"


def _to_dict(row: Any) -> Any:
    """Normalize a record (dataclass, sqlite Row, or dict) to a plain dict."""

    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):  # sqlite3.Row
        return {k: row[k] for k in row.keys()}
    return row


def _rows(rows: Any, allow_raw: bool) -> list:
    """Convert and scrub an iterable of records into a list of plain dicts."""

    return [_scrub_one(_to_dict(row), allow_raw) for row in rows]


def _scrub_one(row: Any, allow_raw: bool) -> Any:
    """Return a copy of ``row`` with raw-log keys removed when not permitted."""

    if not isinstance(row, dict):
        return row
    if allow_raw:
        return dict(row)
    return {k: v for k, v in row.items() if k not in _RAW_KEYS}
