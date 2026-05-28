"""Configuration loaded from environment. No hardcoded paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    corpus_root: Path
    data_dir: Path
    registry_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        # corpus_root defaults to ../corpus relative to engine/
        engine_dir = Path(__file__).resolve().parents[2]
        corpus_root = Path(
            os.environ.get("KE_CORPUS_ROOT", engine_dir.parent / "corpus")
        ).resolve()
        data_dir = Path(
            os.environ.get("KE_DATA_DIR", engine_dir / "data")
        ).resolve()
        registry_path = Path(
            os.environ.get("KE_REGISTRY_PATH", corpus_root / "registry.json")
        ).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(corpus_root=corpus_root, data_dir=data_dir, registry_path=registry_path)
