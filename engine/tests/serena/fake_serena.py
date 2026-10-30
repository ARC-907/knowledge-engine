"""A fake Serena 2.1.0 stdio MCP server for the adapter tests (no Serena needed).

It impersonates the server name/version, the full upstream tool list and the
recorded input schemas of the three read tools, and answers them from the
Python files under ``--project`` (via ``ast``) in Serena's result shapes:
0-based inclusive ``body_location`` lines, JSON text content.

Behaviour switches (environment):

* ``FAKE_SERENA_MODE``: ``normal`` (default), ``wrong_version``,
  ``schema_drift``, ``extra_tool``, ``slow`` (sleeps ``FAKE_SERENA_SLEEP_S``
  before answering), ``escape`` (results point outside the project),
  ``crash_once`` (exits on the first tool call when ``FAKE_SERENA_STATE``
  does not exist yet, creating it);
* ``FAKE_SERENA_LOG``: append one JSON line per call (pid, project, tool, args).
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

HERE = Path(__file__).resolve().parent
SCHEMAS: dict[str, dict[str, Any]] = json.loads(
    (HERE / "serena_2_1_0_read_schemas.json").read_text(encoding="utf-8")
)
UPSTREAM = [
    "activate_project",
    "create_text_file",
    "delete_memory",
    "edit_memory",
    "execute_shell_command",
    "find_declaration",
    "find_file",
    "find_implementations",
    "find_referencing_symbols",
    "find_symbol",
    "get_current_config",
    "get_diagnostics_for_file",
    "get_symbols_overview",
    "insert_after_symbol",
    "insert_before_symbol",
    "list_dir",
    "list_memories",
    "onboarding",
    "read_file",
    "read_memory",
    "rename_memory",
    "rename_symbol",
    "replace_content",
    "replace_in_files",
    "replace_symbol_body",
    "safe_delete_symbol",
    "search_for_pattern",
    "write_memory",
]
MODE = os.environ.get("FAKE_SERENA_MODE", "normal")


def _project() -> Path:
    argv = sys.argv
    return Path(argv[argv.index("--project") + 1]).resolve()


PROJECT = _project()


def _definitions(rel: str) -> list[dict[str, Any]]:
    path = PROJECT / rel
    source = path.read_text(encoding="utf-8")
    found: list[dict[str, Any]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                kind = (
                    "Class"
                    if isinstance(child, ast.ClassDef)
                    else ("Method" if prefix else "Function")
                )
                end = child.end_lineno or child.lineno
                found.append(
                    {
                        "name_path": name,
                        "kind": kind,
                        "relative_path": rel,
                        "body_location": {"start_line": child.lineno - 1, "end_line": end - 1},
                        "_body": "\n".join(source.splitlines()[child.lineno - 1 : end]),
                    }
                )
                visit(child, name + "/")

    visit(ast.parse(source), "")
    return found


def _python_files(relative: str = "") -> list[str]:
    base = PROJECT / relative if relative else PROJECT
    if base.is_file():
        return [base.relative_to(PROJECT).as_posix()]
    return sorted(
        p.relative_to(PROJECT).as_posix()
        for p in base.rglob("*.py")
        if ".git" not in p.parts and ".serena" not in p.parts
    )


def find_symbol(args: dict[str, Any]) -> Any:
    pattern = args["name_path_pattern"]
    substring = bool(args.get("substring_matching"))
    out = []
    for rel in _python_files(args.get("relative_path") or ""):
        for item in _definitions(rel):
            last = item["name_path"].rsplit("/", 1)[-1]
            wanted = pattern.rsplit("/", 1)[-1]
            hit = (wanted in last) if substring else item["name_path"].endswith(pattern)
            if hit:
                entry = {k: v for k, v in item.items() if not k.startswith("_")}
                if args.get("include_body"):
                    entry["body"] = item["_body"]
                out.append(entry)
    if MODE == "escape":
        for entry in out:
            entry["relative_path"] = "../outside.py"
    return out


def get_symbols_overview(args: dict[str, Any]) -> Any:
    rel = args["relative_path"]
    if not (PROJECT / rel).exists():
        raise FileNotFoundError(f"File or directory {rel} does not exist in the project.")
    overview: dict[str, list[str]] = {}
    for item in _definitions(rel):
        if "/" not in item["name_path"]:
            overview.setdefault(item["kind"], []).append(item["name_path"])
    return overview


def find_referencing_symbols(args: dict[str, Any]) -> Any:
    name = args["name_path"].rsplit("/", 1)[-1]
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for rel in _python_files():
        if rel == args["relative_path"]:
            continue
        lines = (PROJECT / rel).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if name in line:
                target = "../escape.py" if MODE == "escape" else rel
                out.setdefault(target, {}).setdefault("File", []).append(
                    {
                        "name_path": Path(rel).stem,
                        "body_location": {"start_line": number, "end_line": number},
                        "content_around_reference": f"  > {number + 1}:{line}",
                    }
                )
    return out


HANDLERS = {
    "find_symbol": find_symbol,
    "get_symbols_overview": get_symbols_overview,
    "find_referencing_symbols": find_referencing_symbols,
}


def _tools() -> list[types.Tool]:
    tools = []
    for name in UPSTREAM + (["brand_new_tool"] if MODE == "extra_tool" else []):
        schema = SCHEMAS.get(name, {"type": "object", "properties": {}})
        if MODE == "schema_drift" and name == "find_symbol":
            schema = {**schema, "required": ["name_path_pattern", "relative_path"]}
        tools.append(types.Tool(name=name, description=name, input_schema=schema))
    return tools


async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_tools())


def _record(tool: str, args: dict[str, Any]) -> None:
    """Log the call; in ``crash_once`` mode exit on the first call of the test."""
    log = os.environ.get("FAKE_SERENA_LOG")
    if log:
        entry = {
            "pid": os.getpid(),
            "project": str(PROJECT),
            "tool": tool,
            "args": args,
            "home": os.environ.get("HOME"),
            "reporting": os.environ.get("SERENA_USAGE_REPORTING"),
        }
        with Path(log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    state = os.environ.get("FAKE_SERENA_STATE")
    if MODE == "crash_once" and state and not Path(state).exists():
        Path(state).write_text("crashed", encoding="utf-8")
        os._exit(3)


async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = dict(params.arguments or {})
    _record(params.name, args)
    if MODE == "slow":
        await anyio.sleep(float(os.environ.get("FAKE_SERENA_SLEEP_S", "5")))
    handler = HANDLERS.get(params.name)
    if handler is None:
        text = f"Error executing tool {params.name}: not available in the fake"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=True
        )
    try:
        payload = handler(args)
    except (FileNotFoundError, KeyError, SyntaxError) as exc:
        text = f"Error executing tool {params.name}: {type(exc).__name__}: {exc}"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=True
        )
    text = json.dumps(payload)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def main() -> None:
    version = "2.0.9" if MODE == "wrong_version" else "2.1.0"
    server = Server(
        "Serena", version=version, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    time.sleep(0)
    anyio.run(run)


if __name__ == "__main__":
    main()
