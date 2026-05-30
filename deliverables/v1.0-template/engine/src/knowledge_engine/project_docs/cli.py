"""CLI for the project-docs subsystem: ``knowledge-engine project-docs <cmd>``.

This is the local, MCP-free entry point for the same operations the MCP tools
expose. It is intentionally thin — each subcommand calls into the relevant
module. Read-only commands are always available; mutating/scanner/embedding
commands honor the same config gates as the MCP tools.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_config
from .paths import resolve_project_root


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _ctx():
    from .mcp_tools.base import ToolContext

    cfg = load_config()
    return ToolContext(cfg=cfg, root=resolve_project_root())


def _cmd_capabilities(_args) -> int:
    from .mcp_tools import capability_tools

    ctx = _ctx()
    res = capability_tools.dispatch("project_docs.capabilities", {}, ctx)
    print(res["content"][0]["text"])
    return 0


def _cmd_config_status(_args) -> int:
    from .config import _as_dict

    _print(_as_dict(load_config()))
    return 0


def _cmd_info(_args) -> int:
    cfg = load_config()
    root = resolve_project_root()
    _print({
        "root": str(root),
        "enabled": cfg.enabled,
        "database_dir": str(Path(root) / cfg.database_dir),
        "fingerprint_database": str(Path(root) / cfg.fingerprint_database),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge-engine project-docs")
    sub = parser.add_subparsers(dest="pd_cmd", required=True)

    sub.add_parser("info", help="Show resolved project root and storage paths")
    sub.add_parser("capabilities", help="Show enabled capabilities / gate states")
    sub.add_parser("config-status", help="Print the effective project-docs config")

    return parser


_HANDLERS = {
    "info": _cmd_info,
    "capabilities": _cmd_capabilities,
    "config-status": _cmd_config_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS.get(args.pd_cmd)
    if handler is None:
        parser.error(f"unknown command: {args.pd_cmd}")
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
