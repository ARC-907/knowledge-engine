"""Scanner MCP tools.

Expose the integrated scanner's five-mode surface as compact-by-default MCP
tools. Read-only introspection (``scanner_report``, ``scanner_status``,
``scanner_validate``) is always available; the writing / mutating modes
(``scanner_ingest``, ``scanner_plan_pointers``, ``scanner_apply_pointers``)
enforce their config gates through the scanner modules and report
``not_permitted`` instead of raising when a gate is off.

Tool → scanner-module mapping:

* ``scanner_report``         → :func:`scanner.report.run`        (always allowed)
* ``scanner_ingest``         → :func:`scanner.ingest.run`        (scanner.enabled)
* ``scanner_plan_pointers``  → :func:`scanner.pointer_plan.run`  (pointer gates)
* ``scanner_apply_pointers`` → :func:`scanner.pointer_apply.run` (mutation gates)

``scanner_apply_pointers`` additionally requires ``mcp.allow_mutating_tools``;
without it the tool returns ``not_permitted`` before touching the scanner.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from typing import Any

from .. import git_context, schema
from ..models import ScanReport
from ..scanner import (
    ingest as scanner_ingest,
    pointer_apply as scanner_pointer_apply,
    pointer_plan as scanner_pointer_plan,
    report as scanner_report,
)
from ..scanner.validators import GateError, preflight
from .base import ToolContext, status_result, text_result

logger = logging.getLogger(__name__)

GROUP = "scanner"


def tools(cfg) -> list[dict]:
    """Return the scanner tool definitions for this installation."""
    return [
        {
            "name": "project_context.scanner_report",
            "description": "Report-only scan of the project root: candidate counts "
            "by category, estimated bytes, sanitization-risk flags, git "
            "availability, and recommended next actions. Writes nothing.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_context.scanner_ingest",
            "description": "Ingest discovered candidates into the project DB. Requires "
            "scanner.enabled; returns not_permitted when the gate is off.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {"type": "string"},
                    "branch_fp": {"type": "string"},
                },
                "required": ["project_fp", "branch_fp"],
            },
        },
        {
            "name": "project_context.scanner_status",
            "description": "Report the scanner's current gate states (enabled, mode, "
            "pointer-replacement, source-mutation) and git availability.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_context.scanner_validate",
            "description": "Pre-flight validation: report which scanner modes are "
            "currently permitted for this installation. Writes nothing.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_context.scanner_plan_pointers",
            "description": "Build a reversible docstring->pointer rewrite plan. Requires "
            "scanner.pointer_replacement.enabled; never mutates source.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_fp": {"type": "string"},
                    "branch_fp": {"type": "string"},
                },
                "required": ["project_fp", "branch_fp"],
            },
        },
        {
            "name": "project_context.scanner_apply_pointers",
            "description": "Execute a rewrite plan, replacing docstrings with pointer "
            "stubs. Requires pointer-replacement + source-mutation gates "
            "AND mcp.allow_mutating_tools; returns not_permitted otherwise.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "project_fp": {"type": "string"},
                    "branch_fp": {"type": "string"},
                    "confirm": {"type": "boolean", "default": False},
                },
                "required": ["plan_id", "project_fp", "branch_fp", "confirm"],
            },
        },
    ]


def _report_summary(report: ScanReport) -> dict[str, Any]:
    """Build the compact-by-default report envelope from a ScanReport.

    Returns aggregate counts plus a small per-candidate index (path, category,
    span) rather than full candidate bodies, honoring compact-by-default. The
    caller can ingest to retrieve full content.
    """
    by_category = Counter(c.category for c in report.candidates)
    return {
        "mode": report.mode,
        "root": report.root,
        "candidate_count": len(report.candidates),
        "by_category": dict(by_category),
        "estimated_bytes": report.total_bytes(),
        "git_available": report.git_available,
        "recommended_actions": list(report.recommended_actions),
        "notes": list(report.notes),
        "candidates": [
            {
                "source_path": c.source_path,
                "category": c.category,
                "subtype": c.subtype,
                "est_bytes": c.est_bytes,
                "span": list(c.span) if c.span is not None else None,
                "risk_flags": list(c.risk_flags),
                "detector": c.detector,
            }
            for c in report.candidates
        ],
    }


def _git_available(root, cfg) -> bool:
    """Return whether git context is available for ``root`` (never raises)."""
    try:
        return git_context.collect(root, cfg) is not None
    except Exception:
        logger.debug("git_context.collect failed during scanner status", exc_info=True)
        return False


def _scanner_status(cfg, root) -> dict[str, Any]:
    """Summarize the scanner gate states for introspection tools."""
    repl = cfg.scanner.pointer_replacement
    return {
        "scanner_enabled": cfg.scanner.enabled,
        "scanner_mode": cfg.scanner.mode,
        "pointer_replacement_enabled": repl.enabled,
        "source_mutation_allowed": repl.allow_source_mutation,
        "mcp_mutating_tools": cfg.mcp.allow_mutating_tools,
        "git_available": _git_available(root, cfg),
    }


def _permitted_modes(cfg) -> dict[str, bool]:
    """Report which scanner modes are currently permitted by config gates."""
    repl = cfg.scanner.pointer_replacement
    return {
        "report": True,
        "ingest": cfg.scanner.enabled,
        "pointer_plan": cfg.scanner.enabled and repl.enabled,
        "pointer_apply": (
            cfg.scanner.enabled
            and repl.enabled
            and repl.allow_source_mutation
            and cfg.mcp.allow_mutating_tools
        ),
    }


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Route a scanner tool call to its scanner module, gated and compact."""
    cfg = ctx.cfg

    if name == "project_context.scanner_report":
        report = scanner_report.run(ctx.root, cfg)
        return text_result(_report_summary(report))

    if name == "project_context.scanner_status":
        return text_result(_scanner_status(cfg, ctx.root))

    if name == "project_context.scanner_validate":
        return text_result(
            {"permitted_modes": _permitted_modes(cfg), "status": _scanner_status(cfg, ctx.root)}
        )

    if name == "project_context.scanner_ingest":
        return _scanner_ingest(args, ctx)

    if name == "project_context.scanner_plan_pointers":
        if not cfg.scanner.pointer_replacement.enabled:
            return status_result(
                "not_permitted",
                reason="scanner.pointer_replacement.enabled must be true",
            )
        return _scanner_plan_pointers(args, ctx)

    if name == "project_context.scanner_apply_pointers":
        if not cfg.mcp.allow_mutating_tools:
            return status_result("not_permitted", reason="mcp.allow_mutating_tools must be true")
        return _scanner_apply_pointers(args, ctx)

    return status_result("unknown_tool", name=name)


