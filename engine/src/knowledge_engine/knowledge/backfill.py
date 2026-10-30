"""Register the existing corpus in ``knowledge.db``: dry run, backup, resumable apply.

``knowledge-engine knowledge migrate --dry-run`` (:func:`plan`) reads the
registry, walks every entry's files exactly like the FTS indexer does
(``.md``/``.txt``/``.rst`` below the entry path) and reports what an apply
would do -- record counts, files reachable from several (nested) entries,
metadata that will be defaulted, missing entry content, records whose file
is gone and the disk space needed -- without creating or writing anything.

``--apply`` (:func:`apply`):

1. plans again and refuses to run without enough free disk space;
2. backs up the authoritative stores it touches *before* any write:
   ``knowledge.db`` (SQLite online backup, when it exists) and a copy of
   ``registry.json``, plus a ``manifest.json`` with the schema versions and
   the plan;
3. opens ``knowledge.db`` (applying its numbered migrations), creates the
   configured default workspace and registers the corpus as source
   :data:`CORPUS_SOURCE_ID` -- explicitly, here, never implicitly per query;
4. inserts one record per corpus file in sorted batches; each batch and its
   checkpoint (``backfill_runs.last_key``) commit in one transaction, so an
   interrupted run resumes after the last committed batch and a rerun
   creates no duplicates (records are keyed by a path-derived id and
   inserted with ``ON CONFLICT DO NOTHING``).

New records are ``unclassified`` with research type ``other`` and belong to
no project and no collection. Existing records -- and any curation done on
them -- are never modified. Nested-entry ownership (ADR 0005) is a retrieval
concern: a file reachable from several entries is one record here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.registry import KIND_TO_BUCKET, load_registry_document
from ..core.util.sqlite import connect
from ..retrieval.fts5 import TEXT_SUFFIXES
from ..retrieval.ownership import EntryOwnership
from .errors import InvalidInputError, KnowledgeError, UnavailableError
from .store import KnowledgeStore, NewRecord, latest_schema_version

_logger = logging.getLogger(__name__)

#: The source id of the base corpus registry.
CORPUS_SOURCE_ID = "corpus:base"
CORPUS_SOURCE_NAME = "Base corpus"
DEFAULT_WORKSPACE_NAME = "Default workspace"
DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 10_000
#: Estimated bytes one record (row + indexes) needs, for the disk estimate.
RECORD_BYTES_ESTIMATE = 512
#: Bytes read from the start of each file to derive a title and check UTF-8.
TITLE_PROBE_BYTES = 65_536
#: Examples listed per category in a report.
MAX_EXAMPLES = 20


def corpus_record_id(logical_path: str) -> str:
    """The stable record id of a corpus file: derived from its corpus-relative path."""
    return "file-" + hashlib.sha256(logical_path.encode("utf-8")).hexdigest()[:32]


@dataclass
class CorpusFile:
    """One indexable corpus file and the registry entries that reach it."""

    logical_path: str
    entry_ids: list[str] = field(default_factory=list)
    enabled: bool = False
    size: int = 0
    title: str = ""
    utf8: bool = True
    owner_entry_id: str | None = None


@dataclass
class CorpusScan:
    files: dict[str, CorpusFile]
    entries_total: int
    entries_enabled: int
    missing_entries: list[str]


def _probe(path: Path) -> tuple[str, bool]:
    """``(title, valid_utf8_prefix)`` from the first bytes of ``path``."""
    import codecs  # noqa: PLC0415

    with path.open("rb") as handle:
        head = handle.read(TITLE_PROBE_BYTES)
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        valid = True
    except UnicodeDecodeError:
        text = head.decode("utf-8", errors="replace")
        valid = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:1000], valid
    return path.stem[:1000], valid


def scan_corpus(corpus_root: Path, registry_path: Path) -> CorpusScan:
    """Walk every registry entry (enabled or not) the way the FTS indexer does."""
    try:
        document = load_registry_document(registry_path)
    except FileNotFoundError as exc:
        raise UnavailableError(
            "the registry does not exist", details={"registry": registry_path.name}
        ) from exc
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"the registry cannot be read: {exc}") from exc
    root = corpus_root.resolve()
    entries = [entry for bucket in KIND_TO_BUCKET.values() for entry in document.get(bucket, [])]
    ownership = EntryOwnership(entries)
    files: dict[str, CorpusFile] = {}
    missing: list[str] = []
    total = enabled_count = 0
    for bucket in KIND_TO_BUCKET.values():
        for entry in document.get(bucket, []):
            total += 1
            enabled = bool(entry.get("enabled", True))
            enabled_count += int(enabled)
            entry_root = (root / entry["path"]).resolve()
            if not entry_root.is_relative_to(root):
                _logger.warning("skipping entry %r: path leaves the corpus root", entry.get("id"))
                missing.append(str(entry.get("id")))
                continue
            if not entry_root.exists():
                missing.append(str(entry.get("id")))
                continue
            candidates = [entry_root] if entry_root.is_file() else sorted(entry_root.rglob("*"))
            for path in candidates:
                if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                rel = path.relative_to(root).as_posix()
                item = files.get(rel)
                if item is None:
                    try:
                        title, valid = _probe(path)
                        size = path.stat().st_size
                    except OSError as exc:
                        _logger.debug("skipping unreadable %s: %s", path, exc)
                        continue
                    owner = ownership.owner(rel)
                    item = files[rel] = CorpusFile(
                        rel,
                        size=size,
                        title=title,
                        utf8=valid,
                        owner_entry_id=str(owner["id"]) if owner is not None else None,
                    )
                item.entry_ids.append(str(entry["id"]))
                item.enabled = item.enabled or enabled
    return CorpusScan(dict(sorted(files.items())), total, enabled_count, sorted(missing))


def _existing(db_path: Path) -> tuple[dict[str, str], dict[str, Any] | None, int]:
    """``(path -> record id, open run, schema version)`` -- read-only."""
    if not db_path.is_file():
        return {}, None, 0
    with KnowledgeStore.open_readonly(db_path) as store:
        version = store.schema_version()
        if version == 0:
            return {}, None, 0
        return store.record_keys(CORPUS_SOURCE_ID), store.open_run(CORPUS_SOURCE_ID), version


def _workspace_exists(db_path: Path, workspace_id: str) -> bool:
    if not db_path.is_file():
        return False
    with KnowledgeStore.open_readonly(db_path) as store:
        if store.schema_version() == 0:
            return False
        try:
            store.get_workspace(workspace_id)
        except KnowledgeError:
            return False
        return True


def plan(
    *,
    corpus_root: Path,
    registry_path: Path,
    db_path: Path,
    workspace_id: str,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """The dry-run report. Reads only; creates and writes nothing."""
    scan = scan_corpus(corpus_root, registry_path)
    known, open_run, version = _existing(db_path)
    new = [rel for rel in scan.files if rel not in known]
    duplicates = [item for item in scan.files.values() if len(item.entry_ids) > 1]
    non_utf8 = [rel for rel, item in scan.files.items() if not item.utf8]
    orphans = sorted(set(known) - set(scan.files))
    db_bytes = db_path.stat().st_size if db_path.is_file() else 0
    registry_bytes = registry_path.stat().st_size if registry_path.is_file() else 0
    growth = sum(RECORD_BYTES_ESTIMATE + 2 * len(rel) for rel in new)
    # backup copy + new rows (twice: WAL then checkpoint) + slack for the manifest
    required = db_bytes + registry_bytes + 2 * growth + 65_536
    probe_dir = _existing_parent(backup_dir or db_path.parent)
    available = shutil.disk_usage(probe_dir).free
    return {
        "mode": "dry-run",
        "workspace": {
            "workspace_id": workspace_id,
            "exists": _workspace_exists(db_path, workspace_id),
        },
        "source": {"source_id": CORPUS_SOURCE_ID, "kind": "corpus"},
        "schema": {
            "current_version": version,
            "target_version": latest_schema_version(),
            "store_exists": db_path.is_file(),
        },
        "entries": {
            "total": scan.entries_total,
            "enabled": scan.entries_enabled,
            "missing_content": scan.missing_entries,
        },
        "records": {
            "corpus_files": len(scan.files),
            "enabled_files": sum(1 for item in scan.files.values() if item.enabled),
            "already_registered": len(scan.files) - len(new),
            "to_create": len(new),
        },
        "duplicates": {
            "files_in_several_entries": len(duplicates),
            "examples": [
                {
                    "logical_path": item.logical_path,
                    "entry_ids": item.entry_ids,
                    "owner_entry_id": item.owner_entry_id,
                }
                for item in duplicates[:MAX_EXAMPLES]
            ],
            "note": "a file reachable from nested entries is one record; search attributes "
            "it to its owner, the deepest enabled entry (ADR 0005)",
        },
        "unknown_metadata": {
            "research_type_defaulted_to_other": len(new),
            "curation_status_defaulted_to_unclassified": len(new),
            "non_utf8_files": len(non_utf8),
            "non_utf8_examples": non_utf8[:MAX_EXAMPLES],
        },
        "orphans": {"records_without_file": len(orphans), "examples": orphans[:MAX_EXAMPLES]},
        "disk": {
            "required_bytes": required,
            "available_bytes": available,
            "sufficient": available >= required,
        },
        "resume": (
            {
                "run_id": open_run["run_id"],
                "status": open_run["status"],
                "last_key": open_run["last_key"],
                "processed": open_run["processed"],
            }
            if open_run
            else None
        ),
    }


def _existing_parent(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path.cwd()


def backup(
    *, db_path: Path, registry_path: Path, backup_dir: Path, report: dict[str, Any]
) -> dict[str, Any]:
    """Copy the stores the apply touches into a new timestamped directory."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"knowledge-migrate-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    files: list[str] = []
    if db_path.is_file():
        source = connect(db_path, wal=False, row_factory=None, create_parent=False)
        try:
            dest = sqlite3.connect(str(target / db_path.name))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        files.append(db_path.name)
    if registry_path.is_file():
        shutil.copy2(registry_path, target / registry_path.name)
        files.append(registry_path.name)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
        "knowledge_db_existed": db_path.is_file(),
        "schema": report.get("schema"),
        "records": report.get("records"),
        "restore": (
            "stop the engine, copy knowledge.db from this directory over the configured "
            "KE_KNOWLEDGE_DB (delete it if knowledge_db_existed is false), then restart"
        ),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files.append("manifest.json")
    _logger.info("knowledge migrate: backup written to %s", target)
    return {"path": str(target), "files": files}


