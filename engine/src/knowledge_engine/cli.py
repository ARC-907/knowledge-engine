"""CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from .core.config import Config
from .core.registry import EntryKind, Registry, RegistryEntry
from .retrieval.fts5 import Indexer

BASE_MCP_TOOLS = ["search", "registry_list", "registry_toggle", "registry_get"]


def _slugify(name: str) -> str:
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "&"):
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _bootstrap(config: Config, registry: Registry) -> dict[str, int]:
    """Walk corpus/ and register every library/skill/kit/tool folder found."""
    root = config.corpus_root
    plan: list[tuple[str, EntryKind]] = [
        ("libraries", "library"),
        ("skills", "skill"),
        ("kits", "kit"),
        ("capabilities", "tool"),
    ]
    counts: dict[str, int] = {"library": 0, "skill": 0, "kit": 0, "tool": 0, "skipped": 0}
    for sub, kind in plan:
        folder = root / sub
        if not folder.exists():
            continue
        for child in sorted(folder.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            entry_id = f"{kind}-{_slugify(child.name)}"
            if registry.get(entry_id):
                counts["skipped"] += 1
                continue
            registry.upsert(
                RegistryEntry(
                    id=entry_id,
                    kind=kind,
                    name=child.name,
                    path=str(child.relative_to(root)).replace("\\", "/"),
                    auto_registered=True,
                )
            )
            counts[kind] += 1
    return counts


def _safe_payload(fn: Callable[[], dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    """Run an optional capability probe without letting it break CLI inventory."""
    try:
        return fn()
    except Exception as exc:
        return {"status": "error", "error": str(exc), **fallback}


def _capability_inventory(config: Config, registry: Registry) -> dict[str, Any]:
    """Return the runtime capability inventory for humans and agents.

    This reports both seeded data and empty-but-available substrates. An empty
    hosted-tool table or empty skill registry is not a missing feature; it means
    no project/agent has populated that surface yet.
    """
    counts = {
        "libraries": len(registry.list("library")),
        "skills": len(registry.list("skill")),
        "kits": len(registry.list("kit")),
        "tools": len(registry.list("tool")),
    }

    def project_context_payload() -> dict[str, Any]:
        from .project_context.config import load_config as load_pd_config
        from .project_context.mcp_tools import collect_tools as collect_pd_tools

        pd_cfg = load_pd_config()
        pd_tools, _ = collect_pd_tools(pd_cfg)
        return {
            "status": "available",
            "enabled": pd_cfg.enabled,
            "mcp_enabled": pd_cfg.mcp.enabled,
            "tool_count": len(pd_tools),
            "tools": [tool["name"] for tool in pd_tools],
        }

    def board_payload() -> dict[str, Any]:
        from .agent_board import scopes
        from .agent_board.mcp_tools import collect_tools as collect_board_tools

        board_tools, _ = collect_board_tools()
        known_scopes = scopes.list_scopes()
        return {
            "status": "available",
            "tool_count": len(board_tools),
            "tools": [tool["name"] for tool in board_tools],
            "scope_count": len(known_scopes),
            "scopes": known_scopes,
        }

    def hosted_tools_payload() -> dict[str, Any]:
        from .tool_host import host

        hosted = host.list_tools(enabled_only=False)
        return {
            "status": "available",
            "tool_count": len(hosted),
            "tools": [
                {
                    "name": tool.get("name"),
                    "kind": tool.get("kind"),
                    "route": tool.get("route"),
                    "enabled": bool(tool.get("enabled", 0)),
                }
                for tool in hosted
            ],
        }

    def sandbox_payload() -> dict[str, Any]:
        from .runtime.sandbox import get_sandbox

        sandbox = get_sandbox()
        return {
            "status": "available",
            "adapter": getattr(sandbox, "name", sandbox.__class__.__name__),
            "configured": getattr(sandbox, "name", "") != "noop",
        }

    return {
        "runtime": "knowledge-engine",
        "corpus_root": str(config.corpus_root),
        "data_dir": str(config.data_dir),
        "registry": {
            "path": str(config.registry_path),
            "counts": counts,
            "empty_surfaces": [name for name, count in counts.items() if count == 0],
        },
        "base_mcp": {"tool_count": len(BASE_MCP_TOOLS), "tools": BASE_MCP_TOOLS},
        "project_context": _safe_payload(project_context_payload, {"tool_count": 0, "tools": []}),
        "board": _safe_payload(board_payload, {"tool_count": 0, "tools": [], "scopes": []}),
        "hosted_tools": _safe_payload(hosted_tools_payload, {"tool_count": 0, "tools": []}),
        "sandbox": _safe_payload(sandbox_payload, {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="knowledge-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Print configuration and counts")
    sub.add_parser("bootstrap", help="Walk corpus/ and register every folder as an entry")
    sub.add_parser("capabilities", help="Print runtime capability and retrieval-surface inventory")
    sub.add_parser("reindex", help="Rebuild FTS5 index over enabled entries")
    sub.add_parser("watch", help="Start watchdog: auto-register new corpus folders")
    sub.add_parser("mcp", help="Run MCP stdio server for AI clients")

    sp = sub.add_parser("search", help="Search the index")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("serve", help="Run the FastAPI server (uvicorn)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=9210)

    pd = sub.add_parser("project-context", help="Project-specific documentation subsystem")
    pd.add_argument(
        "pd_args", nargs=argparse.REMAINDER, help="Subcommand + args (e.g. 'info', 'capabilities')"
    )

    ab = sub.add_parser("board", help="Agent Board (post/read/search/ack/sweep/keys)")
    ab.add_argument(
        "board_args",
        nargs=argparse.REMAINDER,
        help="Subcommand + args (e.g. 'status', 'post', 'read', 'search')",
    )

    abs_serve = sub.add_parser(
        "board-serve",
        help="Run the standalone Agent Board service (separate port).",
    )
    abs_serve.add_argument("--host", default="127.0.0.1")
    abs_serve.add_argument("--port", type=int, default=11437)

    args = parser.parse_args()

    if args.cmd == "project-context":
        from .project_context.cli import main as pd_main

        return pd_main(args.pd_args)

    if args.cmd == "board":
        from .agent_board.cli import main as board_main

        return board_main(args.board_args)

    if args.cmd == "board-serve":
        from .agent_board.service import serve as board_serve

        return board_serve(host=args.host, port=args.port)

    config = Config.from_env()
    # Every command below opens the registry mirror (and most write an index),
    # so the data dir must exist before the first SQLite connection.
    config.ensure_dirs()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")

    if args.cmd == "info":
        print(
            json.dumps(
                {
                    "corpus_root": str(config.corpus_root),
                    "data_dir": str(config.data_dir),
                    "registry_path": str(config.registry_path),
                    "counts": {
                        "libraries": len(registry.list("library")),
                        "skills": len(registry.list("skill")),
                        "kits": len(registry.list("kit")),
                        "tools": len(registry.list("tool")),
                    },
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "capabilities":
        print(json.dumps(_capability_inventory(config, registry), indent=2))
        return 0

    if args.cmd == "bootstrap":
        counts = _bootstrap(config, registry)
        print(json.dumps(counts, indent=2))
        return 0

    if args.cmd == "watch":
        from .core.watcher import auto_register, start_watcher

        added = auto_register(config, registry)
        print(json.dumps({"initial_added": added}))
        observer = start_watcher(config, registry)
        print("Watching... Ctrl+C to stop.", file=sys.stderr)
        try:
            import time as _time

            while True:
                _time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            observer.join()
        return 0

    if args.cmd == "mcp":
        from .mcp.server import main as mcp_main

        return mcp_main()

    if args.cmd == "reindex":
        idx = Indexer(config, registry)
        counts = idx.rebuild()
        print(json.dumps(counts, indent=2))
        return 0

    if args.cmd == "search":
        idx = Indexer(config, registry)
        results = idx.search(args.query, limit=args.limit)
        print(json.dumps(results, indent=2))
        return 0

    if args.cmd == "serve":
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed; pip install knowledge-engine", file=sys.stderr)
            return 1
        from .core.http import uvicorn_proxy_kwargs
        from .core.logging_config import uvicorn_log_config

        # Logging: uvicorn's default config wires only its own loggers, so the
        # engine's INFO policy lines would never be emitted. Proxy headers:
        # uvicorn trusts loopback X-Forwarded-For by default, which would
        # contradict KE_TRUST_PROXY; derive both from the same env vars.
        uvicorn.run(
            "knowledge_engine.app:create_app",
            host=args.host,
            port=args.port,
            factory=True,
            log_config=uvicorn_log_config(),
            **uvicorn_proxy_kwargs(),
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
