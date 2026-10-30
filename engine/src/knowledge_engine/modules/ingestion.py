"""Host ingestion of module artifacts: the interface the evidence store (P3) implements.

The dispatcher never writes evidence itself. When a caller asks for
ingestion it hands the normalized artifacts (with their bytes), an explicit
**destination scope** and the **derivation provenance** of the run to an
:class:`EvidenceIngestion` implementation and records the receipt.

The evidence/revision store is being built in a sibling slice (P3), so this
module ships the interface plus two implementations:

* :class:`UnconfiguredIngestion` -- the default: every call fails with
  ``unavailable`` (ingestion is never silently skipped);
* :class:`InMemoryIngestion` -- a test double with P3's identity rules
  (``derive_revision_id``; the same record + bytes is one revision, so a
  restarted run creates no duplicates) and scope validation.

Wiring the P3 store is one adapter class with the same ``ingest`` signature,
passed to :class:`~knowledge_engine.modules.dispatcher.ModuleHost`.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterable, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..knowledge.contracts import (
    RECORD_ID_PATTERN,
    Derivation,
    Digest,
    RecordScope,
    RevisionId,
    derive_revision_id,
)
from ..knowledge.errors import InvalidInputError, UnavailableError, UnknownScopeError
from .artifacts import ArtifactPayload


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DerivationInput(_Model):
    """Provenance of a module run, before the store assigns output revisions."""

    run_id: str = Field(min_length=1, max_length=200)
    module_id: str = Field(min_length=1, max_length=200)
    module_version: str = Field(min_length=1, max_length=100)
    parameters_digest: Digest
    parent_artifact_ids: tuple[str, ...] = ()
    parent_revision_ids: tuple[RevisionId, ...] = ()


class IngestionReceipt(_Model):
    """What the store committed: one revision per artifact and the full derivation."""

    source_id: str
    destination: RecordScope
    artifact_revisions: dict[str, str] = Field(description="artifact_id -> revision_id")
    created: int = Field(ge=0)
    reused: int = Field(ge=0)
    derivation: Derivation


class EvidenceIngestion(Protocol):
    """The P3 ingestion API as the module host calls it."""

    def ingest(
        self,
        artifacts: Sequence[ArtifactPayload],
        *,
        destination: RecordScope,
        derivation: DerivationInput,
    ) -> IngestionReceipt:
        """Commit ``artifacts`` into ``destination`` atomically, idempotently, with provenance.

        Raises ``unknown_scope`` for a destination that does not exist,
        ``unavailable`` when the store cannot answer.
        """

    def revisions_for(self, artifact_ids: Iterable[str]) -> dict[str, str]:
        """``artifact_id -> revision_id`` for artifacts ingested earlier (parent lineage)."""


def module_source_id(module_id: str) -> str:
    """The source a module's records belong to: ``module:<module_id>``."""
    return f"module:{module_id}"


def record_id_for(payload: ArtifactPayload) -> str:
    """Source-specific record identity: the record key, else the canonical URI, else the bytes."""
    artifact = payload.artifact
    if artifact.record_key and re.fullmatch(RECORD_ID_PATTERN, artifact.record_key):
        return artifact.record_key
    basis = artifact.canonical_uri or artifact.digest
    return "r-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]


class UnconfiguredIngestion:
    """Default sink: the evidence store is not wired into this build."""

    def ingest(
        self,
        artifacts: Sequence[ArtifactPayload],
        *,
        destination: RecordScope,
        derivation: DerivationInput,
    ) -> IngestionReceipt:
        raise UnavailableError(
            "evidence ingestion is not available in this build",
            details={"hint": "the evidence store (V2 P3) provides the ingestion API"},
        )

    def revisions_for(self, artifact_ids: Iterable[str]) -> dict[str, str]:
        return {}


class InMemoryIngestion:
    """Test double with P3 identity rules; ``workspaces`` limits valid destinations."""

    def __init__(
        self, workspaces: Iterable[str] | None = None, projects: Iterable[tuple[str, str]] = ()
    ) -> None:
        self.workspaces = set(workspaces) if workspaces is not None else None
        self.projects = set(projects)
        self.revisions: dict[str, dict[str, object]] = {}
        self.derivations: list[Derivation] = []
        self._by_artifact: dict[str, str] = {}
        self._lock = threading.Lock()

    def _check_scope(self, scope: RecordScope) -> None:
        if self.workspaces is not None and scope.workspace_id not in self.workspaces:
            raise UnknownScopeError(f"unknown workspace {scope.workspace_id!r}")
        if (
            scope.project_id is not None
            and (scope.workspace_id, scope.project_id) not in self.projects
        ):
            raise UnknownScopeError(f"unknown project {scope.project_id!r}")

    def ingest(
        self,
        artifacts: Sequence[ArtifactPayload],
        *,
        destination: RecordScope,
        derivation: DerivationInput,
    ) -> IngestionReceipt:
        if not artifacts:
            raise InvalidInputError("nothing to ingest")
        self._check_scope(destination)
        source_id = module_source_id(derivation.module_id)
        mapping: dict[str, str] = {}
        created = reused = 0
        with self._lock:
            for payload in artifacts:
                record_id = record_id_for(payload)
                revision_id = derive_revision_id(source_id, record_id, payload.artifact.digest)
                if revision_id in self.revisions:
                    reused += 1
                else:
                    created += 1
                    self.revisions[revision_id] = {
                        "record_id": record_id,
                        "scope": destination,
                        "digest": payload.artifact.digest,
                        "content": payload.content,
                    }
                mapping[payload.artifact.artifact_id] = revision_id
                self._by_artifact[payload.artifact.artifact_id] = revision_id
            parents = tuple(
                dict.fromkeys(
                    [*derivation.parent_revision_ids]
                    + [
                        self._by_artifact[a]
                        for a in derivation.parent_artifact_ids
                        if a in self._by_artifact
                    ]
                )
            )
            record = Derivation(
                run_id=derivation.run_id,
                module_id=derivation.module_id,
                module_version=derivation.module_version,
                parent_revision_ids=parents,
                parameters_digest=derivation.parameters_digest,
                output_revision_ids=tuple(dict.fromkeys(mapping.values())),
            )
            self.derivations.append(record)
        return IngestionReceipt(
            source_id=source_id,
            destination=destination,
            artifact_revisions=mapping,
            created=created,
            reused=reused,
            derivation=record,
        )

    def revisions_for(self, artifact_ids: Iterable[str]) -> dict[str, str]:
        with self._lock:
            return {a: self._by_artifact[a] for a in artifact_ids if a in self._by_artifact}


__all__ = [
    "DerivationInput",
    "EvidenceIngestion",
    "InMemoryIngestion",
    "IngestionReceipt",
    "UnconfiguredIngestion",
    "module_source_id",
    "record_id_for",
]
