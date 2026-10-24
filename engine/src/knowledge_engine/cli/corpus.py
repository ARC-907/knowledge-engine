"""Corpus and configuration subcommands: ``info``, ``bootstrap``, ``capabilities``,
``reindex``, ``watch``, ``search`` (``--mode fts|vector|hybrid``) and ``config show``.

Only the commands that need the registry construct a :class:`Config` and a
:class:`Registry`; ``config show`` touches neither.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.config import Config
    from ..core.registry import Registry

_logger = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    # ``BASE_MCP_TOOLS`` is derived from the MCP server's base tools; resolved
    # lazily because importing the server loads the MCP SDK.
    if name == "BASE_MCP_TOOLS":
        return base_mcp_tools()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def base_mcp_tools() -> list[str]:
    """Names of the engine's base MCP tools, as registered by ``mcp.server``."""
    from ..mcp.server import BASE_TOOLS

    return list(BASE_TOOLS)


def open_registry() -> tuple[Config, Registry]:
    """Resolve the configuration and load the registry for a corpus command.

    Creates the data directory first: every corpus command either writes the
    FTS index or is followed by one that does. An invalid ``registry.json``
    becomes a :class:`~knowledge_engine.cli.CommandError` listing every schema
    error, so the command exits 1 with a readable report instead of a traceback.
    """
    from ..core.config import Config
    from ..core.registry import Registry, RegistryValidationError
    from . import CommandError

    config = Config.from_env()
    config.ensure_dirs()
    try:
        registry = Registry(config.registry_path)
    except RegistryValidationError as exc:
        # The registry module logs a one-line ERROR summary; the full report
        # (one line per JSON pointer) is the command's output.
        _logger.debug("registry %s failed validation", config.registry_path, exc_info=True)
        raise CommandError(
            f"{exc.render()}\n"
            "Fix the registry (check it with `knowledge-engine registry validate`) and retry."
        ) from exc
    return config, registry


def _print_json(payload: Any, **kwargs: Any) -> None:
    print(json.dumps(payload, **kwargs))


def _bootstrap(config: Config, registry: Registry) -> dict[str, int]:
    """Walk corpus/ and register every library/skill/kit/tool folder found."""
    from ..core.watcher import register_corpus_folders

    return register_corpus_folders(config, registry)


def _safe_payload(fn: Callable[[], dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    """Run an optional capability probe without letting it break CLI inventory."""
    try:
        return fn()
    except Exception as exc:
        _logger.warning("capability probe %s failed: %s", fn.__name__, exc)
        _logger.debug("capability probe %s traceback", fn.__name__, exc_info=True)
        return {"status": "error", "error": str(exc), **fallback}


def _capability_inventory(config: Config, registry: Registry) -> dict[str, Any]:
    """Return the runtime capability inventory for humans and agents.

    This reports both seeded data and empty-but-available substrates. An empty
    hosted-tool table or empty skill registry is not a missing feature; it means
    no project/agent has populated that surface yet.
    """
    counts = registry.counts()
    base_tools = base_mcp_tools()

    def project_context_payload() -> dict[str, Any]:
        from ..project_context.config import load_config as load_pd_config
        from ..project_context.mcp_tools import collect_tools as collect_pd_tools

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
        from ..agent_board import scopes
        from ..agent_board.mcp_tools import collect_tools as collect_board_tools

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
        from ..tool_host import host

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
        from ..runtime.sandbox import get_sandbox

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
        "base_mcp": {"tool_count": len(base_tools), "tools": base_tools},
        "project_context": _safe_payload(project_context_payload, {"tool_count": 0, "tools": []}),
        "board": _safe_payload(board_payload, {"tool_count": 0, "tools": [], "scopes": []}),
        "hosted_tools": _safe_payload(hosted_tools_payload, {"tool_count": 0, "tools": []}),
        "sandbox": _safe_payload(sandbox_payload, {}),
    }


# ── Handlers ─────────────────────────────────────────────────────────


def cmd_info(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    config, registry = open_registry()
    _print_json(
        {
            "corpus_root": str(config.corpus_root),
            "data_dir": str(config.data_dir),
            "registry_path": str(config.registry_path),
            "counts": registry.counts(),
        },
        indent=2,
    )
    return 0


def cmd_capabilities(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    config, registry = open_registry()
    _print_json(_capability_inventory(config, registry), indent=2)
    return 0


def cmd_bootstrap(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    config, registry = open_registry()
    _print_json(_bootstrap(config, registry), indent=2)
    return 0


def cmd_watch(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..core.watcher import auto_register, lifecycle_allows_auto_register, start_watcher

    config, registry = open_registry()
    if lifecycle_allows_auto_register(registry):
        added = auto_register(config, registry)
    else:
        added = 0
        _logger.warning(
            "lifecycle flags are off (watch_enabled/auto_register); new folders are logged, "
            "not registered, until they are turned on"
        )
    _print_json({"initial_added": added})
    observer = start_watcher(config, registry)
    print("Watching... Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
    return 0


def cmd_reindex(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..retrieval.fts5 import Indexer

    config, registry = open_registry()
    indexer = Indexer(config, registry)
    try:
        counts = indexer.rebuild()
    finally:
        indexer.close()
    _print_json(counts, indent=2)
    return 0


def cmd_search(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..retrieval.fts5 import Indexer

    mode = getattr(args, "mode", "fts") or "fts"
    config, registry = open_registry()
    indexer = Indexer(config, registry)
    try:
        if mode == "fts":
            results = indexer.search(args.query, limit=args.limit)
        else:
            from ..retrieval.embeddings.errors import EmbeddingError
            from ..retrieval.embeddings.index import CorpusEmbeddingIndex
            from ..retrieval.hybrid import HybridSearcher

            try:
                embeddings = CorpusEmbeddingIndex.from_settings()
                searcher = HybridSearcher(indexer, registry, embeddings)
                results = searcher.search(args.query, mode=mode, limit=args.limit)
            except EmbeddingError as exc:
                _logger.error("%s search failed: %s (%s)", mode, exc, exc.hint)
                _print_json({"error": exc.to_dict()}, indent=2)
                return 1
    finally:
        indexer.close()
    if getattr(results, "syntax_fallback", False):
        _logger.warning("query %r is not valid FTS5 syntax; searched it as a phrase", args.query)
    _print_json(list(results), indent=2)
    return 0


def cmd_config_show(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge-engine config show [--markdown]`` (no filesystem writes)."""
    from ..core.settings import Settings, markdown_reference

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
    _print_json(report, indent=2, default=str)
    return 0