# ── Writing modes ──────────────────────────────────────────────────────

_UNREGISTERED_ROOT = (
    "no project_fp given and the server root is not a registered project; "
    "call project_context.register_project first or pass project_fp"
)


def _target_project(
    args: dict[str, Any], ctx: ToolContext
) -> tuple[str, sqlite3.Connection] | dict[str, Any]:
    """Resolve the project a writing tool acts on: ``(project_fp, project_conn)``.

    An explicit ``project_fp`` must be registered; without one the server
    root's registered project is used. The pipeline always receives a real
    fingerprint -- never ``None``. Returns a status envelope when the
    project cannot be resolved.
    """
    explicit = args.get("project_fp")
    project_fp = str(explicit) if explicit else ctx.root_project_fp()
    if project_fp is None:
        return status_result("unknown_project", project_fp=None, reason=_UNREGISTERED_ROOT)
    project_conn = ctx.project_conn(project_fp)
    if project_conn is None:
        return status_result("unknown_project", project_fp=project_fp)
    return project_fp, project_conn


def _branch_arg(args: dict[str, Any]) -> str | None:
    value = args.get("branch_fp")
    return str(value) if value else None


def _scanner_ingest(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        # Gate first: a disabled scanner answers not_permitted without
        # resolving (or creating) any project database.
        preflight(schema.MODE_INGEST, ctx.cfg)
    except GateError as exc:
        return status_result("not_permitted", reason=str(exc))
    target = _target_project(args, ctx)
    if isinstance(target, dict):
        return target
    project_fp, project_conn = target
    branch_fp = _branch_arg(args)
    if branch_fp is None:
        return status_result("invalid_args", reason="branch_fp is required")
    try:
        stats = scanner_ingest.run(
            ctx.root,
            ctx.cfg,
            project_conn,
            ctx.registry_conn(),
            project_fp=project_fp,
            branch_fp=branch_fp,
        )
    except GateError as exc:
        return status_result("not_permitted", reason=str(exc))
    return text_result(stats)


def _scanner_plan_pointers(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _target_project(args, ctx)
    if isinstance(target, dict):
        return target
    project_fp, project_conn = target
    plan = scanner_pointer_plan.run(
        str(ctx.root),
        ctx.cfg,
        project_conn,
        project_fp=project_fp,
        branch_fp=_branch_arg(args),
    )
    return text_result(plan)


def _scanner_apply_pointers(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    plan_id = args.get("plan_id")
    if plan_id is None:
        return status_result("invalid_args", reason="plan_id is required")
    target = _target_project(args, ctx)
    if isinstance(target, dict):
        return target
    project_fp, project_conn = target
    # pointer_apply.run self-gates (validators.preflight + confirm) and
    # returns its own structured {"status": ...} result; it does not raise.
    result = scanner_pointer_apply.run(
        str(plan_id),
        str(ctx.root),
        ctx.cfg,
        project_conn,
        project_fp=project_fp,
        branch_fp=_branch_arg(args),
        confirm=bool(args.get("confirm", False)),
        registry_conn=ctx.registry_conn(),
    )
    return text_result(result)
