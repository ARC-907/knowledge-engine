"""Resolved filesystem configuration: a thin view over :class:`Settings`.

Path defaults come from :mod:`knowledge_engine.core.paths` (``KE_HOME``,
``KE_CORPUS_ROOT``, ``KE_REGISTRY_PATH``, ``KE_DATA_DIR``), read through the
typed :class:`knowledge_engine.core.settings.Settings` layer. Loading a config
never touches the filesystem; entry points that write state call
:meth:`Config.ensure_dirs` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths

if TYPE_CHECKING:
    from .settings import Settings


@dataclass(frozen=True)
class Config:
    corpus_root: Path
    data_dir: Path
    registry_path: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> Config:
        """Resolve the paths described by ``settings``. Side-effect free."""
        resolved = settings.paths
        return cls(
            corpus_root=resolved.corpus_root,
            data_dir=resolved.data_dir,
            registry_path=resolved.registry_path,
        )

    @classmethod
    def from_env(cls) -> Config:
        """Resolve the configuration from ``KE_*`` env vars. Side-effect free."""
        from .settings import Settings  # noqa: PLC0415 - keeps `core.config` import light

        return cls.from_settings(Settings())

    def ensure_dirs(self) -> None:
        """Create the runtime data directory. Idempotent."""
        paths.ensure_data_dir(self.data_dir)
