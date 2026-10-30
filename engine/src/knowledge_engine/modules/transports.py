"""Module transports: script (via the tool host's bounded runner), service, external MCP.

Each transport takes a :class:`CallRequest` and returns the module's raw
response (``{"output": ..., "artifacts": [...]}``) or raises a typed
:class:`~knowledge_engine.knowledge.errors.KnowledgeError`. No transport
accepts a caller-supplied endpoint: scripts run the manifest argv inside the
module directory, services and HTTP MCP servers use the URL from the
operator's configuration.

Script wire protocol v1: the host writes one JSON request to stdin
(``protocol``, ``module_id``, ``module_version``, ``tool``, ``run_id``,
``input``, ``settings``, ``scope``, ``artifacts`` -- approved input artifacts
with a read-only file ``path``, ``now``) and reads one JSON object from
stdout: ``{"output": ..., "artifacts": [...]}`` on success, or
``{"error": {"code", "message"}}``. Secrets arrive as environment variables
named by the manifest's secret references; nothing else of the host
environment is inherited except ``PATH``, locale and temp-dir variables;
``KNOWLEDGE_ENGINE_MODULE_ID`` and ``KNOWLEDGE_ENGINE_RUN_ID`` identify the call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urllib_error, request as urllib_request

from ..knowledge.contracts import RecordScope
from ..knowledge.errors import (
    ERRORS_BY_CODE,
    CancelledError,
    DeniedError,
    InternalError,
    InvalidInputError,
    KnowledgeError,
    KnowledgeTimeoutError,
    OutputInvalidError,
    UnavailableError,
)
from ..tool_host import host as tool_host
from .manifest import MODULE_PROTOCOL_VERSION, McpTransport, ModuleManifest, ToolSpec

_logger = logging.getLogger(__name__)

#: Host environment variables a module process inherits (besides its secrets).
INHERITED_ENV = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE")
#: Codes a module may report for its own failures.
MODULE_ERROR_CODES = frozenset(
    {"invalid_input", "denied", "unavailable", "timeout", "output_invalid", "internal_error"}
)
#: How often an MCP call checks for cancellation.
_MCP_CANCEL_POLL_S = 0.1


@dataclass
class CallRequest:
    """Everything a transport needs for one attempt."""

    manifest: ModuleManifest
    tool: ToolSpec
    source_dir: Path
    arguments: dict[str, Any]
    settings: dict[str, Any]
    secrets: dict[str, str]
    run_id: str
    scope: RecordScope
    timeout_s: float
    now: str
    input_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    should_cancel: Callable[[], bool] = lambda: False


def resolve_argv(command: tuple[str, ...], source_dir: Path) -> list[str]:
    """Validate a module argv with the tool host's rules; ``python`` runs this engine's interpreter.

    Raises:
        DeniedError: the interpreter is not allowlisted or the argv is refused.
        UnavailableError: the module directory or script is missing.
    """
    if not source_dir.is_dir():
        raise UnavailableError("the module directory is missing")
    try:
        argv = tool_host.resolve_script_argv(command, source_dir)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Script not found"):
            raise UnavailableError("the module script is missing") from exc
        raise DeniedError(f"module command refused: {message}") from exc
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    return argv


def child_env(secret_env: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    env.update(secret_env)
    return env


def _request_body(req: CallRequest) -> dict[str, Any]:
    return {
        "protocol": MODULE_PROTOCOL_VERSION,
        "module_id": req.manifest.module_id,
        "module_version": req.manifest.version,
        "tool": req.tool.name,
        "run_id": req.run_id,
        "input": req.arguments,
        "settings": req.settings,
        "scope": req.scope.model_dump(mode="json"),
        "artifacts": req.input_artifacts,
        "now": req.now,
    }


def module_error(payload: Any) -> KnowledgeError:
    """A typed error from a module's ``{"code", "message"}`` (unknown codes -> internal)."""
    if not isinstance(payload, Mapping):
        return InternalError("the module reported a malformed error")
    code = str(payload.get("code", "internal_error"))
    message = str(payload.get("message", "module error"))[:1000]
    cls = ERRORS_BY_CODE.get(code) if code in MODULE_ERROR_CODES else None
    return (cls or InternalError)(f"module error: {message}", details={"module_code": code})


