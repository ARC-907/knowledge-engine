"""``knowledge-engine knowledge ...``: scoped-knowledge metadata (migrate, status, admin, search).

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


def _invoke(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..knowledge.service import KnowledgeService, invoke
    from ..knowledge.store import KnowledgeStore

    config = _config()
    with _typed_errors(), KnowledgeStore.open(config.db_path) as store:
        service = KnowledgeService(store, profile=config.profile, evidence_policy=config.evidence)
        return invoke(service, operation, arguments)


def _run(operation: str, arguments: dict[str, Any]) -> int:
    _emit(_invoke(operation, arguments))
    return 0


def _json_arg(text: str | None, flag: str) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CommandError(f"{flag} is not valid JSON: {exc}", 2) from exc


def _read_json_input(path: str) -> dict[str, Any]:
    import sys

    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"cannot read {path}: {exc}", 2) from exc
    data = _json_arg(text, path)
    if not isinstance(data, dict):
        raise CommandError(f"{path} must hold a JSON object", 2)
    return data


def cmd_knowledge_snapshot(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge snapshot --dry-run|--apply [--delete-missing]``."""
    from ..knowledge.evidence.snapshot import snapshot
    from ..knowledge.evidence.store import EvidenceStore
    from ..knowledge.store import KnowledgeStore

    config = _config()
    if not config.db_path.is_file():
        raise CommandError(
            "error[unknown_scope]: the knowledge store does not exist; run "
            "`knowledge-engine knowledge migrate --apply` first",
            2,
        )
    with _typed_errors(), KnowledgeStore.open(config.db_path) as store:
        report = snapshot(
            EvidenceStore(store, config.evidence),
            corpus_root=config.corpus_root,
            registry_path=config.registry_path,
            apply=bool(args.apply),
            delete_missing=bool(args.delete_missing),
        )
    _emit(report)
    return 0


def cmd_knowledge_evidence_open(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge evidence open`` (scope from --context or --workspace-id/--project)."""
    context = _json_arg(args.context, "--context")
    if context is None:
        context = _clean(
            workspace_id=args.workspace_id or _config().default_workspace,
            project_id=args.project,
        )
    arguments = _clean(
        context=context,
        evidence_id=args.evidence_id,
        source_id=args.source_id,
        record_id=args.record_id,
        revision_id=args.revision_id,
        locator=_json_arg(args.locator, "--locator"),
    )
    return _run("evidence_open", arguments)


def cmd_knowledge_evidence_delete(
    args: argparse.Namespace, _parser: argparse.ArgumentParser
) -> int:
    """``knowledge evidence delete SOURCE RECORD --reason R --yes``."""
    from ..knowledge.evidence.store import EvidenceStore
    from ..knowledge.store import KnowledgeStore

    if not args.yes:
        raise CommandError("refusing to delete without --yes", 2)
    config = _config()
    with _typed_errors(), KnowledgeStore.open(config.db_path) as store:
        result = EvidenceStore(store, config.evidence).delete_record(
            args.source_id, args.record_id, reason=args.reason
        )
    _emit(result)
    return 0


def cmd_knowledge_cite_validate(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge cite validate FILE``: the validation report (JSON)."""
    payload = _read_json_input(args.input)
    payload.pop("format", None)
    return _run("cite_validate", payload)


def cmd_knowledge_cite_export(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge cite export FILE [--format F] [--raw]``."""
    payload = _read_json_input(args.input)
    if args.format:
        payload["format"] = args.format
    result = _invoke("cite_export", payload)
    if not args.raw:
        _emit(result)
        return 0
    if result.get("markdown") is not None:
        print(result["markdown"], end="")
    if result.get("csl_json") is not None:
        if result.get("markdown") is not None:
            print()
        print(json.dumps(result["csl_json"], indent=2, ensure_ascii=False))
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
    "project_context_link": lambda a: (
        "project_context_link",
        _clean(
            workspace_id=a.workspace_id,
            project_id=a.project_id,
            root=a.root,
            project_fp=a.project_fp,
        ),
    ),
    "project_context_unlink": lambda a: (
        "project_context_unlink",
        {"workspace_id": a.workspace_id, "project_id": a.project_id},
    ),
    "evidence_revisions": lambda a: (
        "evidence_revisions",
        {"source_id": a.source_id, "record_id": a.record_id},
    ),
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


def _scope_context(args: argparse.Namespace, **extra: Any) -> dict[str, Any]:
    """The query context of the scope flags (or ``--context``), plus non-empty ``extra``."""
    args.workspace_id = args.workspace
    return {**_context(args), **_clean(**extra)}


def _with_broker(run: Callable[[Any], dict[str, Any]]) -> int:
    from ..knowledge.broker_api import BrokerEngine

    with _typed_errors():
        engine = BrokerEngine.open()
        try:
            result = run(engine.broker)
        finally:
            engine.close()
    _emit(result)
    return 0


def cmd_knowledge_search(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge search QUERY [scope flags] [--mode] [--limit] ...``: the broker envelope."""
    from ..knowledge.broker_api import run_search

    context = _scope_context(
        args,
        mode=args.mode,
        limit=args.limit,
        timeout_ms=args.timeout_ms,
        byte_budget=args.byte_budget,
    )
    payload = {"query": args.query, "context": context}
    return _with_broker(lambda broker: run_search(broker, payload))


def cmd_knowledge_open(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge open SOURCE_ID RECORD_ID [--revision REV] [scope flags]``."""
    from ..knowledge.broker_api import run_open

    payload = _clean(
        context=_scope_context(args, timeout_ms=args.timeout_ms),
        source_id=args.source_id,
        record_id=args.record_id,
        revision_id=args.revision,
    )
    return _with_broker(lambda broker: run_open(broker, payload))


def cmd_knowledge_admin(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """Every ``knowledge <object> <verb>`` admin command."""
    operation, arguments = HANDLERS[args.knowledge_op](args)
    return _run(operation, arguments)


__all__ = [
    "HANDLERS",
    "cmd_knowledge_admin",
    "cmd_knowledge_cite_export",
    "cmd_knowledge_cite_validate",
    "cmd_knowledge_evidence_delete",
    "cmd_knowledge_evidence_open",
    "cmd_knowledge_migrate",
    "cmd_knowledge_open",
    "cmd_knowledge_search",
    "cmd_knowledge_snapshot",
    "cmd_knowledge_status",
]
