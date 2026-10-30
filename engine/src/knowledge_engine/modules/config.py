"""Resolved configuration of the module host (a view over ``Settings``; no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .manifest import CAPABILITIES

if TYPE_CHECKING:
    from ..core.settings import Settings

MODULES_DB_FILENAME = "modules.db"
ARTIFACT_DIRNAME = "module-artifacts"


@dataclass(frozen=True)
class ModulesConfig:
    """Where the catalog lives, where module directories come from and the host policy."""

    db_path: Path
    artifact_dir: Path
    modules_dir: Path | None
    corpus_root: Path
    host_capabilities: frozenset[str]
    default_workspace: str
    enabled: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> ModulesConfig:
        paths = settings.resolve_paths(warn_missing_corpus=False)
        raw_db = (settings.modules_db or "").strip()
        db_path = (
            Path(raw_db).expanduser().resolve() if raw_db else paths.data_dir / MODULES_DB_FILENAME
        )
        raw_dir = (settings.modules_dir or "").strip()
        return cls(
            db_path=db_path,
            artifact_dir=db_path.parent / ARTIFACT_DIRNAME,
            modules_dir=Path(raw_dir).expanduser().resolve() if raw_dir else None,
            corpus_root=paths.corpus_root,
            host_capabilities=parse_capabilities(settings.modules_host_capabilities),
            default_workspace=settings.knowledge_default_workspace,
            enabled=settings.modules_enabled,
        )

    @classmethod
    def from_env(cls) -> ModulesConfig:
        from ..core.settings import Settings  # noqa: PLC0415 - keep the import light

        return cls.from_settings(Settings())


def parse_capabilities(raw: str) -> frozenset[str]:
    """``"read,network"`` -> ``{"read", "network"}``; unknown names are dropped."""
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return frozenset(names & set(CAPABILITIES))


__all__ = ["ARTIFACT_DIRNAME", "MODULES_DB_FILENAME", "ModulesConfig", "parse_capabilities"]