def parse_response(stdout: bytes) -> dict[str, Any]:
    """Parse a module's stdout. Raises typed errors for module errors and garbage."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise OutputInvalidError("the module produced no output")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise OutputInvalidError("the module output is not JSON") from exc
    if not isinstance(data, dict):
        raise OutputInvalidError("the module output must be a JSON object")
    if "error" in data:
        raise module_error(data["error"])
    if "output" not in data:
        raise OutputInvalidError("the module output has no 'output' field")
    return data


# ── script ───────────────────────────────────────────────────────────


def _secret_env(manifest: ModuleManifest, secrets: Mapping[str, str]) -> dict[str, str]:
    by_name = {s.name: s.env for s in manifest.configuration.secrets}
    return {by_name[name]: value for name, value in secrets.items() if name in by_name}


def run_script(req: CallRequest) -> dict[str, Any]:
    """One script call through :func:`knowledge_engine.tool_host.host.run_bounded`."""
    command = getattr(req.manifest.transport, "command", None)
    if not command:
        raise InternalError("script transport without a command")
    argv = resolve_argv(tuple(command), req.source_dir)
    stdin = json.dumps(_request_body(req), default=str).encode("utf-8")
    env = child_env(
        _secret_env(req.manifest, req.secrets),
        {
            "KNOWLEDGE_ENGINE_MODULE_ID": req.manifest.module_id,
            "KNOWLEDGE_ENGINE_RUN_ID": req.run_id,
        },
    )
    try:
        outcome = tool_host.run_bounded(
            argv,
            cwd=req.source_dir,
            stdin=stdin,
            env=env,
            timeout_s=req.timeout_s,
            max_stdout_bytes=req.manifest.limits.max_output_bytes,
            should_cancel=req.should_cancel,
        )
    except OSError as exc:
        raise UnavailableError("the module process could not be started") from exc
    if outcome.cancelled:
        raise CancelledError("the call was cancelled; the module process group was stopped")
    if outcome.timed_out:
        raise KnowledgeTimeoutError(
            f"the module did not finish within {req.timeout_s:g}s; its process group was stopped"
        )
    if outcome.output_exceeded:
        raise OutputInvalidError(
            f"the module output exceeds {req.manifest.limits.max_output_bytes} bytes"
        )
    if outcome.stderr:
        _logger.debug(
            "module %s stderr (%d bytes, not forwarded)",
            req.manifest.module_id,
            len(outcome.stderr),
        )
    if outcome.returncode != 0:
        try:
            return parse_response(outcome.stdout)
        except KnowledgeError as exc:
            if isinstance(exc, OutputInvalidError):
                raise UnavailableError(
                    f"the module process exited with status {outcome.returncode}",
                    details={"returncode": outcome.returncode},
                ) from exc
            raise
    return parse_response(outcome.stdout)


def probe_script(manifest: ModuleManifest, source_dir: Path, secrets: Mapping[str, str]) -> None:
    """Run the manifest's health command (bounded). Raises a typed error when unhealthy."""
    if manifest.health is None or manifest.health.command is None:
        return
    argv = resolve_argv(tuple(manifest.health.command), source_dir)
    try:
        outcome = tool_host.run_bounded(
            argv,
            cwd=source_dir,
            stdin=b"{}",
            env=child_env(
                _secret_env(manifest, secrets), {"KNOWLEDGE_ENGINE_MODULE_ID": manifest.module_id}
            ),
            timeout_s=manifest.health.timeout_ms / 1000,
            max_stdout_bytes=65536,
        )
    except OSError as exc:
        raise UnavailableError("the health probe could not be started") from exc
    if outcome.timed_out:
        raise KnowledgeTimeoutError("the health probe timed out")
    if outcome.returncode != 0:
        raise UnavailableError(f"the health probe exited with status {outcome.returncode}")


