"""Configuration loaded from environment. No hardcoded paths.

Path defaults come from :mod:`knowledge_engine.core.paths` (``KE_HOME``,
``KE_CORPUS_ROOT``, ``KE_REGISTRY_PATH``, ``KE_DATA_DIR``). Loading a config
never touches the filesystem; entry points that write state call
:meth:`Config.ensure_dirs` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class Config:
    corpus_root: Path
    data_dir: Path
    registry_path: Path

    @classmethod
    def from_env(cls) -> Config:
        """Resolve the configuration from ``KE_*`` env vars. Side-effect free."""
        resolved = paths.resolve()
        return cls(
            corpus_root=resolved.corpus_root,
            data_dir=resolved.data_dir,
            registry_path=resolved.registry_path,
        )

    def ensure_dirs(self) -> None:
        """Create the runtime data directory. Idempotent."""
        paths.ensure_data_dir(self.data_dir)
