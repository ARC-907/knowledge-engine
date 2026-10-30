"""Module artifacts into the evidence store: the production :class:`EvidenceIngestion`.

:class:`EvidenceStoreIngestion` adapts P3's
:class:`~knowledge_engine.knowledge.evidence.store.EvidenceStore` to the
interface the module host calls (ADR 0015):

* **Source.** A module's records belong to a ``module`` source registered in
  the destination workspace on first use: ``module:<module_id>`` in the
  default workspace (``KE_KNOWLEDGE_DEFAULT_WORKSPACE``), and
  ``module:<module_id>--<workspace_id>`` in any other (module ids never
  contain ``--``, so the form is unambiguous; an id too long for the
  source-id pattern uses a digest of the workspace id instead).
* **Records and revisions.** Each artifact becomes a revision of the record
  :func:`~knowledge_engine.modules.ingestion.record_id_for` names, with the
  run's derivation (module, version, parameters digest, parent revisions).
  The same record and bytes are one revision, so re-running a module creates
  no duplicates.
* **Scope.** The workspace (and project, when given) must exist; a new
  record is assigned to the destination project. A record that already
  belongs to a different project is never moved: the call fails with
  ``invalid_input`` naming both projects.
* **Restarts.** Each artifact is ingested with the idempotency key
  ``module:<artifact_id>:<run_id>``, so a restarted run replays what it
  already committed. Ingestion is atomic per artifact, not across a run's
  artifacts; a run interrupted midway completes on retry.
* **Lineage.** :meth:`revisions_for` reads those keys back, so a later run
  that names an earlier run's artifacts as parents links to their revisions.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..knowledge.contracts import SOURCE_ID_PATTERN, Derivation, RecordScope
from ..knowledge.errors import InvalidInputError, KnowledgeError, UnknownScopeError
from ..knowledge.evidence.policy import EvidencePolicy
from ..knowledge.evidence.store import (
    DerivationInput as EvidenceDerivation,
    EvidenceStore,
    IngestRequest,
    IngestResult,
)
from ..knowledge.store import KnowledgeStore
from .artifacts import ArtifactPayload
from .ingestion import (
    DerivationInput,
    EvidenceIngestion,
    IngestionReceipt,
    UnconfiguredIngestion,
    record_id_for,
)

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

#: Prefix of the idempotency keys this adapter writes.
KEY_PREFIX = "module:"
_MAX_KEY = 200


def module_source_id_in(module_id: str, workspace_id: str, default_workspace: str) -> str:
    """The ``module`` source id for ``module_id`` in ``workspace_id`` (see module docstring)."""
    if workspace_id == default_workspace:
        candidate = f"module:{module_id}"
    else:
        candidate = f"module:{module_id}--{workspace_id}"
    if re.fullmatch(SOURCE_ID_PATTERN, candidate):
        return candidate
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]
    shortened = f"module:{module_id[:80]}--w{digest}"
    if not re.fullmatch(SOURCE_ID_PATTERN, shortened):  # pragma: no cover - ids are validated
        raise InvalidInputError(
            f"cannot form a source id for module {module_id!r}",
            details={"module_id": module_id, "workspace_id": workspace_id},
        )
    return shortened


def idempotency_key(artifact_id: str, run_id: str) -> str:
    """``module:<artifact_id>:<run_id>``, with a digest of long run ids (max 200 chars)."""
    key = f"{KEY_PREFIX}{artifact_id}:{run_id}"
    if len(key) <= _MAX_KEY:
        return key
    return f"{KEY_PREFIX}{artifact_id}:h{hashlib.sha256(run_id.encode('utf-8')).hexdigest()}"


class EvidenceStoreIngestion:
    """:class:`~knowledge_engine.modules.ingestion.EvidenceIngestion` over an evidence store."""

    def __init__(self, evidence: EvidenceStore, *, default_workspace: str) -> None:
        self.evidence = evidence
        self.default_workspace = default_workspace

    # ── EvidenceIngestion ───────────────────────────────────────

    def ingest(
        self,
        artifacts: Sequence[ArtifactPayload],
        *,
        destination: RecordScope,
        derivation: DerivationInput,
    ) -> IngestionReceipt:
        if not artifacts:
            raise InvalidInputError("nothing to ingest")
        self._check_destination(destination)
        source_id = module_source_id_in(
            derivation.module_id, destination.workspace_id, self.default_workspace
        )
        _, registered = self.evidence.store.register_source(
            destination.workspace_id,
            source_id,
            kind="module",
            display_name=f"Module {derivation.module_id}"[:200],
        )
        if registered:
            _logger.info(
                "module source %s registered in workspace %s", source_id, destination.workspace_id
            )

        parents = list(derivation.parent_revision_ids)
        parents += self.revisions_for(derivation.parent_artifact_ids).values()
        parent_revisions = tuple(dict.fromkeys(parents))
        lineage = EvidenceDerivation(
            run_id=derivation.run_id,
            module_id=derivation.module_id,
            module_version=derivation.module_version,
            parameters_digest=derivation.parameters_digest,
            parent_revision_ids=parent_revisions,
        )

        mapping: dict[str, str] = {}
        created = reused = 0
        for payload in artifacts:
            result = self._ingest_one(payload, source_id, lineage, derivation.run_id)
            self._assign(source_id, result, destination)
            mapping[payload.artifact.artifact_id] = result.revision.revision_id
            if result.revision_created:
                created += 1
            else:
                reused += 1
        _logger.info(
            "module %s run %s: %d artifact(s) ingested into %s (%d new, %d existing)",
            derivation.module_id,
            derivation.run_id,
            len(artifacts),
            source_id,
            created,
            reused,
        )
        return IngestionReceipt(
            source_id=source_id,
            destination=destination,
            artifact_revisions=mapping,
            created=created,
            reused=reused,
            derivation=Derivation(
                run_id=derivation.run_id,
                module_id=derivation.module_id,
                module_version=derivation.module_version,
                parameters_digest=derivation.parameters_digest,
                parent_revision_ids=parent_revisions,
                output_revision_ids=tuple(dict.fromkeys(mapping.values())),
            ),
        )

    def revisions_for(self, artifact_ids: Iterable[str]) -> dict[str, str]:
        """Latest committed revision per artifact id (unknown ids are left out)."""
        wanted = sorted(set(artifact_ids))
        if not wanted:
            return {}
        found: dict[str, str] = {}
        with self.evidence.snapshot() as conn:
            for artifact_id in wanted:
                low = f"{KEY_PREFIX}{artifact_id}:"
                row = conn.execute(
                    "SELECT result_json FROM ingest_requests"
                    " WHERE idempotency_key >= ? AND idempotency_key < ?"
                    " ORDER BY created_at DESC, idempotency_key DESC LIMIT 1",
                    (low, low[:-1] + ";"),
                ).fetchone()
                if row is not None:
                    result = IngestResult.model_validate_json(row["result_json"])
                    found[artifact_id] = result.revision.revision_id
        return found

    # ── internals ───────────────────────────────────────────────

    def _check_destination(self, destination: RecordScope) -> None:
        store = self.evidence.store
        store.get_workspace(destination.workspace_id)
        if destination.project_id is not None:
            project = store.get_project(destination.project_id)
            if project.workspace_id != destination.workspace_id:
                raise UnknownScopeError(
                    f"project {destination.project_id!r} is not in workspace "
                    f"{destination.workspace_id!r}",
                    details={"kind": "project", "id": destination.project_id},
                )

    def _ingest_one(
        self,
        payload: ArtifactPayload,
        source_id: str,
        lineage: EvidenceDerivation,
        run_id: str,
    ) -> IngestResult:
        artifact = payload.artifact
        record_id = record_id_for(payload)
        request = IngestRequest(
            source_id=source_id,
            record_id=record_id,
            logical_path=None if artifact.canonical_uri else f"artifacts/{record_id}",
            canonical_uri=artifact.canonical_uri,
            title=artifact.title,
            media_type=artifact.media_type,
            content=payload.content,
            digest=artifact.digest,
            upstream_date=artifact.published_at,
            rights=artifact.rights,
            derivation=lineage,
            idempotency_key=idempotency_key(artifact.artifact_id, run_id),
        )
        try:
            return self.evidence.ingest(request)
        except KnowledgeError:
            _logger.warning(
                "module artifact %s (record %s/%s) was not ingested",
                artifact.artifact_id,
                source_id,
                record_id,
                exc_info=True,
            )
            raise

    def _assign(self, source_id: str, result: IngestResult, destination: RecordScope) -> None:
        current = self.evidence.store.get_record(source_id, result.record.record_id)
        wanted = destination.project_id
        if current.scope.project_id == wanted:
            return
        if current.scope.project_id is not None and not result.record_created:
            raise InvalidInputError(
                f"record {source_id}/{current.record_id} belongs to project "
                f"{current.scope.project_id!r}, not {wanted!r}; it is not moved",
                details={
                    "reason": "project_mismatch",
                    "record_project": current.scope.project_id,
                    "destination_project": wanted,
                },
            )
        self.evidence.store.classify(source_id, current.record_id, project_id=wanted)


class LazyEvidenceIngestion:
    """Opens ``knowledge.db`` on the first ingestion, for hosts that own no store.

    The CLI and the process-default host use it, so module commands that never
    ingest (``module list``, ``status``, ...) do not create ``knowledge.db``.
    :meth:`revisions_for` answers ``{}`` while the database does not exist.
    :meth:`close` closes the store if it was opened.
    """

    def __init__(
        self, db_path: Path, evidence_policy: EvidencePolicy, *, default_workspace: str
    ) -> None:
        self.db_path = db_path
        self.evidence_policy = evidence_policy
        self.default_workspace = default_workspace
        self._inner: EvidenceStoreIngestion | None = None
        self._lock = threading.Lock()

    def _open(self) -> EvidenceStoreIngestion:
        with self._lock:
            if self._inner is None:
                store = KnowledgeStore.open(self.db_path)
                self._inner = EvidenceStoreIngestion(
                    EvidenceStore(store, self.evidence_policy),
                    default_workspace=self.default_workspace,
                )
                _logger.debug("module ingestion opened %s", self.db_path)
            return self._inner

    def ingest(
        self,
        artifacts: Sequence[ArtifactPayload],
        *,
        destination: RecordScope,
        derivation: DerivationInput,
    ) -> IngestionReceipt:
        return self._open().ingest(artifacts, destination=destination, derivation=derivation)

    def revisions_for(self, artifact_ids: Iterable[str]) -> dict[str, str]:
        if self._inner is None and not self.db_path.exists():
            return {}
        return self._open().revisions_for(artifact_ids)

    def close(self) -> None:
        with self._lock:
            inner, self._inner = self._inner, None
        if inner is not None:
            inner.evidence.store.close()


def ingestion_from_settings(
    settings: Settings, *, store: KnowledgeStore | None = None
) -> EvidenceIngestion:
    """The ingestion a module host should use under ``settings``.

    ``KE_KNOWLEDGE_ENABLED=0`` gives :class:`UnconfiguredIngestion` (calls fail
    with ``unavailable``). With ``store`` (the app's open knowledge store) the
    adapter shares it; otherwise ``knowledge.db`` is opened lazily.
    """
    from ..knowledge.config import KnowledgeConfig  # noqa: PLC0415 - avoid a cycle at import

    if not settings.knowledge_enabled:
        _logger.info("module ingestion unavailable: KE_KNOWLEDGE_ENABLED=0")
        return UnconfiguredIngestion()
    config = KnowledgeConfig.from_settings(settings)
    if store is not None:
        return EvidenceStoreIngestion(
            EvidenceStore(store, config.evidence), default_workspace=config.default_workspace
        )
    return LazyEvidenceIngestion(
        config.db_path, config.evidence, default_workspace=config.default_workspace
    )


__all__ = [
    "KEY_PREFIX",
    "EvidenceStoreIngestion",
    "LazyEvidenceIngestion",
    "idempotency_key",
    "ingestion_from_settings",
    "module_source_id_in",
]
