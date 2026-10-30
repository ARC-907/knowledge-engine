"""Application lifespan: builds and tears down every engine resource.

``create_app`` does no I/O; this lifespan does it all, in order:

0. load ``<KE_PIPELINE_ROOT>/.env`` (``runtime.foundation.config.load_dotenv_file``;
   variables already in the environment win);
1. resolve the paths from ``app.state.settings``, create the data directory,
   load the registry and open the FTS index (``app.state.config``,
   ``registry``, ``indexer``);
2. describe the corpus embedding index for ``GET /search?mode=vector|hybrid``
   (``app.state.embeddings``; no I/O -- the index is opened per search). A
   provider that cannot be configured leaves it ``None`` with the reason in
   ``app.state.embeddings_error``; ``fts`` search is unaffected;
3. build the generation providers (``app.state.providers``) and, unless
   ``KE_PROVIDER_STARTUP_PROBE=0``, probe them on a background thread so an
   unreachable provider is reported at WARNING without delaying startup;
4. board (unless ``KE_BOARD_ENABLED=0``): log the trust policy, warn about
   scope databases whose names are no longer valid scopes, create the
   app's :class:`~knowledge_engine.agent_board.sweeper.Sweeper`
   (``app.state.sweeper``), install it as the process default and start it
   when ``KE_BOARD_SWEEPER`` / the persisted flag say so;
5. scoped knowledge (unless ``KE_KNOWLEDGE_ENABLED=0``): open ``knowledge.db``
   (applying its migrations) for the ``/knowledge`` routes
   (``app.state.knowledge``) and build the retrieval broker over it and the
   registry/FTS/embedding objects above (``app.state.broker``, for
   ``/knowledge/search`` and ``/knowledge/open``); a failure is logged and
   those routes answer ``503``, the rest of the app is unaffected;
6. modules (unless ``KE_MODULES_ENABLED=0``): open ``modules.db``, rebuild
   observed module health and install the host as the process default
   (``app.state.modules``) so ``/modules`` and the MCP module tools share one
   dispatcher; a failure is logged and ``/modules`` answers ``503``;
7. MCP over HTTP (``KE_MCP_HTTP=1``): attach a server built around this
   lifespan's registry/indexer/embedding index to the mount and run its
   session manager.

On shutdown the sweeper is stopped (and the previous default restored), the
MCP session manager exits, the registry and the FTS connection are closed,
the knowledge store is closed, every cached pipeline/board DB connection is closed
(``runtime.foundation.db.close_all``), and the engine objects are removed
from ``app.state`` so a request that still arrives gets ``503`` from
``server.deps`` instead of touching a closed handle. Optional
subsystems (board, MCP, provider probe) are wrapped so a failure there is
logged and never blocks the core app; a failure to open the core databases
aborts startup.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from ..core.config import Config
from ..core.registry import Registry
from ..core.settings import Settings, env_flag
from ..generation import ProviderRegistry
from ..generation.factory import build_provider_registry
from ..retrieval.embeddings.index import CorpusEmbeddingIndex, open_index
from ..retrieval.fts5 import Indexer
from ..runtime.foundation.config import load_dotenv_file

if TYPE_CHECKING:
    from ..agent_board.sweeper import Sweeper

_logger = logging.getLogger(__name__)

#: The address the host app is bound to. ``knowledge-engine serve`` sets it
#: from ``--host``; set it yourself when running uvicorn directly. The app
#: factory takes no arguments under uvicorn, so this is how optional mounts
#: (the MCP endpoint) learn the bind address for their Host-header policy.
BIND_HOST_ENV = "KE_BIND_HOST"
DEFAULT_BIND_HOST = "127.0.0.1"
PROBE_THREAD_NAME = "ke-provider-probe"


def env_truthy(name: str, default: bool) -> bool:
    """Compatibility helper: parse env var ``name`` with the shared flag parser."""
    import os  # noqa: PLC0415 - compatibility path only

    return env_flag(os.environ.get(name), default)


def bind_host(settings: Settings | None = None) -> str:
    """The address the app is bound to (``KE_BIND_HOST``, default loopback)."""
    return (settings if settings is not None else Settings()).bind_host


def _app_settings(app: FastAPI) -> Settings:
    settings = getattr(app.state, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings()
        app.state.settings = settings
    return settings


def open_engine(settings: Settings) -> tuple[Config, Registry, Indexer]:
    """Resolve paths, create the data dir and open the registry + FTS index."""
    config = Config.from_settings(settings)
    try:
        config.ensure_dirs()
        registry = Registry(config.registry_path)
    except Exception:
        _logger.exception(
            "could not open the registry (registry=%s, data_dir=%s)",
            config.registry_path,
            config.data_dir,
        )
        raise
    try:
        indexer = Indexer(config, registry)
    except Exception:
        _logger.exception("could not open the FTS index in %s", config.data_dir)
        registry.close()
        raise
    return config, registry, indexer


def open_embeddings(settings: Settings) -> tuple[CorpusEmbeddingIndex | None, str | None]:
    """The corpus embedding index described by ``settings`` (no I/O), or ``(None, reason)``."""
    return open_index(settings)


def _start_provider_probe(providers: ProviderRegistry) -> threading.Thread:
    """Probe every provider once, off the event loop; warn about unreachable ones."""

    def run() -> None:
        try:
            providers.probe_all(warn=True)
        except Exception:
            _logger.exception("startup provider probe failed")

    thread = threading.Thread(target=run, name=PROBE_THREAD_NAME, daemon=True)
    thread.start()
    return thread


def _start_board(app: FastAPI, settings: Settings) -> tuple[Sweeper | None, Sweeper | None]:
    """Log the trust policy and start the app's sweeper. Returns (owned, previous default)."""
    try:
        # Optional subsystem, imported lazily so a broken board never blocks boot.
        from ..agent_board import http as board_http  # noqa: PLC0415

        board_http.log_trust_policy(settings)
    except Exception:
        _logger.exception("could not log board trust policy; continuing")
    try:
        from ..agent_board import scopes as board_scopes  # noqa: PLC0415

        board_scopes.warn_nonconforming_scopes()
    except Exception:
        _logger.exception("could not check board scope names; continuing")
    try:
        from ..agent_board import sweeper as board_sweeper  # noqa: PLC0415

        owned = board_sweeper.Sweeper(settings=settings)
        app.state.sweeper = owned
        previous = board_sweeper.install_default(owned)
    except Exception:
        _logger.exception("board sweeper could not be created; continuing")
        return None, None
    try:
        if owned.should_run():
            owned.start()
    except Exception:
        _logger.exception("board sweeper failed to start; continuing")
    return owned, previous


