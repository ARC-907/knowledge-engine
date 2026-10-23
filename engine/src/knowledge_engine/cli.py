"""CLI entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from .core.config import Config
from .core.registry import EntryKind, Registry, RegistryEntry
from .core.settings import Settings
from .mcp.options import add_arguments as add_mcp_arguments, resolve_http_options
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


def _env_port(env_var: str, fallback: int) -> int:
    """Default port for a serve command: ``env_var`` when set to an integer, else ``fallback``.

    Keeps the ``KE_PORT`` knob the old serve scripts honoured; a non-integer
    value is reported on stderr and ignored rather than aborting startup.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        print(f"ignoring non-integer {env_var}={raw!r}; using port {fallback}", file=sys.stderr)
        return fallback


def _config_command(args: argparse.Namespace) -> int:
    """``knowledge-engine config show [--markdown]`` (no filesystem writes)."""
    from .core.settings import markdown_reference

    if args.markdown:
        sys.stdout.write(markdown_reference())
        return 0
    settings = Settings()
    resolved = settings.resolve_paths(warn_missing_corpus=False)
    report = {
        "settings": settings.masked(),
        "set_in_environment": settings.explicitly_set(),
        "paths": {
            "home": str(resolved.home),
            "corpus_root": str(resolved.corpus_root),
            "registry_path": str(resolved.registry_path),
            "data_dir": str(resolved.data_dir),
        },
    }
    print(json.dumps(report, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="knowledge-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Print configuration and counts")
    sub.add_parser("bootstrap", help="Walk corpus/ and register every folder as an entry")
    sub.add_parser("capabilities", help="Print runtime capability and retrieval-surface inventory")
    sub.add_parser("reindex", help="Rebuild FTS5 index over enabled entries")
    sub.add_parser("watch", help="Start watchdog: auto-register new corpus folders")
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
    mcp_parser = sub.add_parser(
        "mcp", help="Run the MCP server for AI clients (stdio, or streamable HTTP with --http)"
    )
    add_mcp_arguments(mcp_parser)

    sp = sub.add_parser("search", help="Search the index")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("serve", help="Run the FastAPI server (uvicorn)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None, help="default: $KE_PORT or 9210")

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
    abs_serve.add_argument("--host", default=None, help="default: $BOARD_HOST or 127.0.0.1")
    abs_serve.add_argument("--port", type=int, default=None, help="default: $BOARD_PORT or 11437")

    args = parser.parse_args()

    if args.cmd == "config":
        return _config_command(args)

    if args.cmd == "project-context":
        from .project_context.cli import main as pd_main

        return pd_main(args.pd_args)

    if args.cmd == "board":
        from .agent_board.cli import main as board_main

        return board_main(args.board_args)

    if args.cmd == "board-serve":
        from .agent_board.service import serve as board_serve

        # The removed serve-board.sh honoured BOARD_HOST/BOARD_PORT; keep those knobs.
        settings = Settings()
        host = args.host or settings.board_serve_host or "127.0.0.1"
        port = args.port
        if port is None:
            port = settings.board_serve_port or settings.board_standalone_port
        return board_serve(host=host, port=port)

    if args.cmd == "mcp":
        from .mcp.server import run as mcp_run

        resolve_http_options(parser, args)
        return mcp_run(args)

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
        settings = Settings()
        port = args.port if args.port is not None else settings.port or 9210
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed; pip install knowledge-engine", file=sys.stderr)
            return 1
        from .core.http import uvicorn_proxy_kwargs
        from .core.logging_config import uvicorn_log_config

        # The factory is called by uvicorn without arguments; the bind address
        # travels through the environment so the optional MCP mount can set its
        # Host-header policy (see server/lifespan.py: BIND_HOST_ENV).
        os.environ["KE_BIND_HOST"] = args.host
        settings = settings.model_copy(update={"bind_host": args.host})
        # Logging: uvicorn's default config wires only its own loggers, so the
        # engine's INFO policy lines would never be emitted. Proxy headers:
        # uvicorn trusts loopback X-Forwarded-For by default, which would
        # contradict KE_TRUST_PROXY; derive both from the same env vars.
        uvicorn.run(
            "knowledge_engine.app:create_app",
            host=args.host,
            port=port,
            factory=True,
            log_config=uvicorn_log_config(settings.log_level),
            **uvicorn_proxy_kwargs(settings=settings),
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
