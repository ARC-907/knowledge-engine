"""runtime.foundation.config: lazy pipeline paths and the explicit .env loader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from knowledge_engine.runtime.foundation import config


def test_pipeline_paths_are_resolved_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KE_PIPELINE_ROOT", raising=False)
    monkeypatch.delenv("KE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.pipeline_root() == (tmp_path / "pipeline").resolve()
    assert (tmp_path / "pipeline").resolve() == config.PIPELINE_ROOT
    assert (tmp_path / "pipeline" / "config").resolve() == config.CONFIG_DIR

    other = tmp_path / "elsewhere"
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(other))
    assert other.resolve() == config.PIPELINE_ROOT
    assert config.config_dir() == (other / "config").resolve()
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert config.config_dir() == (tmp_path / "cfg").resolve()
    assert config.config_dir({"KE_PIPELINE_ROOT": "/p"}) == Path("/p/config").resolve()
    with pytest.raises(AttributeError):
        _ = config.NOT_A_SETTING  # type: ignore[attr-defined]


def test_yaml_loader_follows_the_live_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "nodes.yaml").write_text("nodes:\n  a:\n    enabled: true\n", encoding="utf-8")
    assert config.get_enabled_nodes() == {"a": {"enabled": True}}


def test_load_dotenv_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n\nKE_A = one \nKE_B=two=three\nnot a pair\nKE_KEEP=from-file\n=orphan\n",
        encoding="utf-8",
    )
    environ = {"KE_KEEP": "from-env"}
    with caplog.at_level(logging.INFO, logger="knowledge_engine.runtime.foundation.config"):
        loaded = config.load_dotenv_file(dotenv, environ=environ)
    assert loaded == {"KE_A": "one", "KE_B": "two=three"}
    assert environ == {"KE_KEEP": "from-env", "KE_A": "one", "KE_B": "two=three"}
    assert "KE_A" in caplog.text
    assert "two=three" not in caplog.text  # values are never logged


def test_load_dotenv_file_missing_and_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert config.load_dotenv_file(tmp_path / "absent.env", environ={}) == {}
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("KE_DOTENV_PROBE=yes\n", encoding="utf-8")
    monkeypatch.delenv("KE_DOTENV_PROBE", raising=False)
    environ: dict[str, str] = {}
    assert config.default_dotenv_path() == (tmp_path / ".env").resolve()
    assert config.load_dotenv_file(environ=environ) == {"KE_DOTENV_PROBE": "yes"}


def test_foundation_db_path_attribute_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.runtime.foundation import db

    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "x.db"))
    assert (tmp_path / "x.db").resolve() == db.DB_PATH
    with pytest.raises(AttributeError):
        _ = db.NOPE  # type: ignore[attr-defined]


def test_pipeline_package_is_lazy() -> None:
    from knowledge_engine.runtime import pipeline

    assert "queue" in dir(pipeline)
    assert pipeline.queue.__name__ == "knowledge_engine.runtime.pipeline.queue"
    with pytest.raises(AttributeError):
        _ = pipeline.not_a_module  # type: ignore[attr-defined]
