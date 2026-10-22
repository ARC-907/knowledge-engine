"""The FTS5 indexer skips, counts, and warns about registry entries whose content is missing."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from knowledge_engine.core.config import Config
from knowledge_engine.core.registry import Registry, RegistryEntry
from knowledge_engine.retrieval.fts5 import Indexer

_FTS5_LOGGER = "knowledge_engine.retrieval.fts5"


def test_rebuild_warns_when_library_missing_from_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    present = corpus / "libraries" / "present"
    present.mkdir(parents=True)
    (present / "note.md").write_text("present library content", encoding="utf-8")
    data.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))

    config = Config.from_env()
    reg = Registry(config.registry_path, config.data_dir / "registry.db")
    reg.upsert(
        RegistryEntry(id="lib-present", kind="library", name="Present", path="libraries/present")
    )
    reg.upsert(RegistryEntry(id="lib-gone", kind="library", name="Gone", path="libraries/gone"))
    idx = Indexer(config, reg)

    with caplog.at_level(logging.WARNING, logger=_FTS5_LOGGER):
        counts = idx.rebuild()

    # The missing entry is skipped without aborting the rebuild.
    assert counts == {"entries": 2, "files": 1, "missing": 1}

    warnings = [r for r in caplog.records if r.name == _FTS5_LOGGER]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    message = warnings[0].getMessage()
    assert "'Gone'" in message
    assert "missing from the corpus" in message
    assert str((corpus / "libraries" / "gone").resolve()) in message
    assert "KE_CORPUS_ROOT" in message