# ── service ──────────────────────────────────────────────────────────


def service_url(manifest: ModuleManifest, settings: Mapping[str, Any], path: str) -> str:
    """The approved URL for ``path`` (from the operator setting). Raises ``DeniedError``."""
    setting = getattr(manifest.transport, "url_setting", None)
    base = settings.get(setting or "")
    if not isinstance(base, str) or not base:
        raise DeniedError("no approved URL is configured for this module")
    error = tool_host._upstream_error(base)
    if error:
        raise DeniedError(f"the configured module URL is refused: {error}")
    url = tool_host._join_upstream(base, path)
    if url is None:
        raise DeniedError("the module path leaves the configured URL")
    return url


def _secret_headers(manifest: ModuleManifest, secrets: Mapping[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for ref in manifest.configuration.secrets:
        if ref.header and ref.name in secrets:
            headers[ref.header] = ref.header_prefix + secrets[ref.name]
    return headers


def run_service(req: CallRequest) -> dict[str, Any]:
    """POST the request to the configured service and parse its JSON answer."""
    url = service_url(req.manifest, req.settings, req.tool.path or f"/{req.tool.name}")
    body = json.dumps(_request_body(req), default=str).encode("utf-8")
    http_req = urllib_request.Request(url, data=body, method="POST")  # noqa: S310 - http(s) checked
    http_req.add_header("Content-Type", "application/json")
    http_req.add_header("Accept", "application/json")
    for name, value in _secret_headers(req.manifest, req.secrets).items():
        http_req.add_header(name, value)
    limit = req.manifest.limits.max_output_bytes
    try:
        with tool_host._urlopen(http_req, timeout=req.timeout_s) as resp:
            data = resp.read(limit + 1)
    except urllib_error.HTTPError as exc:
        status = exc.code
        if status in (400, 422):
            raise InvalidInputError(
                f"the module service rejected the input (HTTP {status})"
            ) from exc
        if status in (401, 403):
            raise DeniedError(f"the module service refused the call (HTTP {status})") from exc
        if status == 504:
            raise KnowledgeTimeoutError("the module service timed out (HTTP 504)") from exc
        raise UnavailableError(f"the module service answered HTTP {status}") from exc
    except TimeoutError as exc:
        raise KnowledgeTimeoutError("the module service did not answer in time") from exc
    except (urllib_error.URLError, OSError) as exc:
        raise UnavailableError("the module service is unreachable") from exc
    if len(data) > limit:
        raise OutputInvalidError(f"the module output exceeds {limit} bytes")
    return parse_response(data)


def probe_service(manifest: ModuleManifest, settings: Mapping[str, Any]) -> None:
    if manifest.health is None or manifest.health.path is None:
        return
    url = service_url(manifest, settings, manifest.health.path)
    http_req = urllib_request.Request(url, method="GET")  # noqa: S310 - http(s) checked
    try:
        with tool_host._urlopen(http_req, timeout=manifest.health.timeout_ms / 1000) as resp:
            if resp.status >= 400:
                raise UnavailableError(f"the health probe answered HTTP {resp.status}")
    except urllib_error.HTTPError as exc:
        raise UnavailableError(f"the health probe answered HTTP {exc.code}") from exc
    except (urllib_error.URLError, OSError) as exc:
        raise UnavailableError("the module service is unreachable") from exc


# ── external MCP ─────────────────────────────────────────────────────


def _mcp_target(
    req_manifest: ModuleManifest,
    source_dir: Path,
    settings: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> Any:
    transport = req_manifest.transport
    if not isinstance(transport, McpTransport):
        raise InternalError("not an MCP transport")
    if transport.connection == "stdio":
        from mcp.client.stdio import StdioServerParameters  # noqa: PLC0415 - optional SDK use

        argv = resolve_argv(tuple(transport.command or ()), source_dir)
        return StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=child_env(
                _secret_env(req_manifest, secrets),
                {"KNOWLEDGE_ENGINE_MODULE_ID": req_manifest.module_id},
            ),
            cwd=str(source_dir),
        )
    return service_url(req_manifest, settings, "")


def _mcp_output(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return {"output": structured}
    texts = [getattr(item, "text", None) for item in getattr(result, "content", [])]
    text = next((t for t in texts if isinstance(t, str)), None)
    if text is None:
        raise OutputInvalidError("the upstream MCP tool returned no text or structured content")
    try:
        return {"output": json.loads(text)}
    except ValueError:
        return {"output": {"text": text}}


def _run_mcp(
    target: Any,
    action: Callable[[Any], Any],
    timeout_s: float,
    should_cancel: Callable[[], bool],
) -> Any:
    import anyio  # noqa: PLC0415 - optional SDK use
    from mcp import Client  # noqa: PLC0415

    cancelled = threading.Event()
    box: list[Any] = []

    async def main() -> None:
        with anyio.fail_after(timeout_s):
            async with anyio.create_task_group() as group:

                async def watch() -> None:
                    while True:
                        await anyio.sleep(_MCP_CANCEL_POLL_S)
                        if should_cancel():
                            cancelled.set()
                            group.cancel_scope.cancel()
                            return

                group.start_soon(watch)
                async with Client(target, read_timeout_seconds=timeout_s) as client:
                    box.append(await action(client))
                group.cancel_scope.cancel()

    try:
        anyio.run(main)
    except Exception as exc:
        leaf = _first_leaf(exc)
        if isinstance(leaf, TimeoutError):
            raise KnowledgeTimeoutError(
                f"the MCP module did not answer within {timeout_s:g}s"
            ) from exc
        if isinstance(leaf, KnowledgeError):
            raise leaf from exc
        _logger.info("MCP module call failed: %s", type(leaf).__name__)
        raise UnavailableError(f"the MCP module is unavailable ({type(leaf).__name__})") from exc
    if cancelled.is_set() or not box:
        raise CancelledError("the call was cancelled")
    return box[0]


def _first_leaf(exc: BaseException) -> BaseException:
    """The first non-group exception inside (possibly nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def run_mcp(req: CallRequest) -> dict[str, Any]:
    """Call ``remote_name`` (default: the tool name) on the configured external MCP server."""
    target = _mcp_target(req.manifest, req.source_dir, req.settings, req.secrets)
    name = req.tool.remote_name or req.tool.name

    async def action(client: Any) -> Any:
        return await client.call_tool(name, req.arguments)

    result = _run_mcp(target, action, req.timeout_s, req.should_cancel)
    if getattr(result, "is_error", False):
        raise InternalError("the upstream MCP tool reported an error")
    return _mcp_output(result)


def probe_mcp(
    manifest: ModuleManifest,
    source_dir: Path,
    settings: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> None:
    timeout = manifest.health.timeout_ms / 1000 if manifest.health else 5.0
    target = _mcp_target(manifest, source_dir, settings, secrets)

    async def action(client: Any) -> Any:
        return await client.list_tools()

    _run_mcp(target, action, timeout, lambda: False)


def run_transport(req: CallRequest) -> dict[str, Any]:
    kind = req.manifest.transport.kind
    if kind == "script":
        return run_script(req)
    if kind == "service":
        return run_service(req)
    return run_mcp(req)


def probe(
    manifest: ModuleManifest,
    source_dir: Path,
    settings: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> None:
    """Run the module's health probe for its transport (no-op without one, except MCP)."""
    kind = manifest.transport.kind
    if kind == "script":
        probe_script(manifest, source_dir, secrets)
    elif kind == "service":
        probe_service(manifest, settings)
    else:
        probe_mcp(manifest, source_dir, settings, secrets)


__all__ = [
    "INHERITED_ENV",
    "CallRequest",
    "child_env",
    "module_error",
    "parse_response",
    "probe",
    "resolve_argv",
    "run_transport",
]
