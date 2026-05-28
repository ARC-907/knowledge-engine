"""Knowledge-Engine — Tool/Script Hosting.

Manages a registry of addressable tools that agents can discover and invoke
via HTTP. Three tool kinds:

  script  — Run a command, capture stdout/stderr, return JSON result.
  service — Persistent process with its own HTTP endpoint (proxy pass-through).
  static  — Serve a file or directory.

Depends on `knowledge_engine.foundation.{config, db}`.
"""

from __future__ import annotations

import json
import mimetypes
import subprocess
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from ..foundation import config
from ..foundation import db as db_mod

PIPELINE_ROOT = config.PIPELINE_ROOT

_VALID_KINDS = {"script", "service", "static"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_tool_host_config() -> dict[str, Any]:
    """Load tool-host.yaml config, with safe defaults."""
    try:
        return config._load_yaml("tool-host.yaml").get("tool_host", {})
    except FileNotFoundError:
        return {}


# ── CRUD ───────────────────────────────────────────────────────


def register_tool(
    name: str,
    kind: str,
    route: str,
    description: str = "",
    command: str | None = None,
    working_dir: str | None = None,
    timeout_seconds: int = 30,
    upstream_url: str | None = None,
    health_endpoint: str | None = None,
    local_path: str | None = None,
    node_id: str | None = None,
    tags: list[str] | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
) -> dict[str, Any]:
    """Register a new hosted tool."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Must be one of: {_VALID_KINDS}")

    # Normalize route (strip leading/trailing slashes)
    route = route.strip("/")

    conn = db_mod.get_connection()
    tool_id = str(_uuid.uuid4())
    now = _now_iso()

    conn.execute(
        """INSERT INTO hosted_tools
           (tool_id, name, kind, description, route, input_schema, output_schema,
            command, working_dir, timeout_seconds, upstream_url, health_endpoint,
            local_path, node_id, tags, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tool_id, name, kind, description, route,
            json.dumps(input_schema or {}),
            json.dumps(output_schema or {}),
            command, working_dir, timeout_seconds,
            upstream_url, health_endpoint, local_path,
            node_id, json.dumps(tags or []),
            now, now,
        ),
    )
    conn.commit()
    db_mod.log_event("tool_registered", detail=f"{name} ({kind}) at /tools/{route}")
    return get_tool(tool_id)  # type: ignore[return-value]


def get_tool(tool_id_or_name: str) -> dict[str, Any] | None:
    """Look up a tool by ID or name."""
    conn = db_mod.get_connection()
    row = conn.execute(
        "SELECT * FROM hosted_tools WHERE tool_id = ? OR name = ?",
        (tool_id_or_name, tool_id_or_name),
    ).fetchone()
    return db_mod.dict_from_row(row)


def get_tool_by_route(route: str) -> dict[str, Any] | None:
    """Look up a tool by its route path."""
    route = route.strip("/")
    conn = db_mod.get_connection()
    row = conn.execute(
        "SELECT * FROM hosted_tools WHERE route = ? AND enabled = 1",
        (route,),
    ).fetchone()
    return db_mod.dict_from_row(row)


