"""MCP server for the knowledge engine, built on the official MCP Python SDK.

Exposes corpus search and the registry tools — plus the optional
project-context and agent-board tool groups — over stdio (default) or
streamable HTTP. The SDK negotiates the protocol: modern clients get
``2026-07-28`` via ``server/discover``; clients using the ``initialize``
handshake get the newest revision they offer, back to ``2024-11-05``.

Run via ``knowledge-engine mcp [--http] [--selftest]`` or
``python -m knowledge_engine.mcp.server``. Logging goes to stderr (level from
``KE_LOG_LEVEL``); stdout belongs to the stdio transport.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

import anyio
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import BeforeValidator, Field

from .. import __version__
from ..core.config import Config
from ..core.registry import EntryKind, Registry, RegistryValidationError
from ..retrieval.embeddings.errors import EmbeddingError
from ..retrieval.fts5 import Indexer
from ..retrieval.hybrid import DEFAULT_MODE, HybridSearcher, SearchMode
from .http import transport_security
from .options import add_arguments, resolve_http_options
from .toolkit import DispatchFn, register_group, tool_errors

if TYPE_CHECKING:
    from ..retrieval.embeddings.index import CorpusEmbeddingIndex

logger = logging.getLogger(__name__)

SERVER_NAME = "knowledge-engine"
BASE_TOOLS = ("search", "registry_list", "registry_toggle", "registry_get")

INSTRUCTIONS = """\
Knowledge Engine: full-text search (SQLite FTS5) over a curated corpus of \
libraries, skills, kits and tools.
- `search` ranks matching documents; `query` uses FTS5 syntax (words, "phrases", \
AND/OR/NOT, prefix*), `kind` restricts results to one entry kind, `limit` caps them. \
`mode` is `fts` (default), `vector` (semantic, needs the embedding index) or \
`hybrid` (both, fused by reciprocal rank).
- `registry_list` / `registry_get` inspect registry entries; `registry_toggle` \
enables or disables one (disabled entries drop out of the index on the next reindex).
- `project_context.*` and `board_*` tools are present when those subsystems are enabled.
Tool results are JSON text."""


# ── Optional tool groups ─────────────────────────────────────────────


@dataclass(frozen=True)
class ToolGroup:
    """A legacy tool group: JSON-schema definitions, dispatch map and call context."""

    name: str
    schemas: Sequence[Mapping[str, Any]]
    dispatch: Mapping[str, DispatchFn]
    context: Any = None


class GroupDisabledError(Exception):
    """A tool group is switched off by configuration."""


def load_project_context_group() -> ToolGroup:
    """``project_context.*`` tools, unless ``[project_context.mcp] enabled = false``."""
    from ..project_context.config import load_config  # noqa: PLC0415 - optional subsystem
    from ..project_context.mcp_tools import collect_tools  # noqa: PLC0415
    from ..project_context.mcp_tools.base import ToolContext  # noqa: PLC0415
    from ..project_context.paths import resolve_project_root  # noqa: PLC0415

    cfg = load_config()
    if not cfg.mcp.enabled:
        raise GroupDisabledError("disabled by config ([project_context.mcp] enabled = false)")
    schemas, dispatch = collect_tools(cfg)
    return ToolGroup(
        "project_context", schemas, dispatch, ToolContext(cfg=cfg, root=resolve_project_root())
    )


def load_agent_board_group() -> ToolGroup:
    """``board_*`` tools, with a context built from the persisted board config."""
    from ..agent_board.mcp_tools import collect_tools  # noqa: PLC0415 - optional subsystem
    from ..agent_board.mcp_tools.base import BoardContext  # noqa: PLC0415

    schemas, dispatch = collect_tools()
    return ToolGroup("agent_board", schemas, dispatch, BoardContext.from_config())


GROUP_LOADERS: tuple[tuple[str, Callable[[], ToolGroup]], ...] = (
    ("project_context", load_project_context_group),
    ("agent_board", load_agent_board_group),
)


def _register_optional_groups(server: MCPServer[Any]) -> None:
    for name, loader in GROUP_LOADERS:
        try:
            group = loader()
        except GroupDisabledError as exc:
            logger.warning("MCP tool group %r skipped: %s", name, exc)
            continue
        except Exception as exc:
            logger.warning(
                "MCP tool group %r unavailable, skipping: %s: %s",
                name,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            continue
        # Outside the try: a duplicate tool name is a programming error and must fail loudly.
        register_group(
            server, group.schemas, group.dispatch, group=group.name, context=group.context
        )


# ── Base tools ───────────────────────────────────────────────────────


def _empty_is_none(value: object) -> object:
    """Keep the pre-SDK contract: an empty ``kind`` means "no filter"."""
    return None if value == "" else value


def _embedding_error_result(exc: EmbeddingError) -> CallToolResult:
    """A model-visible, structured ``isError`` result for a vector/hybrid failure."""
    logger.warning("MCP search: %s (%s)", exc, exc.code)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"error": exc.to_dict()}, indent=2))],
        is_error=True,
    )


def _register_base_tools(
    server: MCPServer[Any], registry: Registry, indexer: Indexer, searcher: HybridSearcher
) -> None:
    # Names, parameters and descriptions are a public contract, frozen in
    # tests/snapshots/mcp_tools.json. Results are the same JSON text as before.

    def search(
        query: Annotated[str, Field(description="FTS5 query string")],
        limit: int = 10,
        kind: Annotated[
            EntryKind | None,
            BeforeValidator(_empty_is_none),
            Field(description="Optional kind filter"),
        ] = None,
        mode: Annotated[
            SearchMode,
            BeforeValidator(lambda value: DEFAULT_MODE if value in ("", None) else value),
            Field(
                description=(
                    "Search mode: fts (keyword, default), vector (semantic; needs the "
                    "embedding index) or hybrid (both, fused by reciprocal rank)"
                )
            ),
        ] = DEFAULT_MODE,
    ) -> CallToolResult:
        """Full-text search across enabled libraries, skills, kits, and tools in the corpus."""
        with tool_errors("search"):
            if limit < 1:
                raise ValueError("limit must be a positive integer")
            try:
                hits = searcher.search(query, mode=mode, limit=limit, kind=kind)
            except EmbeddingError as exc:
                return _embedding_error_result(exc)
            meta = getattr(hits, "meta", None) or None
            # Same JSON text as always; an FTS5 syntax fallback (and a non-default
            # mode) is flagged in `_meta`.
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(list(hits), indent=2))],
                _meta=meta,
            )

    def registry_list(
        kind: Annotated[EntryKind | None, BeforeValidator(_empty_is_none)] = None,
        enabled_only: bool = False,
    ) -> str:
        """List entries in the registry."""
        with tool_errors("registry_list"):
            return json.dumps(registry.list(kind=kind, enabled_only=enabled_only), indent=2)

    def registry_toggle(entry_id: str, enabled: bool) -> str:
        """Enable or disable a registry entry by id."""
        with tool_errors("registry_toggle"):
            row = registry.set_enabled(entry_id, enabled)
            return json.dumps({"updated": row is not None, "entry": row})

    def registry_get(entry_id: str) -> str:
        """Fetch a single registry entry by id."""
        with tool_errors("registry_get"):
            return json.dumps(registry.get(entry_id), indent=2)

    base = (search, registry_list, registry_toggle, registry_get)
    # BASE_TOOLS is the public list (CLI `capabilities`, selftest); keep it honest.
    if tuple(fn.__name__ for fn in base) != BASE_TOOLS:  # pragma: no cover - programming error
        raise RuntimeError("mcp.server.BASE_TOOLS is out of sync with the registered base tools")
    for fn in base:
        server.add_tool(fn, structured_output=False)


# ── Server factory ───────────────────────────────────────────────────


@contextmanager
def _host_logging_untouched() -> Iterator[None]:
    """Stop ``MCPServer()`` from configuring the host's logging.

    The SDK constructor calls ``logging.basicConfig`` (a Rich handler at INFO)
    when the root logger has no handlers. A library must leave that to the
    application, so give ``basicConfig`` a placeholder to find and remove it
    afterwards. The stdio entry point configures logging itself in ``main``.
    """
    root = logging.getLogger()
    if root.handlers:
        yield
        return
    placeholder = logging.NullHandler()
    root.addHandler(placeholder)
    try:
        yield
    finally:
        root.removeHandler(placeholder)


def _default_embeddings() -> tuple[CorpusEmbeddingIndex | None, str | None]:
    """The corpus embedding index from the environment (no I/O), or ``(None, reason)``."""
    from ..core.settings import Settings  # noqa: PLC0415 - read when the server is built
    from ..retrieval.embeddings.index import open_index  # noqa: PLC0415

    return open_index(Settings())


def build_server(
    config: Config,
    registry: Registry,
    indexer: Indexer,
    *,
    include_groups: bool = True,
    embeddings: CorpusEmbeddingIndex | None = None,
    embeddings_error: str | None = None,
) -> MCPServer[Any]:
    """Build the ``knowledge-engine`` MCP server around existing engine objects.

    Registers the four base tools and, when ``include_groups`` is true, the
    optional project-context and agent-board tool groups. A group that is
    disabled or fails to initialise is logged and skipped; a duplicate tool
    name raises ``ValueError``.

    ``embeddings`` is the corpus embedding index behind ``search``'s
    ``vector``/``hybrid`` modes; when neither it nor ``embeddings_error`` (why
    it is unavailable) is given, it is configured from the environment.
    """
    if embeddings is None and embeddings_error is None:
        embeddings, embeddings_error = _default_embeddings()
    searcher = HybridSearcher(indexer, registry, embeddings, embeddings_error=embeddings_error)
    with _host_logging_untouched():
        server: MCPServer[Any] = MCPServer(
            SERVER_NAME, version=__version__, instructions=INSTRUCTIONS
        )
    _register_base_tools(server, registry, indexer, searcher)
    if include_groups:
        _register_optional_groups(server)
    logger.info("MCP server %s %s ready (corpus %s)", SERVER_NAME, __version__, config.corpus_root)
    return server


# ── Entry points ─────────────────────────────────────────────────────


def configure_logging() -> None:
    """Log to stderr at ``KE_LOG_LEVEL`` (default INFO); stdout is the transport.

    A no-op when the host already configured logging (``knowledge-engine mcp``
    does, once, in the CLI's ``main``); an invalid level is reported by the
    settings layer and replaced by INFO.
    """
    from ..core.logging_config import CLI_FORMAT, resolve_log_level  # noqa: PLC0415

    logging.basicConfig(stream=sys.stderr, level=resolve_log_level(), format=CLI_FORMAT)


async def selftest(server: MCPServer[Any]) -> dict[str, Any]:
    """Connect an in-process client, list the tools and summarise the result."""
    async with Client(server) as client:
        listed = await client.list_tools()
        names = [tool.name for tool in listed.tools]
        protocol_version = client.protocol_version
    missing = [name for name in BASE_TOOLS if name not in names]
    return {
        "ok": not missing,
        "server": server.name,
        "version": server.version,
        "protocol_version": protocol_version,
        "tool_count": len(names),
        "missing_base_tools": missing,
        "tools": names,
    }


def run(args: argparse.Namespace) -> int:
    """Run the server as selected by the ``knowledge-engine mcp`` flags."""
    from ..runtime.foundation.config import load_dotenv_file  # noqa: PLC0415 - entry point only

    configure_logging()
    load_dotenv_file()
    config = Config.from_env()
    # The Indexer opens its SQLite file eagerly; a fresh KE_DATA_DIR must exist
    # first (Config.from_env() itself never touches the filesystem).
    config.ensure_dirs()
    try:
        registry = Registry(config.registry_path)
    except RegistryValidationError as exc:
        # The registry already logged every schema error; stdout stays clean
        # (it is the stdio transport) and the client sees a failed start.
        logger.error(
            "MCP server not started: %s %s (check it with `knowledge-engine registry validate`)",
            exc.path,
            exc.message,
        )
        return 1
    try:
        indexer = Indexer(config, registry)
    except BaseException:
        registry.close()
        raise
    try:
        return _serve(args, build_server(config, registry, indexer))
    finally:
        _shutdown(registry, indexer)


def _shutdown(registry: Registry, indexer: Indexer) -> None:
    """Close every handle the server opened (index, registry, pipeline/board DBs)."""
    for name, resource in (("index", indexer), ("registry", registry)):
        try:
            resource.close()
        except Exception:
            logger.exception("closing the %s failed", name)
    try:
        from ..runtime.foundation import db as pipeline_db  # noqa: PLC0415 - optional substrate

        pipeline_db.close_all()
    except Exception:
        logger.exception("closing the pipeline DB connections failed")
    logger.debug("MCP server shut down; connections closed")


def _serve(args: argparse.Namespace, server: MCPServer[Any]) -> int:
    if args.selftest:
        report = anyio.run(selftest, server)
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        if not report["ok"]:
            logger.error("MCP selftest failed: missing base tools %s", report["missing_base_tools"])
            return 1
        return 0

    try:
        if args.http:
            logger.info(
                "serving MCP over streamable HTTP at http://%s:%d/mcp", args.host, args.port
            )
            server.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                transport_security=transport_security(args.host),
            )
        else:
            logger.info("serving MCP over stdio")
            server.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("MCP server interrupted; shutting down")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m knowledge_engine.mcp.server [--http [--host H] [--port P] | --selftest]``."""
    parser = argparse.ArgumentParser(
        prog="knowledge-engine mcp", description="Run the knowledge-engine MCP server."
    )
    add_arguments(parser)
    args = parser.parse_args(argv)
    resolve_http_options(parser, args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
