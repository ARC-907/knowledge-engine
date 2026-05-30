"""Git / lineage MCP tools for the project-docs subsystem.

This group exposes the optional git capability as compact-by-default MCP tools.
Every tool is conservative: git is off unless ``git.enabled`` is true, and the
diff-oriented tools additionally require ``git.include_diff_summaries``. A gated
capability never raises — it returns a structured ``{"status": "disabled"}``
envelope so an agent can discover the capability instead of hitting an error.

All git access flows through :mod:`knowledge_engine.project_docs.git_context`
(``subprocess`` argv lists, ``shell=False``) or the small argv helpers below,
which degrade gracefully when the ``git`` binary is missing.

Tools:

* ``project_docs.git_context`` — current branch / commit / dirty / remote hash.
* ``project_docs.search_by_commit`` — records ingested at a given commit.
* ``project_docs.get_branch_lineage`` — merge-base / ahead-behind vs a base ref.
* ``project_docs.get_change_context`` — files + numstat for a single commit.
* ``project_docs.explain_file_history`` — recent commits that touched a file.
* ``project_docs.search_by_diff`` — numstat summary between two refs.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from typing import Any

from .. import git_context as gitctx
from .. import search as pdsearch
from .base import ToolContext, status_result, text_result

GROUP = "git"

# Conservative timeout so a hung/locked repo can never stall a tool call.
_GIT_TIMEOUT = 10


def tools(cfg) -> list[dict]:
    """Return the MCP tool definitions for the git/lineage group."""
    return [
        {
            "name": "project_docs.git_context",
            "description": "Report the current git context for the project root "
                           "(branch, commit, dirty state, sanitized remote hash). "
                           "Returns status 'disabled' when git is off.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_docs.search_by_commit",
            "description": "List project-docs records ingested at a specific git "
                           "commit, newest first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "commit": {"type": "string", "description": "Full or short commit hash."},
                    "query": {"type": "string", "description": "Optional FTS query to narrow."},
                    "project_fp": {"type": "string"},
                    "branch_fp": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["commit"],
            },
        },
        {
            "name": "project_docs.get_branch_lineage",
            "description": "Describe the lineage of the current branch relative to "
                           "a base ref (merge-base, ahead/behind counts).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "default": "main"},
                },
            },
        },
        {
            "name": "project_docs.get_change_context",
            "description": "Summarize a single commit: subject, files changed, and "
                           "insertion/deletion counts (numstat).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "commit": {"type": "string", "default": "HEAD"},
                },
            },
        },
        {
            "name": "project_docs.explain_file_history",
            "description": "List recent commits that touched a given file path "
                           "(hash + subject), most recent first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["path"],
            },
        },
        {
            "name": "project_docs.search_by_diff",
            "description": "Summarize the diff between two refs (files changed, "
                           "insertions, deletions). Requires git diff summaries to "
                           "be enabled; otherwise returns status 'disabled'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from_ref": {"type": "string"},
                    "to_ref": {"type": "string", "default": "HEAD"},
                },
                "required": ["from_ref"],
            },
        },
    ]


def _run_git(root, args: list[str]) -> str | None:
    """Run ``git <args>`` in ``root`` (argv list, ``shell=False``).

    Returns stripped stdout, or ``None`` if the binary is missing, the command
    fails, or it times out. Never raises for the common failure modes.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            shell=False,
            timeout=_GIT_TIMEOUT,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Handle a git/lineage tool call, honoring config gates."""
    if name == "project_docs.git_context":
        return _git_context(args, ctx)
    if name == "project_docs.search_by_commit":
        return _search_by_commit(args, ctx)
    if name == "project_docs.get_branch_lineage":
        return _get_branch_lineage(args, ctx)
    if name == "project_docs.get_change_context":
        return _get_change_context(args, ctx)
    if name == "project_docs.explain_file_history":
        return _explain_file_history(args, ctx)
    if name == "project_docs.search_by_diff":
        return _search_by_diff(args, ctx)

    return status_result("unknown_tool", name=name)


def _git_context(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return current git context, or ``disabled`` when git is off."""
    if not ctx.cfg.git.enabled:
        return status_result("disabled", reason="git.enabled is false")
    gctx = gitctx.collect(ctx.root, ctx.cfg)
    if gctx is None:
        return status_result("not_configured", reason="not a git repository or git unavailable")
    return text_result(asdict(gctx))


