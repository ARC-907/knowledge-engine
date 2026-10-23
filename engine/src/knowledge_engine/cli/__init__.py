"""``knowledge-engine`` command line: parser and dispatch.

The parser is built here from the standard library alone (plus
``mcp.options``, which is SDK-free); each subcommand's implementation lives in
a sibling module that is imported only when that subcommand runs:

* :mod:`.corpus`          -- ``info``, ``bootstrap``, ``capabilities``, ``reindex``,
  ``watch``, ``search``, ``config show``
* :mod:`.registry`        -- ``registry validate``
* :mod:`.embeddings`      -- ``embeddings build``, ``embeddings search``, ``embeddings status``
* :mod:`.serve`           -- ``serve``, ``board-serve``
* :mod:`.mcp`             -- ``mcp``
* :mod:`.board`           -- ``board ...`` (the agent-board client)
* :mod:`.project_context` -- ``project-context ...``

:func:`main` configures stderr logging once (``-v``/``-q``), loads the
pipeline ``.env`` (variables already set win) and runs the subcommand. A
handler that raises :class:`CommandError` has its message printed on stderr
and exits with the error's status (1) instead of a traceback.
Library modules never print; command output goes to stdout from these
modules only.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any

from ..mcp.options import add_arguments as add_mcp_arguments

PROG = "knowledge-engine"

#: Commands that run until interrupted; their default log level is
#: ``KE_LOG_LEVEL`` (INFO) instead of WARNING.
LONG_RUNNING = frozenset({"serve", "board-serve", "mcp", "watch"})

Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]

#: ``search --mode`` values (``knowledge_engine.retrieval.hybrid.SEARCH_MODES``).
SEARCH_MODES = ("fts", "vector", "hybrid")
#: ``embeddings --provider`` values (``knowledge_engine.core.settings.EMBED_PROVIDERS``).
EMBED_PROVIDERS = ("fake", "ollama", "remote")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text!r}")
    return value


class CommandError(Exception):
    """A subcommand failed in an expected way; :func:`main` prints it and exits non-zero."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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
    reg_parser = sub.add_parser("registry", help="Inspect the corpus registry (registry.json)")
    reg_sub = reg_parser.add_subparsers(dest="registry_cmd", required=True)
    reg_validate = reg_sub.add_parser(
        "validate",
        help="Validate registry.json against the registry JSON Schema (exit 1 if invalid)",
    )
    reg_validate.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Registry file to check (default: $KE_REGISTRY_PATH or <corpus_root>/registry.json)",
    )
    reg_validate.add_argument(
        "--json", action="store_true", help="Print the result (and every error) as JSON"
    )
    _route(reg_validate, "registry", "cmd_registry_validate")
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
    sp.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        default="fts",
        help="fts: keyword index (default); vector: embedding index; hybrid: both, "
        "fused by reciprocal rank",
    )
    _route(sp, "corpus", "cmd_search")

    _add_embeddings_parser(sub)

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


def _embedding_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=EMBED_PROVIDERS,
        default=None,
        help="embedding provider (default: $KE_EMBED_PROVIDER or ollama; fake: offline dry run)",
    )
    parser.add_argument(
        "--model", default=None, help="embedding model (default: $KE_EMBED_MODEL or bge-m3)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="index database (default: $KE_EMBEDDINGS_DB or <data_dir>/embeddings.db)",
    )


def _add_embeddings_parser(sub: Any) -> None:
    emb = sub.add_parser(
        "embeddings",
        help="Corpus embedding index for vector / hybrid search (build, search, status)",
    )
    emb_sub = emb.add_subparsers(dest="embeddings_cmd", required=True)

    build = emb_sub.add_parser("build", help="Embed the corpus into the embedding index")
    mode = build.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true", help="only embed new or changed files")
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="drop the index first (required to switch to another model)",
    )
    _embedding_options(build)
    build.add_argument(
        "--batch",
        type=_positive_int,
        default=None,
        help="chunks per embedding request (default: $KE_EMBED_BATCH or 32)",
    )
    build.add_argument(
        "--chunk-size", type=_positive_int, default=None, help="characters per chunk (1500)"
    )
    build.add_argument(
        "--chunk-overlap",
        type=_non_negative_int,
        default=None,
        help="characters of overlap between chunks (200)",
    )
    build.add_argument(
        "--max-chars",
        type=_positive_int,
        default=None,
        help="characters of each chunk sent to the model (2000)",
    )
    _route(build, "embeddings", "cmd_embeddings_build")

    search = emb_sub.add_parser("search", help="Vector search over the embedding index")
    search.add_argument("query")
    search.add_argument("--top", type=_positive_int, default=10, help="hits to return (10)")
    search.add_argument(
        "--library", default=None, help="restrict to one top-level corpus subdirectory"
    )
    _embedding_options(search)
    _route(search, "embeddings", "cmd_embeddings_search")

    status = emb_sub.add_parser(
        "status", help="Report what the embedding index holds and which model built it"
    )
    _embedding_options(status)
    _route(status, "embeddings", "cmd_embeddings_status")


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
    loaded = load_dotenv_file()
    verbosity = args.verbose - args.quiet
    args.log_level_override = cli_log_level(verbosity) if verbosity else None
    restore = configure_cli_logging(args.log_level_override, long_running=args.cmd in LONG_RUNNING)
    if loaded:
        # load_dotenv_file() ran before the handler existed; say it now (-v shows it).
        logging.getLogger(__name__).info(
            "pipeline .env: loaded %d variable(s): %s", len(loaded), ", ".join(sorted(loaded))
        )
    try:
        module, func = args._handler
        return _load_handler(module, func)(args, parser)
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    finally:
        restore()


def __getattr__(name: str) -> Any:
    # PEP 562: names that lived on the old single-module CLI, resolved lazily so
    # importing ``knowledge_engine.cli`` stays cheap and side-effect free.
    if name in {"BASE_MCP_TOOLS", "_capability_inventory", "_bootstrap"}:
        return getattr(importlib.import_module(f"{__name__}.corpus"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EMBED_PROVIDERS",
    "LONG_RUNNING",
    "PROG",
    "SEARCH_MODES",
    "CommandError",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
