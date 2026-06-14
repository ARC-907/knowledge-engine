"""Tests for the project-docs TOML config layer."""

from __future__ import annotations

from pathlib import Path

from knowledge_engine.project_docs.config import (
    PROJECT_DOCS_DEFAULTS,
    find_config_file,
    load_config,
)


def test_absent_file_yields_safe_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    cfg = load_config(start=tmp_path)
    assert cfg.enabled is False
    assert cfg.scanner.enabled is False
    assert cfg.scanner.pointer_replacement.enabled is False
    assert cfg.scanner.pointer_replacement.allow_source_mutation is False
    assert cfg.embeddings.enabled is False
    assert cfg.ingestion.retain_raw_content is False
    assert cfg.git.include_full_diffs is False
    assert cfg.mcp.default_result_mode == "summary"
    assert cfg.mcp.allow_mutating_tools is False


def test_partial_toml_overrides_only_named_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    toml = tmp_path / "knowledge-engine.toml"
    toml.write_text(
        "[project_docs]\nenabled = true\n\n[project_docs.scanner]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(path=toml)
    assert cfg.enabled is True
    assert cfg.scanner.enabled is True
    # Siblings keep their conservative defaults.
    assert cfg.scanner.dry_run is True
    assert cfg.embeddings.enabled is False
    assert cfg.mcp.allow_full_content is True


def test_find_config_file_walks_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    (tmp_path / "knowledge-engine.toml").write_text("[project_docs]\n", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    found = find_config_file(start=deep)
    assert found == tmp_path / "knowledge-engine.toml"


def test_find_config_file_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KE_CONFIG_PATH", raising=False)
    assert find_config_file(start=tmp_path) is None


def test_defaults_dict_matches_dataclass() -> None:
    assert PROJECT_DOCS_DEFAULTS["enabled"] is False
    assert PROJECT_DOCS_DEFAULTS["scanner"]["pointer_replacement"]["pointer_scheme"] == "KE-DOCSTRING"