def _search_by_commit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search records by ``git_commit`` filter; empty list on empty/unknown DB."""
    commit = args.get("commit")
    if not commit:
        return status_result("invalid_args", reason="'commit' is required")
    conn = ctx.project_conn(args.get("project_fp"))
    if conn is None:
        return text_result([])
    limit = int(args.get("limit", 10))
    branch_fp = args.get("branch_fp")
    query = (args.get("query") or "").strip()

    if query:
        rows = pdsearch.search(
            conn,
            query,
            limit=limit,
            branch_fp=branch_fp,
            git_commit=commit,
            mode="summary",
        )
        return text_result(rows)

    sql = (
        "SELECT record_id, pointer_id, project_fp, branch_fp, category, subtype, "
        "source_path, summary, git_commit, git_branch, created_at, updated_at "
        "FROM project_docs WHERE git_commit=?"
    )
    params: list[Any] = [commit]
    if branch_fp:
        sql += " AND branch_fp=?"
        params.append(branch_fp)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return text_result(rows)


def _get_branch_lineage(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Describe lineage of HEAD relative to a base ref."""
    if not ctx.cfg.git.enabled:
        return status_result("disabled", reason="git.enabled is false")
    if not gitctx._is_repo(ctx.root):
        return status_result("not_configured", reason="not a git repository or git unavailable")
    base = args.get("base") or "main"
    current = _run_git(ctx.root, ["rev-parse", "--abbrev-ref", "HEAD"])
    merge_base = _run_git(ctx.root, ["merge-base", "HEAD", base])
    counts = _run_git(ctx.root, ["rev-list", "--left-right", "--count", f"{base}...HEAD"])
    ahead: int | None = None
    behind: int | None = None
    if counts:
        parts = counts.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            behind, ahead = int(parts[0]), int(parts[1])
    return text_result(
        {
            "branch": current,
            "base": base,
            "merge_base": merge_base,
            "ahead": ahead,
            "behind": behind,
        }
    )


def _get_change_context(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Summarize a single commit (subject + numstat)."""
    if not ctx.cfg.git.enabled:
        return status_result("disabled", reason="git.enabled is false")
    if not gitctx._is_repo(ctx.root):
        return status_result("not_configured", reason="not a git repository or git unavailable")
    commit = args.get("commit") or "HEAD"
    subject = _run_git(ctx.root, ["log", "-1", "--format=%s", commit])
    numstat = _run_git(ctx.root, ["show", "--numstat", "--format=", commit])
    files: list[dict[str, Any]] = []
    insertions = 0
    deletions = 0
    for line in (numstat or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        add_str, del_str, path = parts[0], parts[1], parts[2]
        adds = int(add_str) if add_str.isdigit() else None
        dels = int(del_str) if del_str.isdigit() else None
        if adds:
            insertions += adds
        if dels:
            deletions += dels
        files.append({"path": path, "insertions": adds, "deletions": dels})
    return text_result(
        {
            "commit": commit,
            "subject": subject,
            "files_changed": len(files),
            "insertions": insertions,
            "deletions": deletions,
            "files": files,
        }
    )


def _explain_file_history(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List recent commits touching a file path."""
    if not ctx.cfg.git.enabled:
        return status_result("disabled", reason="git.enabled is false")
    if not gitctx._is_repo(ctx.root):
        return status_result("not_configured", reason="not a git repository or git unavailable")
    path = args.get("path")
    if not path:
        return status_result("invalid_args", reason="'path' is required")
    limit = int(args.get("limit", 10))
    out = _run_git(
        ctx.root,
        ["log", f"-{limit}", "--format=%H\t%s", "--", str(path)],
    )
    commits: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        commit_hash, _, subject = line.partition("\t")
        if commit_hash:
            commits.append({"commit": commit_hash, "subject": subject})
    return text_result({"path": path, "commits": commits, "count": len(commits)})


def _search_by_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Summarize the diff between two refs; gated on diff-summary capability."""
    if not ctx.cfg.git.enabled:
        return status_result("disabled", reason="git.enabled is false")
    if not ctx.cfg.git.include_diff_summaries:
        return status_result("disabled", reason="git.include_diff_summaries is false")
    from_ref = args.get("from_ref")
    if not from_ref:
        return status_result("invalid_args", reason="'from_ref' is required")
    to_ref = args.get("to_ref") or "HEAD"
    summary = gitctx.diff_summary(ctx.root, from_ref, to_ref)
    return text_result(asdict(summary))
