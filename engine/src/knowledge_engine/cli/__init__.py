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
* :mod:`.knowledge`       -- ``knowledge migrate``, ``knowledge status`` and the
  scoped-knowledge admin commands

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

_logger = logging.getLogger(__name__)

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
    _add_knowledge_parser(sub)

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


#: ``knowledge record classify --type`` values
#: (``knowledge_engine.knowledge.contracts.ResearchType``).
RESEARCH_TYPES = (
    "reference",
    "paper",
    "article",
    "transcript",
    "code",
    "note",
    "dataset",
    "synthesis",
    "other",
)
#: ``knowledge record classify --status`` values
#: (``knowledge_engine.knowledge.contracts.CurationStatus``).
CURATION_STATUSES = ("unclassified", "inbox", "reviewed", "archived")


def _admin(parser: argparse.ArgumentParser, op: str) -> None:
    parser.set_defaults(_handler=("knowledge", "cmd_knowledge_admin"), knowledge_op=op)


def _selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source", action="append", metavar="ID", help="source id (repeatable; ORed)"
    )
    parser.add_argument(
        "--collection", action="append", metavar="ID", help="collection id (repeatable; ORed)"
    )
    parser.add_argument(
        "--type",
        action="append",
        choices=RESEARCH_TYPES,
        help="research type (repeatable; ORed)",
    )
    parser.add_argument("--tag", action="append", metavar="TAG", help="tag (repeatable; ANDed)")


