"""``knowledge-engine knowledge ...``: scoped-knowledge metadata (migrate, status, admin).

Every command prints JSON on stdout. Admin commands go through the same
operation catalog as the ``/knowledge`` HTTP routes and the ``knowledge_*``
MCP tools (:mod:`knowledge_engine.knowledge.service`). A typed failure is
printed on stderr as ``error[<code>]: <message>`` and exits with the code's
status (:data:`knowledge_engine.knowledge.errors.CLI_EXIT_CODES`).
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import CommandError

_logger = logging.getLogger(__name__)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _config() -> Any:
    from ..knowledge.config import KnowledgeConfig

    return KnowledgeConfig.from_env()


@contextmanager
def _typed_errors() -> Iterator[None]:
    from ..knowledge.errors import CLI_EXIT_CODES, KnowledgeError

    try:
        yield
    except KnowledgeError as exc:
        _logger.debug("knowledge command failed", exc_info=True)
        detail = ""
        errors = exc.details.get("errors")
        if isinstance(errors, list) and errors:
            detail = " (" + "; ".join(str(e) for e in errors[:5]) + ")"
        raise CommandError(
            f"error[{exc.code}]: {exc.message}{detail}", CLI_EXIT_CODES.get(exc.code, 1)
        ) from exc


def cmd_knowledge_migrate(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge migrate --dry-run|--apply [--backup-dir DIR] [--batch-size N] [--max-batches N]``."""
    from ..knowledge import backfill

    config = _config()
    common = {
        "corpus_root": config.corpus_root,
        "registry_path": config.registry_path,
        "db_path": config.db_path,
        "workspace_id": config.default_workspace,
        "backup_dir": Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None,
    }
    with _typed_errors():
        if args.apply:
            report = backfill.apply(
                **common, batch_size=args.batch_size, max_batches=args.max_batches
            )
        else:
            report = backfill.plan(**common)
    _emit(report)
    return 0


def cmd_knowledge_status(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge status``: counts, schema version and last backfill (read-only)."""
    from ..knowledge.service import KnowledgeService
    from ..knowledge.store import KnowledgeStore, latest_schema_version

    config = _config()
    if not config.db_path.is_file():
        _emit(
            {
                "store_exists": False,
                "latest_schema_version": latest_schema_version(),
                "hint": "run `knowledge-engine knowledge migrate --dry-run`, then --apply",
            }
        )
        return 0
    with _typed_errors(), KnowledgeStore.open_readonly(config.db_path) as store:
        status = KnowledgeService(store, profile=config.profile).status().model_dump(mode="json")
    _emit({"store_exists": True, **status})
    return 0


def _run(operation: str, arguments: dict[str, Any]) -> int:
    from ..knowledge.service import KnowledgeService, invoke
    from ..knowledge.store import KnowledgeStore

    config = _config()
    with _typed_errors(), KnowledgeStore.open(config.db_path) as store:
        result = invoke(KnowledgeService(store, profile=config.profile), operation, arguments)
    _emit(result)
    return 0


def _clean(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _records(pairs: list[list[str]] | None) -> list[dict[str, str]]:
    return [{"source_id": s, "record_id": r} for s, r in (pairs or [])]


def _context(args: argparse.Namespace) -> dict[str, Any]:
    if args.context:
        try:
            context = json.loads(args.context)
        except ValueError as exc:
            raise CommandError(f"--context is not valid JSON: {exc}", 2) from exc
        if not isinstance(context, dict):
            raise CommandError("--context must be a JSON object", 2)
        return context
    return _clean(
        workspace_id=args.workspace_id or _config().default_workspace,
        project_id=args.project,
        profile_id=args.profile,
        include_workspace=True if args.include_workspace else None,
        source_ids=args.source,
        collection_ids=args.collection,
        research_types=args.type,
        tags=args.tag,
    )


HANDLERS: dict[str, Callable[[argparse.Namespace], tuple[str, dict[str, Any]]]] = {
    "workspace_list": lambda a: ("workspace_list", {}),
    "workspace_create": lambda a: (
        "workspace_create",
        _clean(display_name=a.name, workspace_id=a.id, description=a.description),
    ),
    "project_list": lambda a: ("project_list", {"workspace_id": a.workspace_id}),
    "project_create": lambda a: (
        "project_create",
        _clean(
            workspace_id=a.workspace_id,
            display_name=a.name,
            project_id=a.id,
            description=a.description,
        ),
    ),
    "source_list": lambda a: ("source_list", {"workspace_id": a.workspace_id}),
    "collection_list": lambda a: (
        "collection_list",
        _clean(workspace_id=a.workspace_id, project_id=a.project),
    ),
    "collection_create": lambda a: (
        "collection_create",
        _clean(
            workspace_id=a.workspace_id,
            display_name=a.name,
            collection_id=a.id,
            project_id=a.project,
            description=a.description,
        ),
    ),
    "collection_add": lambda a: (
        "collection_add",
        {"collection_id": a.collection_id, "records": _records(a.record)},
    ),
    "collection_remove": lambda a: (
        "collection_remove",
        {"collection_id": a.collection_id, "records": _records(a.record)},
    ),
    "record_classify": lambda a: (
        "record_classify",
        _clean(
            source_id=a.source_id,
            record_id=a.record_id,
            research_type=a.type,
            curation_status=a.status,
            tags=a.set_tag,
            add_tags=a.add_tag,
            remove_tags=a.remove_tag,
            project_id=a.project,
            clear_project=True if a.clear_project else None,
        ),
    ),
    "record_list": lambda a: (
        "record_query",
        {"context": _context(a), "limit": a.limit, "offset": a.offset},
    ),
    "profile_list": lambda a: ("profile_list", {"workspace_id": a.workspace_id}),
    "profile_set": lambda a: (
        "profile_set",
        {
            "profile": _clean(
                workspace_id=a.workspace_id,
                profile_id=a.profile_id,
                description=a.description,
                source_ids=a.source,
                collection_ids=a.collection,
                research_types=a.type,
                tags=a.tag,
            )
        },
    ),
}


def cmd_knowledge_admin(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """Every ``knowledge <object> <verb>`` admin command."""
    operation, arguments = HANDLERS[args.knowledge_op](args)
    return _run(operation, arguments)


__all__ = ["HANDLERS", "cmd_knowledge_admin", "cmd_knowledge_migrate", "cmd_knowledge_status"]
