"""Register legacy JSON-schema MCP tool groups on an SDK ``MCPServer``.

The project-context and agent-board subsystems describe their MCP tools as
plain ``{"name", "description", "inputSchema"}`` dicts plus a
``name -> dispatch(name, args, ctx)`` map (see ``project_context.mcp_tools`` and
``agent_board.mcp_tools``). :func:`register_group` mounts each pair on an
:class:`~mcp.server.mcpserver.MCPServer` unchanged — same name, description and
``inputSchema`` — so clients see exactly the tool surface they saw before the
SDK migration.

Arguments are validated against the advertised JSON schema before the dispatch
function runs, and every failure becomes an ``isError`` tool result rather than
a protocol error or a dead server process (see :func:`tool_errors`).
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from jsonschema import exceptions as jsonschema_exceptions, validators as jsonschema_validators
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import ToolManager
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult
from pydantic import ValidationError

logger = logging.getLogger(__name__)

DispatchFn = Callable[[str, dict[str, Any], Any], Any]
"""``dispatch(name, arguments, context) -> {"content": [...]}`` (an MCP result envelope)."""


@contextmanager
def tool_errors(tool: str) -> Iterator[None]:
    """Turn exceptions raised while running ``tool`` into model-visible ``ToolError``s.

    ``KeyError``/``ValueError``/``TypeError`` are anticipated failures (bad or
    missing input) and keep their message. Anything else is a crash: the
    traceback is logged here and the client only learns the exception type.
    ``ToolError`` and ``MCPError`` (a deliberate protocol error) pass through.
    The SDK turns the resulting ``ToolError`` into an ``isError`` result, so a
    failing tool never takes the server process down.
    """
    try:
        yield
    except (ToolError, MCPError):
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        logger.exception("MCP tool %r raised an unexpected exception", tool)
        raise ToolError(f"internal error: {type(exc).__name__}") from exc


class _RawArgumentsMetadata(FuncMetadata):
    """Hand the call's raw ``arguments`` object to the tool function untouched.

    The SDK normally validates arguments with a pydantic model generated from
    the function signature; legacy tools are validated against their own JSON
    schema inside the tool function instead.
    """

    def validate_arguments(self, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        return {"arguments": dict(arguments_to_validate)}


def _decode_json_strings(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Decode JSON sent as a string for a parameter whose schema type is not ``string``.

    Some clients (Claude Desktop in particular) send ``"[1, 2]"`` or ``"5"`` where
    the schema asks for an array or an integer. The SDK pre-parses these for
    signature-typed tools; legacy tools get the same leniency before validation.
    """
    decoded = dict(arguments)
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return decoded
    for key, value in arguments.items():
        prop = properties.get(key)
        if not isinstance(value, str) or not isinstance(prop, Mapping):
            continue
        declared = prop.get("type")
        types = {declared} if isinstance(declared, str) else set(declared or ())
        if not types or "string" in types:
            continue
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            continue
        if not isinstance(parsed, str):
            decoded[key] = parsed
    return decoded


def _to_result(tool: str, envelope: object) -> CallToolResult:
    """Convert a legacy ``{"content": [...]}`` envelope into a ``CallToolResult``."""
    if isinstance(envelope, CallToolResult):
        return envelope
    try:
        return CallToolResult.model_validate(envelope)
    except ValidationError as exc:
        logger.error("MCP tool %r returned a malformed result envelope: %s", tool, exc)
        raise ToolError("internal error: malformed tool result") from exc


def _make_invoker(
    name: str, input_schema: dict[str, Any], handler: DispatchFn, context: Any
) -> Callable[[dict[str, Any]], CallToolResult]:
    validator_cls = jsonschema_validators.validator_for(input_schema)
    try:
        validator_cls.check_schema(input_schema)
    except jsonschema_exceptions.SchemaError as exc:
        raise ValueError(f"tool {name!r} has an invalid inputSchema: {exc.message}") from exc
    validator = validator_cls(input_schema)

    def invoke(arguments: dict[str, Any]) -> CallToolResult:
        with tool_errors(name):
            args = _decode_json_strings(input_schema, arguments)
            error = jsonschema_exceptions.best_match(validator.iter_errors(args))
            if error is not None:
                where = "/".join(str(part) for part in error.absolute_path)
                prefix = f"invalid argument {where!r}" if where else "invalid arguments"
                raise ToolError(f"{prefix}: {error.message}")
            envelope = handler(name, args, context)
        return _to_result(name, envelope)

    return invoke


def _tool_manager(server: MCPServer[Any]) -> ToolManager:
    """The server's tool manager.

    ``MCPServer.add_tool`` derives a tool's input schema from a Python
    signature and offers no public way to register a tool whose schema is
    given explicitly, so the adapter registers through the manager and then
    pins the advertised schema. Fail loudly if the SDK internals move.
    """
    manager = getattr(server, "_tool_manager", None)
    if not isinstance(manager, ToolManager):
        raise RuntimeError(
            "unsupported mcp SDK: MCPServer has no ToolManager; cannot register legacy tool groups"
        )
    return manager


def _registration_error(group: str, message: str) -> ValueError:
    logger.error("cannot register MCP tool group %r: %s", group, message)
    return ValueError(f"MCP tool group {group!r}: {message}")


def register_group(
    server: MCPServer[Any],
    schemas: Sequence[Mapping[str, Any]],
    dispatch: Mapping[str, DispatchFn],
    *,
    group: str,
    context: Any = None,
) -> list[str]:
    """Register a legacy tool group on ``server`` and return the tool names.

    Each tool keeps its original ``name``, ``description`` and ``inputSchema``;
    calls run ``dispatch[name](name, arguments, context)`` on a worker thread.
    The group is checked as a whole before anything is registered.

    Raises:
        ValueError: a tool name already registered on the server (or repeated
            in the group), a tool with no dispatch handler, a definition
            without a name or ``inputSchema``, or an invalid JSON schema.
    """
    manager = _tool_manager(server)
    prepared: list[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], CallToolResult]]] = []
    seen: set[str] = set()
    for schema in schemas:
        name = schema.get("name")
        if not isinstance(name, str) or not name:
            raise _registration_error(group, f"tool definition without a name: {schema!r}")
        if name in seen or manager.get_tool(name) is not None:
            raise _registration_error(group, f"duplicate MCP tool name {name!r}")
        handler = dispatch.get(name)
        if handler is None:
            raise _registration_error(group, f"tool {name!r} has no dispatch handler")
        input_schema = schema.get("inputSchema")
        if not isinstance(input_schema, Mapping):
            raise _registration_error(group, f"tool {name!r} has no inputSchema")
        pinned = copy.deepcopy(dict(input_schema))
        try:
            invoker = _make_invoker(name, pinned, handler, context)
        except ValueError as exc:
            raise _registration_error(group, str(exc)) from exc
        seen.add(name)
        prepared.append((name, str(schema.get("description") or ""), pinned, invoker))

    for name, description, pinned, invoker in prepared:
        tool = manager.add_tool(
            invoker, name=name, description=description, structured_output=False
        )
        tool.parameters = pinned
        tool.fn_metadata = _RawArgumentsMetadata(arg_model=ArgModelBase)
    logger.info("registered %d MCP tools from group %r", len(prepared), group)
    return [name for name, *_ in prepared]


__all__ = ["DispatchFn", "register_group", "tool_errors"]