def _add_knowledge_parser(sub: Any) -> None:
    kn = sub.add_parser(
        "knowledge",
        help="Scoped-knowledge metadata: migrate the corpus, status, workspaces, projects, "
        "collections, classification",
    )
    kn_sub = kn.add_subparsers(dest="knowledge_cmd", required=True)

    mig = kn_sub.add_parser(
        "migrate",
        help="Register the existing corpus in knowledge.db (dry run, or backup + resumable apply)",
    )
    mode = mig.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="report what --apply would do; write nothing"
    )
    mode.add_argument(
        "--apply", action="store_true", help="back up, then register in resumable batches"
    )
    mig.add_argument(
        "--backup-dir", default=None, help="backup location (default: <data_dir>/backups)"
    )
    mig.add_argument(
        "--batch-size", type=_positive_int, default=500, help="records per batch (500)"
    )
    mig.add_argument(
        "--max-batches",
        type=_positive_int,
        default=None,
        help="stop after N batches; a later --apply resumes from the checkpoint",
    )
    _route(mig, "knowledge", "cmd_knowledge_migrate")

    _route(
        kn_sub.add_parser("status", help="Workspace/project/collection/source/record counts"),
        "knowledge",
        "cmd_knowledge_status",
    )

    ws = kn_sub.add_parser("workspace", help="List or create workspaces")
    ws_sub = ws.add_subparsers(dest="knowledge_ws_cmd", required=True)
    _admin(ws_sub.add_parser("list", help="List workspaces"), "workspace_list")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("name", help="display name")
    ws_create.add_argument("--id", default=None, help="workspace id (generated when omitted)")
    ws_create.add_argument("--description", default=None)
    _admin(ws_create, "workspace_create")

    pr = kn_sub.add_parser("project", help="List or create projects")
    pr_sub = pr.add_subparsers(dest="knowledge_project_cmd", required=True)
    pr_list = pr_sub.add_parser("list", help="List the projects of a workspace")
    pr_list.add_argument("workspace_id")
    _admin(pr_list, "project_list")
    pr_create = pr_sub.add_parser("create", help="Create a project (names may repeat)")
    pr_create.add_argument("workspace_id")
    pr_create.add_argument("name", help="display name")
    pr_create.add_argument("--id", default=None, help="project id (generated when omitted)")
    pr_create.add_argument("--description", default=None)
    _admin(pr_create, "project_create")

    src = kn_sub.add_parser("source", help="List record-owning sources")
    src_sub = src.add_subparsers(dest="knowledge_source_cmd", required=True)
    src_list = src_sub.add_parser("list", help="List the sources of a workspace")
    src_list.add_argument("workspace_id")
    _admin(src_list, "source_list")

    col = kn_sub.add_parser("collection", help="List, create and edit collections")
    col_sub = col.add_subparsers(dest="knowledge_collection_cmd", required=True)
    col_list = col_sub.add_parser("list", help="List the collections of a workspace")
    col_list.add_argument("workspace_id")
    col_list.add_argument("--project", default=None, help="only this project's collections")
    _admin(col_list, "collection_list")
    col_create = col_sub.add_parser("create", help="Create a collection")
    col_create.add_argument("workspace_id")
    col_create.add_argument("name", help="display name")
    col_create.add_argument("--id", default=None, help="collection id (generated when omitted)")
    col_create.add_argument("--project", default=None, help="owning project id")
    col_create.add_argument("--description", default=None)
    _admin(col_create, "collection_create")
    for verb, op, text in (
        ("add", "collection_add", "Add records to a collection"),
        ("remove", "collection_remove", "Remove records from a collection"),
    ):
        member = col_sub.add_parser(verb, help=text)
        member.add_argument("collection_id")
        member.add_argument(
            "--record",
            nargs=2,
            action="append",
            required=True,
            metavar=("SOURCE_ID", "RECORD_ID"),
            help="a record (repeatable)",
        )
        _admin(member, op)

    rec = kn_sub.add_parser("record", help="List and classify records")
    rec_sub = rec.add_subparsers(dest="knowledge_record_cmd", required=True)
    rec_list = rec_sub.add_parser("list", help="List the records in a scope")
    rec_list.add_argument("workspace_id", nargs="?", default=None)
    rec_list.add_argument("--project", default=None, help="project id")
    rec_list.add_argument(
        "--include-workspace",
        action="store_true",
        help="with --project: also records that belong to no project",
    )
    rec_list.add_argument("--profile", default=None, help="profile for omitted selections")
    _selection_options(rec_list)
    rec_list.add_argument(
        "--context", default=None, help="a full QueryContext as JSON (overrides the flags)"
    )
    rec_list.add_argument("--limit", type=_positive_int, default=100)
    rec_list.add_argument("--offset", type=_non_negative_int, default=0)
    _admin(rec_list, "record_list")
    classify = rec_sub.add_parser("classify", help="Set research type, status, tags or project")
    classify.add_argument("source_id")
    classify.add_argument("record_id")
    classify.add_argument("--type", choices=RESEARCH_TYPES, default=None, help="research type")
    classify.add_argument(
        "--status", choices=CURATION_STATUSES, default=None, help="curation status"
    )
    classify.add_argument(
        "--set-tag", action="append", metavar="TAG", help="replace the tags (repeatable)"
    )
    classify.add_argument("--add-tag", action="append", metavar="TAG", help="add a tag")
    classify.add_argument("--remove-tag", action="append", metavar="TAG", help="remove a tag")
    project = classify.add_mutually_exclusive_group()
    project.add_argument("--project", default=None, help="assign to this project")
    project.add_argument(
        "--clear-project", action="store_true", help="make the record workspace-wide"
    )
    _admin(classify, "record_classify")

    prof = kn_sub.add_parser("profile", help="List or set scope profiles")
    prof_sub = prof.add_subparsers(dest="knowledge_profile_cmd", required=True)
    prof_list = prof_sub.add_parser("list", help="List the profiles of a workspace")
    prof_list.add_argument("workspace_id")
    _admin(prof_list, "profile_list")
    prof_set = prof_sub.add_parser("set", help="Create or replace a profile")
    prof_set.add_argument("workspace_id")
    prof_set.add_argument("profile_id")
    prof_set.add_argument("--description", default=None)
    _selection_options(prof_set)
    _admin(prof_set, "profile_set")


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
        _close_pipeline_db()
        restore()


def _close_pipeline_db() -> None:
    """Close cached pipeline/board DB connections before the process exits."""
    # Only when a subcommand actually loaded the substrate: plain commands
    # (search, serve ...) never pay for importing it.
    db = sys.modules.get("knowledge_engine.runtime.foundation.db")
    if db is None:
        return
    try:
        db.close_all()
    except Exception:
        _logger.exception("closing the pipeline DB connections failed")


def __getattr__(name: str) -> Any:
    # PEP 562: names that lived on the old single-module CLI, resolved lazily so
    # importing ``knowledge_engine.cli`` stays cheap and side-effect free.
    if name in {"BASE_MCP_TOOLS", "_capability_inventory", "_bootstrap"}:
        return getattr(importlib.import_module(f"{__name__}.corpus"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CURATION_STATUSES",
    "EMBED_PROVIDERS",
    "LONG_RUNNING",
    "PROG",
    "RESEARCH_TYPES",
    "SEARCH_MODES",
    "CommandError",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
