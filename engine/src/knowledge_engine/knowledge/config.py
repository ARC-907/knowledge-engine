"""Resolved configuration of the scoped-knowledge layer (a view over ``Settings``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import RetentionMode
from .evidence.policy import EvidencePolicy
from .store import KNOWLEDGE_DB_FILENAME

if TYPE_CHECKING:
    from ..core.settings import Settings


@dataclass(frozen=True)
class KnowledgeConfig:
    """Where ``knowledge.db`` lives and which defaults apply. Resolution does no I/O."""

    db_path: Path
    default_workspace: str
    profile: str
    corpus_root: Path
    registry_path: Path
    data_dir: Path
    enabled: bool = True
    evidence: EvidencePolicy = field(default_factory=EvidencePolicy)

    @classmethod
    def from_settings(cls, settings: Settings) -> KnowledgeConfig:
        paths = settings.resolve_paths(warn_missing_corpus=False)
        raw = (settings.knowledge_db or "").strip()
        db_path = (
            Path(raw).expanduser().resolve() if raw else paths.data_dir / KNOWLEDGE_DB_FILENAME
        )
        return cls(
            db_path=db_path,
            default_workspace=settings.knowledge_default_workspace,
            profile=settings.knowledge_profile,
            corpus_root=paths.corpus_root,
            registry_path=paths.registry_path,
            data_dir=paths.data_dir,
            enabled=settings.knowledge_enabled,
            evidence=EvidencePolicy(
                default_retention=RetentionMode(settings.evidence_retention),
                on_delete="purge" if settings.evidence_on_delete == "purge" else "tombstone",
                max_bytes=settings.evidence_max_bytes,
            ),
        )

    @classmethod
    def from_env(cls) -> KnowledgeConfig:
        from ..core.settings import Settings  # noqa: PLC0415 - keep the import light

        return cls.from_settings(Settings())


__all__ = ["KnowledgeConfig"]
