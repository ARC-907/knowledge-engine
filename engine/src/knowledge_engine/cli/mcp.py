"""``knowledge-engine mcp``: the MCP server (stdio, streamable HTTP or --selftest)."""

from __future__ import annotations

import argparse


def cmd_mcp(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from ..mcp.options import resolve_http_options
    from ..mcp.server import run as mcp_run

    resolve_http_options(parser, args)
    return mcp_run(args)
