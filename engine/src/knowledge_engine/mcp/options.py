"""Command-line options for ``knowledge-engine mcp``.

Kept free of MCP SDK imports so building the top-level CLI parser stays cheap;
both ``knowledge_engine.cli`` and ``knowledge_engine.mcp.server.main`` use it.
"""

from __future__ import annotations

import argparse

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 9211


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range (1-65535): {port}")
    return port


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the MCP transport flags to ``parser``."""
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP over streamable HTTP at http://HOST:PORT/mcp instead of stdio.",
    )
    mode.add_argument(
        "--selftest",
        action="store_true",
        help="Build the server, list its tools over an in-process session, print a JSON "
        "summary and exit (non-zero if a base tool is missing).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Bind address for --http (default {DEFAULT_HTTP_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=None,
        help=f"Port for --http (default {DEFAULT_HTTP_PORT}).",
    )


def resolve_http_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Fill in ``--host``/``--port`` defaults, or reject them when ``--http`` is off.

    Without this the flags were accepted and silently ignored for stdio and
    ``--selftest`` runs. Call it right after ``parse_args``.
    """
    if not args.http and (args.host is not None or args.port is not None):
        parser.error("--host/--port only apply with --http")
    if args.host is None:
        args.host = DEFAULT_HTTP_HOST
    if args.port is None:
        args.port = DEFAULT_HTTP_PORT
