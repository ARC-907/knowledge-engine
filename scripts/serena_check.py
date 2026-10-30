"""Start Serena for this checkout over stdio and run read-only symbol queries.

A health check for the development setup (``nox -s serena -- --check``): it
verifies that Serena starts with ``.serena/project.yml``, negotiates MCP,
exposes the symbol tools (not only its REPL tool), and answers
``get_symbols_overview``, ``find_symbol`` and ``find_referencing_symbols`` for
a known engine symbol. Nothing is edited.

Usage (from the repository root, with the engine environment)::

    uv --project engine run python scripts/serena_check.py --serena "$(command -v serena)"

``--serena`` is the Serena executable (``serena`` from ``serena-agent``, or
``uvx --from serena-agent==2.1.0 serena``, split on spaces). Exit status is
non-zero when any check fails; the report is JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Serena flags for an unattended stdio server: no dashboard, no GUI, tool interface.
SERENA_ARGS = (
    "start-mcp-server",
    "--transport",
    "stdio",
    "--context",
    "agent",
    "--agent-interface",
    "tools",
    "--enable-web-dashboard",
    "false",
    "--open-web-dashboard",
    "false",
    "--enable-gui-log-window",
    "false",
    "--log-level",
    "WARNING",
)
READ_TOOLS = ("find_symbol", "get_symbols_overview", "find_referencing_symbols")
PROBE_FILE = "engine/src/knowledge_engine/retrieval/ownership.py"
PROBE_SYMBOL = "EntryOwnership"


def serena_env() -> dict[str, str]:
    """Child environment: the caller's, with Serena's usage reporting turned off."""
    env = dict(os.environ)
    env["SERENA_USAGE_REPORTING"] = "false"
    if shutil.which("uvx", path=env.get("PATH")) is None:
        sys.stderr.write("warning: uvx is not on PATH; Serena starts pyright through it\n")
    return env


async def check(command: list[str], project: Path, request_timeout: float) -> dict[str, Any]:
    params = StdioServerParameters(
        command=command[0],
        args=[*command[1:], *SERENA_ARGS, "--project", str(project)],
        env=serena_env(),
    )
    report: dict[str, Any] = {"project": str(project), "ok": False}
    async with Client(params, read_timeout_seconds=request_timeout) as client:
        info = client.server_info
        report["server"] = f"{info.name} {info.version}" if info else None
        tools = {t.name for t in (await client.list_tools()).tools}
        report["tool_count"] = len(tools)
        missing = [name for name in READ_TOOLS if name not in tools]
        report["missing_tools"] = missing
        if missing:
            return report
        calls = {
            "get_symbols_overview": {"relative_path": PROBE_FILE},
            "find_symbol": {"name_path_pattern": PROBE_SYMBOL, "relative_path": PROBE_FILE},
            "find_referencing_symbols": {
                "name_path": PROBE_SYMBOL,
                "relative_path": PROBE_FILE,
            },
        }
        results: dict[str, Any] = {}
        for name, arguments in calls.items():
            result = await client.call_tool(name, arguments)
            text = "".join(c.text for c in result.content if getattr(c, "text", None))
            results[name] = {
                "is_error": bool(result.is_error),
                "mentions_symbol": PROBE_SYMBOL in text,
                "chars": len(text),
            }
        report["calls"] = results
        report["ok"] = all(not r["is_error"] and r["mentions_symbol"] for r in results.values())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--serena",
        default=os.environ.get("SERENA", "serena"),
        help="Serena command (default: $SERENA or `serena`); split like a shell would",
    )
    parser.add_argument("--project", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--timeout", type=float, default=300, help="seconds per MCP request (first start indexes)"
    )
    args = parser.parse_args(argv)
    command = shlex.split(args.serena)
    if not command or shutil.which(command[0]) is None:
        print(json.dumps({"ok": False, "error": f"Serena command not found: {args.serena!r}"}))
        return 2
    try:
        report = anyio.run(check, command, args.project.resolve(), args.timeout)
    except Exception as exc:  # report any startup or protocol failure as JSON
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