def _stop_board(owned: Sweeper | None, previous: Sweeper | None) -> None:
    if owned is None:
        return
    try:
        owned.stop()
    except Exception:
        _logger.exception("board sweeper failed to stop cleanly")
    try:
        from ..agent_board import sweeper as board_sweeper  # noqa: PLC0415

        board_sweeper.install_default(previous)
    except Exception:
        _logger.exception("could not restore the default board sweeper")


async def _start_mcp(
    app: FastAPI, stack: AsyncExitStack, config: Config, registry: Registry, indexer: Indexer
) -> None:
    embeddings = getattr(app.state, "embeddings", None)
    embeddings_error = getattr(app.state, "embeddings_error", None)
    mcp_http = getattr(app.state, "mcp_http", None)
    if mcp_http is None:
        return
    try:
        # Imported lazily: the MCP SDK is only loaded when the endpoint is enabled.
        from ..mcp import server as mcp_server  # noqa: PLC0415

        mcp_http.attach(
            mcp_server.build_server(
                config,
                registry,
                indexer,
                embeddings=embeddings,
                embeddings_error=embeddings_error,
            )
        )
        await stack.enter_async_context(mcp_http.lifespan())
    except Exception:
        _logger.exception("MCP session manager failed to start; /mcp will answer 503")


def _open_knowledge(app: FastAPI, settings: Settings) -> None:
    """Open the scoped-knowledge store for ``/knowledge`` (optional; failures are logged)."""
    try:
        from ..knowledge.config import KnowledgeConfig  # noqa: PLC0415 - optional subsystem
        from ..knowledge.service import KnowledgeService  # noqa: PLC0415
        from ..knowledge.store import KnowledgeStore  # noqa: PLC0415

        config = KnowledgeConfig.from_settings(settings)
        app.state.knowledge = KnowledgeService(
            KnowledgeStore.open(config.db_path),
            profile=config.profile,
            evidence_policy=config.evidence,
        )
    except Exception:
        _logger.exception("knowledge store could not be opened; /knowledge will answer 503")
        app.state.knowledge = None
        return
    try:
        from ..knowledge.broker_api import build_broker  # noqa: PLC0415 - optional subsystem

        app.state.broker = build_broker(
            app.state.knowledge.store,
            app.state.registry,
            app.state.indexer,
            corpus_root=app.state.config.corpus_root,
            embeddings=getattr(app.state, "embeddings", None),
            embeddings_error=getattr(app.state, "embeddings_error", None),
            profile=config.profile,
            evidence_policy=config.evidence,
        )
    except Exception:
        _logger.exception("retrieval broker could not be built; /knowledge/search will answer 503")
        app.state.broker = None


