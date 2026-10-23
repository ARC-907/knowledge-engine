"""Server subcommands: ``serve`` (engine HTTP app) and ``board-serve`` (standalone board)."""

from __future__ import annotations

import argparse
import logging
import os

_logger = logging.getLogger(__name__)


def cmd_serve(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..core.settings import Settings

    settings = Settings()
    port = args.port if args.port is not None else settings.port or 9210
    try:
        import uvicorn
    except ImportError:
        _logger.error("uvicorn not installed; pip install knowledge-engine")
        return 1
    from ..core.http import uvicorn_proxy_kwargs
    from ..core.logging_config import uvicorn_log_config

    # The factory is called by uvicorn without arguments; the bind address
    # travels through the environment so the optional MCP mount can set its
    # Host-header policy (see server/lifespan.py: BIND_HOST_ENV).
    os.environ["KE_BIND_HOST"] = args.host
    settings = settings.model_copy(update={"bind_host": args.host})
    # Logging: uvicorn's default config wires only its own loggers, so the
    # engine's INFO policy lines would never be emitted. Proxy headers:
    # uvicorn trusts loopback X-Forwarded-For by default, which would
    # contradict KE_TRUST_PROXY; derive both from the same env vars.
    level = getattr(args, "log_level_override", None) or settings.log_level
    uvicorn.run(
        "knowledge_engine.app:create_app",
        host=args.host,
        port=port,
        factory=True,
        log_config=uvicorn_log_config(level),
        **uvicorn_proxy_kwargs(settings=settings),
    )
    return 0


def cmd_board_serve(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..agent_board.service import serve as board_serve
    from ..core.settings import Settings

    # The removed serve-board.sh honoured BOARD_HOST/BOARD_PORT; keep those knobs.
    settings = Settings()
    host = args.host or settings.board_serve_host or "127.0.0.1"
    port = args.port
    if port is None:
        port = settings.board_serve_port or settings.board_standalone_port
    override = getattr(args, "log_level_override", None)
    if override:
        return board_serve(host=host, port=port, log_level=override)
    return board_serve(host=host, port=port)