def list_tools(
    kind: str | None = None,
    tag: str | None = None,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    """List registered tools, optionally filtered."""
    conn = db_mod.get_connection()
    sql = "SELECT * FROM hosted_tools"
    conditions: list[str] = []
    params: list[Any] = []

    if enabled_only:
        conditions.append("enabled = 1")
    if kind:
        conditions.append("kind = ?")
        params.append(kind)

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY name"

    rows = conn.execute(sql, params).fetchall()
    tools = db_mod.rows_to_dicts(rows)

    if tag:
        tools = [t for t in tools if tag in t.get("tags", [])]

    return tools


def update_tool(tool_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update tool fields."""
    allowed = {
        "name", "description", "command", "working_dir", "timeout_seconds",
        "upstream_url", "health_endpoint", "local_path", "node_id",
        "enabled", "tags", "input_schema", "output_schema",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_tool(tool_id)

    for field in ("tags", "input_schema", "output_schema"):
        if field in updates and not isinstance(updates[field], str):
            updates[field] = json.dumps(updates[field])

    updates["updated_at"] = _now_iso()

    conn = db_mod.get_connection()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [tool_id]
    conn.execute(f"UPDATE hosted_tools SET {set_clause} WHERE tool_id = ?", values)
    conn.commit()
    return get_tool(tool_id)


def delete_tool(tool_id: str) -> bool:
    """Delete a tool from the registry."""
    conn = db_mod.get_connection()
    cursor = conn.execute("DELETE FROM hosted_tools WHERE tool_id = ?", (tool_id,))
    conn.commit()
    return cursor.rowcount > 0


def toggle_tool(tool_id: str) -> dict[str, Any] | None:
    """Toggle a tool's enabled state."""
    conn = db_mod.get_connection()
    conn.execute(
        "UPDATE hosted_tools SET enabled = 1 - enabled, updated_at = ? WHERE tool_id = ?",
        (_now_iso(), tool_id),
    )
    conn.commit()
    return get_tool(tool_id)


# ── Invocation ─────────────────────────────────────────────────


def _record_invocation(tool_id: str) -> None:
    """Bump invocation counter and timestamp."""
    conn = db_mod.get_connection()
    conn.execute(
        """UPDATE hosted_tools
           SET invocation_count = invocation_count + 1,
               last_invoked_at = ?
           WHERE tool_id = ?""",
        (_now_iso(), tool_id),
    )
    conn.commit()


def invoke_script(
    tool: dict[str, Any],
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a script tool and capture output."""
    cfg = _load_tool_host_config()
    max_timeout = cfg.get("max_script_timeout", 120)
    allowed_interpreters = cfg.get("allowed_script_interpreters", ["python", "node", "bash"])

    command = tool.get("command", "")
    if not command:
        return {"ok": False, "error": "No command configured for this tool"}

    first_word = command.split()[0] if command else ""
    if first_word not in allowed_interpreters:
        return {"ok": False, "error": f"Interpreter '{first_word}' not in allowed list"}

    timeout = min(tool.get("timeout_seconds", 30), max_timeout)
    working_dir = tool.get("working_dir") or str(PIPELINE_ROOT)

    stdin_data = json.dumps(input_data or {})

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            input=stdin_data,
        )
        _record_invocation(tool["tool_id"])

        stdout = result.stdout.strip()
        try:
            output = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            output = stdout

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": output,
            "stderr": result.stderr.strip() if result.stderr else None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Script timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def proxy_to_service(
    tool: dict[str, Any],
    method: str = "GET",
    subpath: str = "",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Forward a request to a service tool's upstream URL."""
    upstream = tool.get("upstream_url", "")
    if not upstream:
        return {"ok": False, "error": "No upstream_url configured"}

    url = upstream.rstrip("/")
    if subpath:
        url += "/" + subpath.lstrip("/")

    _record_invocation(tool["tool_id"])

    try:
        req = urllib_request.Request(url, data=body, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if body and "Content-Type" not in (headers or {}):
            req.add_header("Content-Type", "application/json")

        with urllib_request.urlopen(req, timeout=tool.get("timeout_seconds", 30)) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            try:
                return {"ok": True, "status": resp.status, "data": json.loads(resp_body)}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "data": resp_body}
    except urllib_error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.reason}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def serve_static(
    tool: dict[str, Any],
    subpath: str = "",
) -> dict[str, Any]:
    """Serve a file from a static tool's local_path."""
    local_path = tool.get("local_path", "")
    if not local_path:
        return {"ok": False, "error": "No local_path configured"}

    base = Path(local_path)
    if not base.exists():
        return {"ok": False, "error": f"Path not found: {local_path}"}

    target = (base / subpath) if subpath else base

    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return {"ok": False, "error": "Path traversal not allowed"}

    if not target.exists():
        return {"ok": False, "error": f"Not found: {subpath}"}

    _record_invocation(tool["tool_id"])

    if target.is_dir():
        entries = []
        for child in sorted(target.iterdir()):
            entries.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            })
        return {"ok": True, "type": "directory", "entries": entries}
    else:
        mime, _ = mimetypes.guess_type(str(target))
        try:
            content = target.read_text(encoding="utf-8")
            return {"ok": True, "type": "file", "mime": mime, "content": content}
        except UnicodeDecodeError:
            return {
                "ok": True,
                "type": "file",
                "mime": mime,
                "size": target.stat().st_size,
                "binary": True,
            }


# ── Health Checks ──────────────────────────────────────────────


def health_check_service(tool: dict[str, Any]) -> dict[str, Any]:
    """Ping a service tool's health endpoint."""
    upstream = tool.get("upstream_url", "")
    health_ep = tool.get("health_endpoint", "/health")
    if not upstream:
        return {"ok": False, "error": "No upstream_url"}

    url = upstream.rstrip("/") + health_ep
    try:
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            return {"ok": True, "status": resp.status}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def health_check_all_services() -> list[dict[str, Any]]:
    """Check health of all service-kind tools."""
    tools = list_tools(kind="service")
    results = []
    for t in tools:
        check = health_check_service(t)
        results.append({
            "tool_id": t["tool_id"],
            "name": t["name"],
            "route": t["route"],
            **check,
        })
    return results
