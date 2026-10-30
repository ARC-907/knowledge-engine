"""``knowledge-engine serena ...``: the optional, read-only Serena adapter.

Every command prints JSON on stdout. ``status`` never starts Serena;
``check`` starts it for each configured project and verifies it against the
recorded witness; ``sync`` registers the broker sources; ``find-symbol``,
``overview`` and ``references`` run the read-only tools. Typed failures are
printed on stderr as ``error[<code>]: <message>``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import CommandError


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


@contextmanager
def _manager() -> Iterator[Any]:
    from ..knowledge.errors import CLI_EXIT_CODES, KnowledgeError
    from ..serena.config import SerenaConfig
    from ..serena.manager import SerenaManager

    manager = None
    try:
        config = SerenaConfig.from_env()
        if not config.enabled:
            raise CommandError(
                "error[unavailable]: the Serena adapter is disabled (set KE_SERENA_ENABLED=1)",
                CLI_EXIT_CODES.get("unavailable", 1),
            )
        manager = SerenaManager(config, start_reaper=False)
        yield manager
    except KnowledgeError as exc:
        raise CommandError(
            f"error[{exc.code}]: {exc.message}", CLI_EXIT_CODES.get(exc.code, 1)
        ) from exc
    finally:
        if manager is not None:
            manager.close()


def cmd_serena(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """Every ``serena <verb>`` command."""
    from ..serena.manager import run_tool

    verb = args.serena_cmd
    with _manager() as manager:
        if verb == "status":
            _emit(manager.status())
            return 0
        if verb == "check":
            report = manager.check(args.project_id)
            _emit(report)
            return 0 if report["ok"] else 1
        if verb == "sync":
            _emit(_sync(manager))
            return 0
        if verb == "find-symbol":
            arguments = {
                "project_id": args.project_id,
                "name_path_pattern": args.name_path_pattern,
                "relative_path": args.relative_path,
                "substring_matching": args.substring,
                "include_body": args.body,
                "max_matches": args.max_matches,
            }
            _emit(run_tool(manager, "serena.find_symbol", arguments))
        elif verb == "overview":
            arguments = {"project_id": args.project_id, "relative_path": args.relative_path}
            _emit(run_tool(manager, "serena.get_symbols_overview", arguments))
        elif verb == "references":
            arguments = {
                "project_id": args.project_id,
                "name_path": args.name_path,
                "relative_path": args.relative_path,
                "max_results": args.max_results,
            }
            _emit(run_tool(manager, "serena.find_referencing_symbols", arguments))
    return 0


def _sync(manager: Any) -> dict[str, Any]:
    from ..knowledge.config import KnowledgeConfig
    from ..knowledge.store import KnowledgeStore
    from ..serena.adapter import register_sources

    config = KnowledgeConfig.from_env()
    with KnowledgeStore.open(config.db_path) as store:
        return register_sources(store, manager.config)
