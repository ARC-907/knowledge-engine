"""``knowledge-engine`` command line: parser and dispatch.

The parser is built here from the standard library alone (plus
``mcp.options``, which is SDK-free); each subcommand's implementation lives in
a sibling module that is imported only when that subcommand runs:

* :mod:`.corpus`          -- ``info``, ``bootstrap``, ``capabilities``, ``reindex``,
  ``watch``, ``search``, ``config show``
* :mod:`.serve`           -- ``serve``, ``board-serve``
* :mod:`.mcp`             -- ``mcp``
* :mod:`.board`           -- ``board ...`` (the agent-board client)
* :mod:`.project_context` -- ``project-context ...``

:func:`main` configures stderr logging once (``-v``/``-q``), loads the
pipeline ``.env`` (variables already set win) and runs the subcommand.
Library modules never print; command output goes to stdout from these
modules only.
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable, Sequence
from typing import Any

from ..mcp.options import add_arguments as add_mcp_arguments

PROG = "knowledge-engine"

#: Commands that run until interrupted; their default log level is
#: ``KE_LOG_LEVEL`` (INFO) instead of WARNING.
LONG_RUNNING = frozenset({"serve", "board-serve", "mcp", "watch"})

Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]


def _route(parser: argparse.ArgumentParser, module: str, func: str) -> None:
    parser.set_defaults(_handler=(module, func))


def build_parser() -> argparse.ArgumentParser:
    """The ``knowledge-engine`` argument parser (no I/O, no heavy imports)."""
    parser = argparse.ArgumentParser(prog=PROG)
    noise = parser.add_mutually_exclusive_group()
    noise.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="More log output on stderr (-v: INFO, -vv: DEBUG).",
    )
    noise.add_argument(
        "-q",
        "--quiet",
        action="count",
        default=0,
        help="Less log output on stderr (-q: ERROR, -qq: CRITICAL).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _route(sub.add_parser("info", help="Print configuration and counts"), "corpus", "cmd_info")
    _route(
        sub.add_parser("bootstrap", help="Walk corpus/ and register every folder as an entry"),
        "corpus",
        "cmd_bootstrap",
    )
    _route(
        sub.add_parser(
            "capabilities", help="Print runtime capability and retrieval-surface inventory"
        ),
        "corpus",
        "cmd_capabilities",
    )
    _route(
        sub.add_parser("reindex", help="Rebuild FTS5 index over enabled entries"),
        "corpus",
        "cmd_reindex",
    )
    _route(
        sub.add_parser("watch", help="Start watchdog: auto-register new corpus folders"),
        "corpus",
        "cmd_watch",
    )
    cfg_parser = sub.add_parser("config", help="Inspect the effective configuration")
    cfg_sub = cfg_parser.add_subparsers(dest="config_cmd", required=True)
    cfg_show = cfg_sub.add_parser(
        "show", help="Print every setting's effective value (secrets masked) as JSON"
    )
    cfg_show.add_argument(
        "--markdown",
        action="store_true",
        help="Print the configuration reference table instead (01-user-docs/CONFIGURATION.md)",
    )
    _route(cfg_show, "corpus", "cmd_config_show")
    mcp_parser = sub.add_parser(
        "mcp", help="Run the MCP server for AI clients (stdio, or streamable HTTP with --http)"
    )
    add_mcp_arguments(mcp_parser)
    _route(mcp_parser, "mcp", "cmd_mcp")

    sp = sub.add_parser("search", help="Search the index")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    _route(sp, "corpus", "cmd_search")

    sp = sub.add_parser("serve", help="Run the FastAPI server (uvicorn)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None, help="default: $KE_PORT or 9210")
    _route(sp, "serve", "cmd_serve")

    pd = sub.add_parser("project-context", help="Project-specific documentation subsystem")
    pd.add_argument(
        "pd_args", nargs=argparse.REMAINDER, help="Subcommand + args (e.g. 'info', 'capabilities')"
    )
    _route(pd, "project_context", "cmd_project_context")

    ab = sub.add_parser("board", help="Agent Board (post/read/search/ack/sweep/keys)")
    ab.add_argument(
        "board_args",
        nargs=argparse.REMAINDER,
        help="Subcommand + args (e.g. 'status', 'post', 'read', 'search')",
    )
    _route(ab, "board", "cmd_board")

    abs_serve = sub.add_parser(
        "board-serve",
        help="Run the standalone Agent Board service (separate port).",
    )
    abs_serve.add_argument("--host", default=None, help="default: $BOARD_HOST or 127.0.0.1")
    abs_serve.add_argument("--port", type=int, default=None, help="default: $BOARD_PORT or 11437")
    _route(abs_serve, "serve", "cmd_board_serve")
    return parser


def _load_handler(module: str, func: str) -> Handler:
    handler: Handler = getattr(importlib.import_module(f"{__name__}.{module}"), func)
    return handler


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``knowledge-engine`` console script."""
    from ..core.logging_config import cli_log_level, configure_cli_logging
    from ..runtime.foundation.config import load_dotenv_file

    parser = build_parser()
    args = parser.parse_args(argv)
    # The pipeline .env used to be loaded as an import side effect; do it
    # explicitly, before any subcommand reads its settings.
    load_dotenv_file()
    verbosity = args.verbose - args.quiet
    args.log_level_override = cli_log_level(verbosity) if verbosity else None
    restore = configure_cli_logging(args.log_level_override, long_running=args.cmd in LONG_RUNNING)
    try:
        module, func = args._handler
        return _load_handler(module, func)(args, parser)
    finally:
        restore()


def __getattr__(name: str) -> Any:
    # PEP 562: names that lived on the old single-module CLI, resolved lazily so
    # importing ``knowledge_engine.cli`` stays cheap and side-effect free.
    if name in {"BASE_MCP_TOOLS", "_capability_inventory", "_bootstrap"}:
        return getattr(importlib.import_module(f"{__name__}.corpus"), name)
    if name == "_env_port":
        return importlib.import_module(f"{__name__}.serve")._env_port
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LONG_RUNNING", "PROG", "build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
