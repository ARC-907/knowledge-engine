"""``knowledge-engine knowledge snapshot``: record the corpus files as immutable revisions.

The corpus is walked exactly like ``knowledge migrate`` and the FTS indexer
walk it (:func:`knowledge_engine.knowledge.backfill.scan_corpus`). For every
file the snapshot compares the bytes with the record's current revision:

* **unchanged** -- same digest: nothing is written (a rerun is a no-op, and
  rebuilding the FTS/vector indexes never touches these identities);
* **edited** -- new bytes: a new revision; the previous one stays retained, so
  citations of it resolve ``stale`` instead of following the edit;
* **new** -- no record at that path: a record is created (corpus record id,
  ADR 0007) unless the file is a **rename**;
* **renamed** -- a record whose path disappeared has exactly the same current
  digest as exactly one new path: the record moves and keeps its id; the old
  path becomes an alias. Several candidates are not guessed: they are
  recorded as new records plus missing ones;
* **missing** -- a record's file is gone. Only ``--delete-missing`` deletes it,
  following ``KE_EVIDENCE_ON_DELETE`` (``tombstone`` or ``purge``); deletion is
  skipped while any registry entry's content is missing (an unmounted
  directory must not erase evidence).

Files larger than ``KE_EVIDENCE_MAX_BYTES`` are recorded digest-only with the
reason. The dry run (``apply=False``) writes nothing.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backfill import CORPUS_SOURCE_ID, corpus_record_id, scan_corpus
from ..errors import KnowledgeError, UnknownScopeError
from .extract import MEDIA_TEXT, SUFFIX_MEDIA
from .store import EvidenceStore, IngestRequest

_logger = logging.getLogger(__name__)

#: Examples listed per category in a report.
MAX_EXAMPLES = 20
_CHUNK = 1 << 20


@dataclass(frozen=True)
class _FileState:
    path: str
    digest: str
    size: int
    content: bytes | None


@dataclass(frozen=True)
class _RecordHead:
    record_id: str
    path: str | None
    digest: str | None
    deleted: bool


def _read(path: Path, max_bytes: int) -> tuple[str, int, bytes | None]:
    """``(digest, size, content or None when larger than max_bytes)`` -- streamed."""
    sha = hashlib.sha256()
    kept = bytearray()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            sha.update(chunk)
            size += len(chunk)
            if size <= max_bytes:
                kept.extend(chunk)
    return "sha256:" + sha.hexdigest(), size, bytes(kept) if size <= max_bytes else None


def _heads(evidence: EvidenceStore) -> dict[str, _RecordHead]:
    with evidence.snapshot() as conn:
        rows = conn.execute(
            "SELECT r.record_id, r.logical_path, r.deleted_at, v.digest FROM records r"
            " LEFT JOIN revisions v ON v.revision_id = r.current_revision_id"
            " WHERE r.source_id = ?",
            (CORPUS_SOURCE_ID,),
        ).fetchall()
    return {
        row["record_id"]: _RecordHead(
            row["record_id"], row["logical_path"], row["digest"], row["deleted_at"] is not None
        )
        for row in rows
    }


def _allocate_id(path: str, taken: set[str]) -> str:
    """The corpus record id of ``path``; a suffix when a renamed record already holds it."""
    record_id = corpus_record_id(path)
    attempt = 1
    while record_id in taken:
        attempt += 1
        record_id = corpus_record_id(f"{path}\x00{attempt}")
    taken.add(record_id)
    return record_id


def snapshot(
    evidence: EvidenceStore,
    *,
    corpus_root: Path,
    registry_path: Path,
    apply: bool,
    delete_missing: bool = False,
) -> dict[str, Any]:
    """Plan (and with ``apply``, record) the corpus as revisions; return a report."""
    try:
        evidence.store.get_source(CORPUS_SOURCE_ID)
    except UnknownScopeError as exc:
        raise UnknownScopeError(
            f"source {CORPUS_SOURCE_ID!r} is not registered",
            details={"hint": "run `knowledge-engine knowledge migrate --apply` first"},
        ) from exc
    scan = scan_corpus(corpus_root, registry_path)
    root = corpus_root.resolve()
    policy = evidence.policy
    files: dict[str, _FileState] = {}
    unreadable: list[str] = []
    for rel in scan.files:
        try:
            digest, size, content = _read(root / rel, policy.max_bytes)
        except OSError as exc:
            _logger.warning("snapshot: cannot read %s: %s", rel, exc)
            unreadable.append(rel)
            continue
        files[rel] = _FileState(rel, digest, size, content)

    heads = _heads(evidence)
    by_path = {head.path: head for head in heads.values() if head.path is not None}
    unchanged: list[str] = []
    edited: list[tuple[str, str]] = []
    first: list[tuple[str, str]] = []
    restored: list[tuple[str, str]] = []
    fresh: list[str] = []
    for rel, state in files.items():
        head = by_path.get(rel)
        if head is None:
            fresh.append(rel)
        elif head.deleted:
            restored.append((rel, head.record_id))
        elif head.digest is None:
            first.append((rel, head.record_id))
        elif head.digest == state.digest:
            unchanged.append(rel)
        else:
            edited.append((rel, head.record_id))
    missing = [
        head
        for head in heads.values()
        if not head.deleted and head.path is not None and head.path not in files
    ]
    # Renames: a unique digest match between one vanished record and one new path.
    renames: list[tuple[str, str, str]] = []  # (old path, new path, record id)
    by_digest_missing: dict[str, list[_RecordHead]] = {}
    for head in missing:
        if head.digest is not None:
            by_digest_missing.setdefault(head.digest, []).append(head)
    by_digest_fresh: dict[str, list[str]] = {}
    for rel in fresh:
        by_digest_fresh.setdefault(files[rel].digest, []).append(rel)
    for digest, olds in sorted(by_digest_missing.items()):
        news = by_digest_fresh.get(digest, [])
        if len(olds) == 1 and len(news) == 1:
            renames.append((str(olds[0].path), news[0], olds[0].record_id))
    renamed_ids = {record_id for _, _, record_id in renames}
    renamed_new = {new for _, new, _ in renames}
    fresh = [rel for rel in fresh if rel not in renamed_new]
    missing = [head for head in missing if head.record_id not in renamed_ids]
    deletion_blocked = bool(scan.missing_entries)
    digest_only = sorted(rel for rel, state in files.items() if state.content is None)

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "source_id": CORPUS_SOURCE_ID,
        "policy": {
            "on_delete": policy.on_delete,
            "max_bytes": policy.max_bytes,
            "default_retention": policy.default_retention.value,
        },
        "files": len(files),
        "unchanged": len(unchanged),
        "new_records": len(fresh),
        "first_revisions": len(first),
        "edited": len(edited),
        "restored": len(restored),
        "renamed": [{"from": old, "to": new, "record_id": rid} for old, new, rid in renames][
            :MAX_EXAMPLES
        ],
        "renamed_count": len(renames),
        "missing": len(missing),
        "missing_examples": sorted(str(h.path) for h in missing)[:MAX_EXAMPLES],
        "delete_missing": delete_missing,
        "deletion_blocked_by_missing_entries": scan.missing_entries if deletion_blocked else [],
        "digest_only": len(digest_only),
        "digest_only_examples": digest_only[:MAX_EXAMPLES],
        "unreadable": unreadable[:MAX_EXAMPLES],
    }
    if not apply:
        return report

    taken = set(heads)
    created_revisions = 0
    deleted = 0
    failures: list[dict[str, str]] = []
    # Renames first: a new file may reuse a path a renamed record is leaving.
    work: list[tuple[str, str]] = sorted((new, rid) for _, new, rid in renames)
    work += sorted([*first, *edited, *restored])
    work += [(rel, _allocate_id(rel, taken)) for rel in sorted(fresh)]
    titles = {rel: item.title for rel, item in scan.files.items()}
    for rel, record_id in work:
        state = files[rel]
        suffix = Path(rel).suffix.lower()
        request = IngestRequest(
            source_id=CORPUS_SOURCE_ID,
            record_id=record_id,
            logical_path=rel,
            title=titles.get(rel) or None,
            media_type=SUFFIX_MEDIA.get(suffix, MEDIA_TEXT),
            content=state.content,
            digest=state.digest,
            byte_length=state.size,
            external_locator=f"{CORPUS_SOURCE_ID}/{rel}" if state.content is None else None,
            unavailable_reason=(
                f"larger than KE_EVIDENCE_MAX_BYTES ({policy.max_bytes} bytes)"
                if state.content is None
                else None
            ),
        )
        try:
            result = evidence.ingest(request)
        except KnowledgeError as exc:
            _logger.warning("snapshot: %s failed: %s", rel, exc.message)
            failures.append({"path": rel, "code": exc.code, "message": exc.message})
            continue
        created_revisions += int(result.revision_created)
    if delete_missing and not deletion_blocked:
        for head in missing:
            evidence.delete_record(
                CORPUS_SOURCE_ID, head.record_id, reason="file removed from the corpus"
            )
            deleted += 1
    elif delete_missing:
        _logger.warning(
            "snapshot: not deleting %d record(s): registry entries without content: %s",
            len(missing),
            ", ".join(scan.missing_entries),
        )
    report.update(
        {
            "revisions_created": created_revisions,
            "deleted": deleted,
            "failures": failures[:MAX_EXAMPLES],
            "failure_count": len(failures),
            "evidence": evidence.stats(),
        }
    )
    _logger.info(
        "snapshot: %d file(s), %d new revision(s), %d renamed, %d deleted",
        len(files),
        created_revisions,
        len(renames),
        deleted,
    )
    return report


__all__ = ["snapshot"]