def apply(
    *,
    corpus_root: Path,
    registry_path: Path,
    db_path: Path,
    workspace_id: str,
    backup_dir: Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = None,
    on_batch: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Back up, then register the corpus in resumable batches; return a report.

    ``max_batches`` stops after that many batches (the run stays resumable);
    ``on_batch(batches_done, processed)`` is called after each committed batch.
    """
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise InvalidInputError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    if max_batches is not None and max_batches < 1:
        raise InvalidInputError("max_batches must be at least 1")
    report = plan(
        corpus_root=corpus_root,
        registry_path=registry_path,
        db_path=db_path,
        workspace_id=workspace_id,
        backup_dir=backup_dir,
    )
    if not report["disk"]["sufficient"]:
        raise UnavailableError(
            "not enough free disk space for the backup and the new records",
            details=report["disk"],
        )
    backup_info = backup(
        db_path=db_path,
        registry_path=registry_path,
        backup_dir=backup_dir or db_path.parent / "backups",
        report=report,
    )
    scan = scan_corpus(corpus_root, registry_path)
    with KnowledgeStore.open(db_path) as store:
        _, workspace_created = store.ensure_workspace(workspace_id, DEFAULT_WORKSPACE_NAME)
        _, source_created = store.register_source(
            workspace_id, CORPUS_SOURCE_ID, kind="corpus", display_name=CORPUS_SOURCE_NAME
        )
        open_run = store.open_run(CORPUS_SOURCE_ID)
        total = len(scan.files)
        if open_run is not None:
            run_id = str(open_run["run_id"])
            last_key = open_run["last_key"]
            store.resume_run(run_id, total, backup_info["path"])
            _logger.info("knowledge migrate: resuming run %s after %r", run_id, last_key)
        else:
            run_id = str(store.start_run(CORPUS_SOURCE_ID, total, backup_info["path"])["run_id"])
            last_key = None
        pending = [item for rel, item in scan.files.items() if last_key is None or rel > last_key]
        batches = created = existing = processed = 0
        try:
            for start in range(0, len(pending), batch_size):
                if max_batches is not None and batches >= max_batches:
                    break
                chunk = pending[start : start + batch_size]
                rows = [
                    NewRecord(
                        record_id=corpus_record_id(item.logical_path),
                        logical_path=item.logical_path,
                        title=item.title,
                    )
                    for item in chunk
                ]
                c, e = store.commit_batch(run_id, CORPUS_SOURCE_ID, rows, chunk[-1].logical_path)
                batches += 1
                created += c
                existing += e
                processed += len(chunk)
                if on_batch is not None:
                    on_batch(batches, processed)
        except Exception as exc:
            store.finish_run(run_id, error=f"{type(exc).__name__}: {exc}")
            _logger.exception("knowledge migrate: run %s failed; rerun to resume", run_id)
            raise
        complete = processed == len(pending)
        run = store.finish_run(run_id) if complete else store.open_run(CORPUS_SOURCE_ID)
        registered = store.count_records(CORPUS_SOURCE_ID)
        keys = store.record_keys(CORPUS_SOURCE_ID)
        missing = [rel for rel in scan.files if rel not in keys]
    _logger.info(
        "knowledge migrate: run %s %s (%d created, %d existing)",
        run_id,
        "completed" if complete else "paused",
        created,
        existing,
    )
    return {
        "mode": "apply",
        "run_id": run_id,
        "resumed": open_run is not None,
        "complete": complete,
        "workspace_created": workspace_created,
        "source_created": source_created,
        "batches": batches,
        "processed": processed,
        "created": created,
        "existing": existing,
        "remaining": len(pending) - processed,
        "run": run,
        "backup": backup_info,
        "reconciliation": {
            "corpus_files": total,
            "registered_records": registered,
            "missing_records": len(missing),
            "reconciled": complete and not missing,
            "orphan_records": report["orphans"]["records_without_file"],
        },
        "plan": report,
    }


__all__ = [
    "CORPUS_SOURCE_ID",
    "DEFAULT_BATCH_SIZE",
    "CorpusFile",
    "CorpusScan",
    "apply",
    "backup",
    "corpus_record_id",
    "plan",
    "scan_corpus",
]
