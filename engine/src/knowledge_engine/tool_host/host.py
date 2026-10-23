"""Knowledge-Engine — Tool/Script Hosting.

Manages a registry of addressable tools that agents can discover and invoke
via HTTP. Three tool kinds:

  script  — Run a command, capture stdout/stderr, return JSON result.
  service — Persistent process with its own HTTP endpoint (proxy pass-through).
  static  — Serve a file or directory.

Security posture (defensive defaults; every feature is still available):

* ``script`` commands are parsed with ``shlex`` and run as an argv list
  (no shell). The interpreter must be allowlisted, and the first argument must be
  a script file with an allowed extension that resolves under the tool's
  ``working_dir`` or ``PIPELINE_ROOT``. Inline-code flags (``-c``, ``-m``,
  ``-e``, ``--eval``, ``-``) and any other option before the script are refused
  unless the tool record sets ``allow_inline``.
* ``service`` upstreams must be ``http``/``https`` URLs with a host, both for
  proxying and for health checks. Only the tool's ``allowed_methods`` (default
  GET, POST) and ``allowed_headers`` (default content-type, accept) are
  forwarded. Hop-by-hop headers are always stripped, and ``Authorization`` is
  stripped unless explicitly allowed. Redirects may only go to http/https, and
  credentials are dropped when a redirect changes host.
* Every invocation is logged at INFO (tool id, interpreter/script or
  method + URL, outcome). Refusals are logged at WARNING.

Depends on `knowledge_engine.foundation.{config, db}`.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shlex
import subprocess
import uuid as _uuid
from collections.abc import Iterable, Mapping
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

from ..core.util.env import parse_bool_or, parse_list
from ..core.util.time import utc_now_iso
from ..runtime.foundation import config, db as db_mod

logger = logging.getLogger(__name__)


def _pipeline_root() -> Path:
    """The pipeline root: a module-level ``PIPELINE_ROOT`` override, else resolved now."""
    override = globals().get("PIPELINE_ROOT")
    return Path(override) if override is not None else config.pipeline_root()


def __getattr__(name: str) -> Any:
    # PEP 562: ``PIPELINE_ROOT`` used to be frozen at import; it is now
    # resolved on access (assigning the attribute still overrides it).
    if name == "PIPELINE_ROOT":
        return config.pipeline_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_VALID_KINDS = {"script", "service", "static"}

#: Interpreters a script tool may start, unless ``tool-host.yaml`` overrides it.
DEFAULT_ALLOWED_INTERPRETERS: tuple[str, ...] = ("python", "node", "bash")
#: Script file extensions accepted after the interpreter (``allowed_script_extensions``).
DEFAULT_SCRIPT_EXTENSIONS: tuple[str, ...] = (".py", ".js", ".mjs", ".sh")
#: Interpreter flags that run code without a script file.
_INLINE_CODE_FLAGS = frozenset({"-c", "-m", "-e", "--eval", "-p", "--print", "-"})

#: Proxy defaults applied when a tool record leaves the field unset (NULL).
DEFAULT_ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST")
DEFAULT_ALLOWED_HEADERS: tuple[str, ...] = ("content-type", "accept")
#: Headers that are never forwarded, even if a tool allowlists them.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_ALLOWED_UPSTREAM_SCHEMES = frozenset({"http", "https"})
_HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_URL_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f]")
_CREDENTIAL_HEADERS = ("Authorization", "Cookie", "Proxy-authorization")


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
    allow_inline: bool = False,
    allowed_methods: list[str] | None = None,
    allowed_headers: list[str] | None = None,
) -> dict[str, Any]:
    """Register a new hosted tool.

    ``allow_inline`` lets a script tool use inline-code interpreter flags
    (``python -c``, ``node -e`` ...). ``allowed_methods`` / ``allowed_headers``
    restrict what a service proxy forwards. ``None`` means the defaults
    :data:`DEFAULT_ALLOWED_METHODS` / :data:`DEFAULT_ALLOWED_HEADERS`.

    Raises:
        ValueError: invalid ``kind``, a non-http(s) ``upstream_url``, or a
            malformed method/header name.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Must be one of: {_VALID_KINDS}")
    if upstream_url:
        _require_valid_upstream(upstream_url)
    methods_json = _encode_token_list(allowed_methods, "allowed_methods", upper=True)
    headers_json = _encode_token_list(allowed_headers, "allowed_headers", upper=False)

    # Normalize route (strip leading/trailing slashes)
    route = route.strip("/")

    conn = db_mod.get_connection()
    tool_id = str(_uuid.uuid4())
    now = utc_now_iso()

    conn.execute(
        """INSERT INTO hosted_tools
           (tool_id, name, kind, description, route, input_schema, output_schema,
            command, working_dir, timeout_seconds, upstream_url, health_endpoint,
            local_path, node_id, tags, created_at, updated_at,
            allow_inline, allowed_methods, allowed_headers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tool_id,
            name,
            kind,
            description,
            route,
            json.dumps(input_schema or {}),
            json.dumps(output_schema or {}),
            command,
            working_dir,
            timeout_seconds,
            upstream_url,
            health_endpoint,
            local_path,
            node_id,
            json.dumps(tags or []),
            now,
            now,
            1 if allow_inline else 0,
            methods_json,
            headers_json,
        ),
    )
    conn.commit()
    db_mod.log_event("tool_registered", detail=f"{name} ({kind}) at /tools/{route}")
    return db_mod.require_row(get_tool(tool_id), "hosted tool", tool_id)


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
        "name",
        "description",
        "command",
        "working_dir",
        "timeout_seconds",
        "upstream_url",
        "health_endpoint",
        "local_path",
        "node_id",
        "enabled",
        "tags",
        "input_schema",
        "output_schema",
        "allow_inline",
        "allowed_methods",
        "allowed_headers",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_tool(tool_id)

    if updates.get("upstream_url"):
        _require_valid_upstream(str(updates["upstream_url"]))
    if "allow_inline" in updates:
        updates["allow_inline"] = 1 if _as_bool(updates["allow_inline"]) else 0
    if "allowed_methods" in updates:
        updates["allowed_methods"] = _encode_token_list(
            _decode_list(updates["allowed_methods"]), "allowed_methods", upper=True
        )
    if "allowed_headers" in updates:
        updates["allowed_headers"] = _encode_token_list(
            _decode_list(updates["allowed_headers"]), "allowed_headers", upper=False
        )

    for field in ("tags", "input_schema", "output_schema"):
        if field in updates and not isinstance(updates[field], str):
            updates[field] = json.dumps(updates[field])

    updates["updated_at"] = utc_now_iso()

    conn = db_mod.get_connection()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = [*updates.values(), tool_id]
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
        (utc_now_iso(), tool_id),
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
        (utc_now_iso(), tool_id),
    )
    conn.commit()


def invoke_script(
    tool: dict[str, Any],
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a script tool and capture output.

    Trust boundary: ``command`` comes from ``register_tool()`` / ``update_tool()``,
    which are library-level Python APIs — not HTTP input. The interpreter
    allowlist + timeout cap remain in place. The command is parsed with
    ``shlex.split`` and passed to ``subprocess.run`` as an argv list (no shell).
    Shell features (pipes, variable expansion, globs, command substitution) are
    not available; if a tool author needs them, they should write a wrapper
    script and register that.

    The whole argv is validated, not just the interpreter: the first argument
    must be a script file with an allowed extension (``allowed_script_extensions``
    in ``tool-host.yaml``, default ``.py .js .mjs .sh``) that resolves under the
    tool's ``working_dir`` or ``PIPELINE_ROOT``. Inline-code flags (``-c``,
    ``-m``, ``-e``, ``--eval``, ``-``) and other options before the script are
    refused unless the tool record sets ``allow_inline``. In that case only the
    interpreter allowlist applies.
    """
    cfg = _load_tool_host_config()
    max_timeout = cfg.get("max_script_timeout", 120)
    allowed_interpreters = cfg.get(
        "allowed_script_interpreters", list(DEFAULT_ALLOWED_INTERPRETERS)
    )
    tool_id = tool.get("tool_id")

    command = tool.get("command", "")
    if not command:
        return {"ok": False, "error": "No command configured for this tool"}

    try:
        argv = _split_command(command)
    except ValueError as exc:
        return {"ok": False, "error": f"Unparseable command string: {exc}"}
    if not argv:
        return {"ok": False, "error": "No command configured for this tool"}

    first_word = argv[0]
    if first_word not in allowed_interpreters:
        logger.warning("tool %s: interpreter %r denied (not allowlisted)", tool_id, first_word)
        return {"ok": False, "error": f"Interpreter '{first_word}' not in allowed list"}

    working_dir = tool.get("working_dir") or str(_pipeline_root())
    inline = _as_bool(tool.get("allow_inline"))
    if inline:
        script_label = "<inline mode>"
    else:
        extensions = cfg.get("allowed_script_extensions", list(DEFAULT_SCRIPT_EXTENSIONS))
        checked = _check_script_argv(argv, working_dir, extensions)
        if isinstance(checked, str):
            logger.warning("tool %s: %s invocation denied: %s", tool_id, first_word, checked)
            return {"ok": False, "error": checked}
        script_label = str(checked)

    timeout = min(tool.get("timeout_seconds", 30), max_timeout)

    stdin_data = json.dumps(input_data or {})

    logger.info("tool %s: running %s %s", tool_id, first_word, script_label)
    try:
        result = subprocess.run(  # noqa: S603 — argv list, no shell, allowlisted interpreter + contained script
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            input=stdin_data,
            check=False,  # returncode is reported to the caller below
        )
        _record_invocation(tool["tool_id"])

        stdout = result.stdout.strip()
        try:
            output = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            output = stdout

        logger.info(
            "tool %s: %s %s exited %d", tool_id, first_word, script_label, result.returncode
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": output,
            "stderr": result.stderr.strip() if result.stderr else None,
        }
    except subprocess.TimeoutExpired:
        logger.warning(
            "tool %s: %s %s timed out after %ss", tool_id, first_word, script_label, timeout
        )
        return {"ok": False, "error": f"Script timed out after {timeout}s"}
    except FileNotFoundError as e:
        logger.warning("tool %s: interpreter %r not found: %s", tool_id, first_word, e)
        return {"ok": False, "error": f"Interpreter not found on PATH: {e}"}
    except Exception as e:
        logger.warning("tool %s: %s %s failed: %s", tool_id, first_word, script_label, e)
        return {"ok": False, "error": str(e)}


def _split_command(command: str) -> list[str]:
    """Split a tool's command string into argv without a shell.

    POSIX quoting rules apply everywhere. On Windows the backslash is the
    path separator rather than an escape character, so escaping is disabled
    there and ``python C:\\tools\\job.py`` survives intact; quotes still
    group arguments on both platforms.
    """
    if os.name != "nt":
        return shlex.split(command, posix=True)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def _check_script_argv(
    argv: list[str], working_dir: str | Path, extensions: Iterable[str]
) -> Path | str:
    """Validate the script part of a script tool's argv.

    The first argument after the interpreter must be a script file with an
    allowed extension that resolves (following symlinks) under ``working_dir``
    or :data:`PIPELINE_ROOT`. Returns the resolved script path, or an error
    message when the argv is refused.
    """
    if len(argv) < 2:
        return "Script tools must name a script file after the interpreter"
    first_arg = argv[1]
    if first_arg in _INLINE_CODE_FLAGS:
        return (
            f"Inline code flag '{first_arg}' is not allowed; "
            "set allow_inline on the tool to permit it"
        )
    if first_arg.startswith("-"):
        return (
            f"Interpreter option '{first_arg}' before the script path is not allowed; "
            "set allow_inline on the tool to permit it"
        )

    base = Path(working_dir)
    try:
        script = (base / first_arg).resolve()
        roots = [base.resolve(), _pipeline_root().resolve()]
    except (OSError, RuntimeError, ValueError) as exc:
        return f"Script path '{first_arg}' cannot be resolved: {exc}"
    if not any(script.is_relative_to(root) for root in roots):
        return f"Script path '{first_arg}' resolves outside the tool's root"

    allowed_ext = {str(ext).lower() for ext in extensions}
    if script.suffix.lower() not in allowed_ext:
        return (
            f"Script '{first_arg}' must have one of the extensions: "
            f"{', '.join(sorted(allowed_ext))}"
        )
    if not script.is_file():
        return f"Script not found: {first_arg}"
    return script


def proxy_to_service(
    tool: dict[str, Any],
    method: str = "GET",
    subpath: str = "",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Forward a request to a service tool's upstream URL.

    Only methods in the tool's ``allowed_methods`` and headers in its
    ``allowed_headers`` are forwarded. Hop-by-hop headers are always dropped,
    and ``Authorization`` is dropped unless allowlisted. The upstream must be an
    http(s) URL with a host, and ``subpath`` may not leave it (no ``..``
    segments or control characters). Refusals return ``{"ok": False, "error"}``
    (with ``status`` 405 for a disallowed method) and never contact the upstream.
    """
    tool_id = tool.get("tool_id")
    upstream = tool.get("upstream_url") or ""
    if not upstream:
        return {"ok": False, "error": "No upstream_url configured"}

    upstream_error = _upstream_error(upstream)
    if upstream_error:
        logger.warning("tool %s: proxy refused: %s", tool_id, upstream_error)
        return {"ok": False, "error": upstream_error}

    verb = (method or "GET").upper()
    allowed_methods = {
        m.upper() for m in _tool_list(tool, "allowed_methods", DEFAULT_ALLOWED_METHODS)
    }
    if verb not in allowed_methods:
        logger.warning(
            "tool %s: proxy refused method %s (allowed: %s)",
            tool_id,
            verb,
            ", ".join(sorted(allowed_methods)),
        )
        return {"ok": False, "status": 405, "error": f"Method '{verb}' not allowed for this tool"}

    url = _join_upstream(upstream, subpath)
    if url is None:
        logger.warning("tool %s: proxy refused subpath %r", tool_id, subpath)
        return {"ok": False, "error": "Invalid subpath for this tool"}

    forward = _filter_headers(
        headers or {}, _tool_list(tool, "allowed_headers", DEFAULT_ALLOWED_HEADERS), tool_id
    )
    if body and not any(k.lower() == "content-type" for k in forward):
        forward["Content-Type"] = "application/json"

    _record_invocation(tool["tool_id"])
    shown_url = _redact_url(url)

    try:
        req = urllib_request.Request(url, data=body, method=verb)  # noqa: S310  # scheme validated as http(s) above
        for k, v in forward.items():
            req.add_header(k, v)

        with _urlopen(req, timeout=tool.get("timeout_seconds", 30)) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            logger.info("tool %s: proxy %s %s -> %s", tool_id, verb, shown_url, resp.status)
            try:
                return {"ok": True, "status": resp.status, "data": json.loads(resp_body)}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "data": resp_body}
    except urllib_error.HTTPError as e:
        logger.info("tool %s: proxy %s %s -> HTTP %s", tool_id, verb, shown_url, e.code)
        return {"ok": False, "status": e.code, "error": e.reason}
    except Exception as e:
        logger.warning("tool %s: proxy %s %s failed: %s", tool_id, verb, shown_url, e)
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
        logger.warning("tool %s: static path traversal refused: %r", tool.get("tool_id"), subpath)
        return {"ok": False, "error": "Path traversal not allowed"}

    if not target.exists():
        return {"ok": False, "error": f"Not found: {subpath}"}

    _record_invocation(tool["tool_id"])
    logger.info("tool %s: serving static %s", tool.get("tool_id"), target)

    if target.is_dir():
        entries = [
            {
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            }
            for child in sorted(target.iterdir())
        ]
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
    """Ping a service tool's health endpoint (http/https upstreams only)."""
    tool_id = tool.get("tool_id")
    upstream = tool.get("upstream_url") or ""
    health_ep = tool.get("health_endpoint") or "/health"
    if not upstream:
        return {"ok": False, "error": "No upstream_url"}

    upstream_error = _upstream_error(upstream)
    if upstream_error:
        logger.warning("tool %s: health check refused: %s", tool_id, upstream_error)
        return {"ok": False, "error": upstream_error}
    url = _join_upstream(upstream, health_ep)
    if url is None:
        logger.warning("tool %s: health check refused endpoint %r", tool_id, health_ep)
        return {"ok": False, "error": "Invalid health_endpoint for this tool"}

    try:
        req = urllib_request.Request(url, method="GET")  # noqa: S310  # scheme validated as http(s) above
        with _urlopen(req, timeout=5) as resp:
            logger.debug("tool %s: health %s -> %s", tool_id, _redact_url(url), resp.status)
            return {"ok": True, "status": resp.status}
    except Exception as e:
        logger.info("tool %s: health %s failed: %s", tool_id, _redact_url(url), e)
        return {"ok": False, "error": str(e)}


def health_check_all_services() -> list[dict[str, Any]]:
    """Check health of all service-kind tools."""
    tools = list_tools(kind="service")
    results = []
    for t in tools:
        check = health_check_service(t)
        results.append(
            {
                "tool_id": t["tool_id"],
                "name": t["name"],
                "route": t["route"],
                **check,
            }
        )
    return results


# ── Validation helpers ─────────────────────────────────────────


def _as_bool(value: Any) -> bool:
    """Interpret a DB/config flag (``1``, ``True``, ``"true"`` ...) as a bool."""
    if isinstance(value, str):
        return parse_bool_or(value, False)
    return bool(value)


def _decode_list(value: Any) -> Any:
    """Decode a JSON-encoded list if ``value`` is a string; pass others through."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return parse_list(value)
    return value


def _encode_token_list(values: Any, field: str, *, upper: bool) -> str | None:
    """Validate an HTTP method/header-name list and return it JSON-encoded.

    ``None`` stays ``None`` (use the host defaults). Raises ``ValueError`` for
    anything that is not a list of HTTP tokens.
    """
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ValueError(f"{field} must be a list of strings")
    out: list[str] = []
    for item in values:
        if not isinstance(item, str) or not _HTTP_TOKEN_RE.fullmatch(item):
            raise ValueError(f"{field} entry {item!r} is not a valid HTTP token")
        token = item.upper() if upper else item.lower()
        if token not in out:
            out.append(token)
    return json.dumps(out)


def _tool_list(tool: Mapping[str, Any], field: str, default: tuple[str, ...]) -> list[str]:
    """Return a tool's method/header list, falling back to ``default`` when unset."""
    value = _decode_list(tool.get(field))
    if value is None:
        return list(default)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    logger.warning(
        "tool %s: malformed %s %r; using defaults %s", tool.get("tool_id"), field, value, default
    )
    return list(default)


def _upstream_error(url: str) -> str | None:
    """Return why ``url`` is not an acceptable upstream, or ``None`` if it is."""
    if _URL_FORBIDDEN_RE.search(url):
        return "upstream_url must not contain whitespace or control characters"
    try:
        parts = urllib_parse.urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        return f"upstream_url is not a valid URL: {exc}"
    if parts.scheme.lower() not in _ALLOWED_UPSTREAM_SCHEMES:
        return f"upstream_url scheme must be http or https, got {parts.scheme or 'none'!r}"
    if not host:
        return "upstream_url must include a host"
    return None


def _require_valid_upstream(url: str) -> None:
    """Raise ``ValueError`` if ``url`` is not an acceptable upstream."""
    error = _upstream_error(url)
    if error:
        raise ValueError(error)


def _join_upstream(upstream: str, subpath: str | None) -> str | None:
    """Append ``subpath`` to ``upstream``; ``None`` if it would leave the upstream.

    Rejects control characters/whitespace and ``.``/``..`` path segments (also
    percent-encoded), and re-checks that scheme and host are unchanged.
    """
    url = upstream.rstrip("/")
    if subpath:
        if _URL_FORBIDDEN_RE.search(subpath):
            return None
        path_part = subpath.split("?", 1)[0].split("#", 1)[0]
        segments = urllib_parse.unquote(path_part).replace("\\", "/").split("/")
        if any(seg in {".", ".."} for seg in segments):
            return None
        url += "/" + subpath.lstrip("/")
    try:
        before = urllib_parse.urlsplit(upstream)
        after = urllib_parse.urlsplit(url)
    except ValueError:
        return None
    if (before.scheme.lower(), before.netloc.lower()) != (
        after.scheme.lower(),
        after.netloc.lower(),
    ):
        return None
    return url


def _filter_headers(
    headers: Mapping[str, str], allowed: Iterable[str], tool_id: Any
) -> dict[str, str]:
    """Keep only allowlisted, non-hop-by-hop headers with clean values."""
    allowed_lc = {h.lower() for h in allowed}
    named_by_connection = {
        token.strip().lower()
        for key, value in headers.items()
        if key.lower() == "connection"
        for token in str(value).split(",")
    }
    forward: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in headers.items():
        name = key.lower()
        if (
            name in _HOP_BY_HOP_HEADERS
            or name in named_by_connection
            or name not in allowed_lc
            or not _HTTP_TOKEN_RE.fullmatch(key)
        ):
            dropped.append(name)
            continue
        text = str(value)
        if any(ch in text for ch in "\r\n\x00"):
            logger.warning("tool %s: dropping header %s with CR/LF/NUL in value", tool_id, key)
            continue
        forward[key] = text
    if dropped:
        logger.debug("tool %s: proxy dropped headers %s", tool_id, sorted(set(dropped)))
    return forward


def _redact_url(url: str) -> str:
    """Strip userinfo and query from ``url`` for logging."""
    try:
        parts = urllib_parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<unparseable url>"
    netloc = f"{host}:{port}" if port else host
    return urllib_parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class _SafeRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Follow redirects only to http/https, dropping credentials across hosts."""

    def redirect_request(
        self,
        req: urllib_request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib_request.Request | None:
        error = _upstream_error(newurl)
        if error:
            raise urllib_error.HTTPError(
                req.full_url, code, f"refused redirect: {error}", headers, fp
            )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib_parse.urlsplit(req.full_url).netloc.lower()
            if urllib_parse.urlsplit(newurl).netloc.lower() != old_host:
                for name in _CREDENTIAL_HEADERS:
                    new.remove_header(name)
        return new


def _urlopen(req: urllib_request.Request, timeout: float) -> Any:
    """Open ``req`` with the redirect-restricting opener (patched in tests)."""
    opener = urllib_request.build_opener(_SafeRedirectHandler)
    return opener.open(req, timeout=timeout)