def _open_modules(app: FastAPI, settings: Settings) -> None:
    """Open the module host for ``/modules`` and the MCP module tools (failures are logged)."""
    app.state.modules = None
    app.state.previous_module_host = None
    try:
        from ..modules.config import ModulesConfig  # noqa: PLC0415 - optional subsystem
        from ..modules.dispatcher import ModuleHost, set_default_host  # noqa: PLC0415

        host = ModuleHost.open(ModulesConfig.from_settings(settings))
        host.start()
        app.state.modules = host
        app.state.previous_module_host = set_default_host(host)
    except Exception:
        _logger.exception("module catalog could not be opened; /modules will answer 503")


def _close_modules(app: FastAPI) -> None:
    host = getattr(app.state, "modules", None)
    if host is None:
        return
    try:
        from ..modules.dispatcher import set_default_host  # noqa: PLC0415

        set_default_host(getattr(app.state, "previous_module_host", None))
        host.close()
    except Exception:
        _logger.exception("closing the module host failed")
    app.state.modules = None


#: ``app.state`` attributes the lifespan owns; cleared on shutdown so the
#: ``server.deps`` providers answer 503 afterwards.
ENGINE_STATE = ("indexer", "registry", "providers", "config", "knowledge", "broker")


def _close_engine(app: FastAPI) -> None:
    broker = getattr(app.state, "broker", None)
    if broker is not None:
        try:
            broker.close()
        except Exception:
            _logger.exception("closing the retrieval broker failed")
    knowledge = getattr(app.state, "knowledge", None)
    if knowledge is not None:
        try:
            knowledge.store.close()
        except Exception:
            _logger.exception("closing the knowledge store failed")
    for name in ("indexer", "registry"):
        resource = getattr(app.state, name, None)
        if resource is None:
            continue
        try:
            resource.close()
        except Exception:
            _logger.exception("closing %s failed", name)
    for name in ENGINE_STATE:
        setattr(app.state, name, None)
    try:
        from ..runtime.foundation import db as pipeline_db  # noqa: PLC0415 - optional substrate

        pipeline_db.close_all()
    except Exception:
        _logger.exception("closing the pipeline DB connections failed")
    if getattr(app.state, "mcp_http", None) is None:
        return
    try:
        from ..mcp.server import close_knowledge_stores  # noqa: PLC0415 - MCP mounted

        close_knowledge_stores()
    except Exception:
        _logger.exception("closing the MCP knowledge stores failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the engine on startup and release everything on shutdown."""
    # The pipeline .env used to be loaded as an import side effect of the
    # board substrate; load it explicitly (the real environment always wins).
    load_dotenv_file()
    settings = _app_settings(app)
    config, registry, indexer = open_engine(settings)
    app.state.config = config
    app.state.registry = registry
    app.state.indexer = indexer
    app.state.embeddings, app.state.embeddings_error = open_embeddings(settings)
    owned_sweeper: Sweeper | None = None
    previous_sweeper: Sweeper | None = None
    try:
        providers = build_provider_registry(settings)
        app.state.providers = providers
        if settings.provider_startup_probe:
            _start_provider_probe(providers)
        if settings.board_enabled:
            owned_sweeper, previous_sweeper = _start_board(app, settings)
        if settings.knowledge_enabled:
            _open_knowledge(app, settings)
        if settings.modules_enabled:
            _open_modules(app, settings)
        async with AsyncExitStack() as optional_services:
            await _start_mcp(app, optional_services, config, registry, indexer)
            yield
    finally:
        _stop_board(owned_sweeper, previous_sweeper)
        _close_modules(app)
        _close_engine(app)
        _logger.info("engine shut down; registry, index and pipeline DB connections closed")


__all__ = [
    "BIND_HOST_ENV",
    "DEFAULT_BIND_HOST",
    "bind_host",
    "env_truthy",
    "lifespan",
    "open_embeddings",
    "open_engine",
]
