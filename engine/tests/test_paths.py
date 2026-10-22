"""Default path resolution (core/paths.py) and its consumers."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from knowledge_engine.core import paths
from knowledge_engine.core.config import Config


def _make_repo(root: Path) -> Path:
    """A minimal engine checkout: ``<root>/corpus/registry.json``."""
    (root / "corpus").mkdir(parents=True)
    (root / "corpus" / "registry.json").write_text(
        json.dumps({"libraries": [], "skills": [], "kits": [], "tools": []}), encoding="utf-8"
    )
    return root.resolve()


def _tree(root: Path) -> set[Path]:
    return set(root.rglob("*"))


@pytest.fixture
def no_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise discovery: drop the per-test KE_HOME the root conftest sets."""
    monkeypatch.delenv("KE_HOME", raising=False)


@pytest.mark.usefixtures("no_home")
def test_default_config_discovers_repo_from_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "repo")
    workdir = repo / "engine" / "src"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)

    config = Config.from_env()

    assert config.corpus_root == repo / "corpus"
    assert config.registry_path == repo / "corpus" / "registry.json"
    assert config.data_dir == repo / ".knowledge-engine" / "data"


@pytest.mark.usefixtures("no_home")
def test_from_env_creates_no_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    before = _tree(tmp_path)

    config = Config.from_env()

    assert _tree(tmp_path) == before
    assert not config.data_dir.exists()


def test_ensure_dirs_creates_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "state" / "data"))
    config = Config.from_env()
    assert not config.data_dir.exists()

    config.ensure_dirs()
    config.ensure_dirs()  # idempotent

    assert config.data_dir.is_dir()


@pytest.mark.usefixtures("no_home")
def test_home_falls_back_to_cwd_without_a_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = (tmp_path / "plain").resolve()
    workdir.mkdir()
    if any((d / "corpus" / "registry.json").is_file() for d in workdir.parents):
        pytest.skip("an ancestor of the pytest tmp dir looks like an engine home")
    monkeypatch.chdir(workdir)

    resolved = paths.resolve()

    assert resolved.home == workdir
    assert resolved.corpus_root == workdir / "corpus"
    assert resolved.data_dir == workdir / ".knowledge-engine" / "data"


def test_ke_home_overrides_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("KE_HOME", str(elsewhere))

    resolved = paths.resolve()

    assert resolved.home == elsewhere.resolve()
    assert resolved.corpus_root == elsewhere.resolve() / "corpus"
    assert resolved.data_dir == elsewhere.resolve() / ".knowledge-engine" / "data"


def test_explicit_env_vars_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CORPUS_ROOT", str(tmp_path / "c"))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(tmp_path / "r" / "reg.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "d"))

    config = Config.from_env()

    assert config.corpus_root == (tmp_path / "c").resolve()
    assert config.registry_path == (tmp_path / "r" / "reg.json").resolve()
    assert config.data_dir == (tmp_path / "d").resolve()


def test_registry_defaults_under_explicit_corpus_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_CORPUS_ROOT", str(tmp_path / "c"))
    assert Config.from_env().registry_path == (tmp_path / "c" / "registry.json").resolve()


def test_blank_env_values_are_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("KE_HOME", str(home))
    monkeypatch.setenv("KE_DATA_DIR", "   ")
    monkeypatch.setenv("KE_CORPUS_ROOT", "")

    resolved = paths.resolve()

    assert resolved.corpus_root == home.resolve() / "corpus"
    assert resolved.data_dir == home.resolve() / ".knowledge-engine" / "data"


def test_missing_corpus_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(paths, "_warned_missing_corpus", set())
    monkeypatch.setenv("KE_CORPUS_ROOT", str(tmp_path / "nope"))

    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.paths"):
        Config.from_env()
        Config.from_env()

    warnings = [r for r in caplog.records if "does not exist" in r.getMessage()]
    assert len(warnings) == 1
    assert str((tmp_path / "nope").resolve()) in warnings[0].getMessage()


def test_existing_corpus_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(paths, "_warned_missing_corpus", set())
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("KE_HOME", str(repo))

    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.paths"):
        Config.from_env()

    assert not [r for r in caplog.records if r.name == "knowledge_engine.core.paths"]


# ── Consumers share the one data dir ─────────────────────────


def test_pipeline_db_defaults_into_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.runtime.foundation import db

    monkeypatch.delenv("KE_PIPELINE_DB", raising=False)
    monkeypatch.setenv("KE_HOME", str(tmp_path / "home"))

    expected = (tmp_path / "home").resolve() / ".knowledge-engine" / "data" / "pipeline.db"
    assert Path(db.resolve_db_path()) == expected

    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "d"))
    assert Path(db.resolve_db_path()) == (tmp_path / "d").resolve() / "pipeline.db"

    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "explicit.db"))
    assert Path(db.resolve_db_path()) == (tmp_path / "explicit.db").resolve()


def test_board_scopes_live_under_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.agent_board import scopes

    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "d"))
    assert scopes.scopes_root() == (tmp_path / "d").resolve() / "board-scopes"


def test_master_key_file_lands_in_created_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.agent_board import keys

    data = tmp_path / "fresh" / "data"
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "pipeline.db"))

    created = keys.ensure_master_key()

    assert created is not None
    key_file = data / "board-master-key.txt"
    assert key_file.is_file()
    assert created["raw_key"] in key_file.read_text(encoding="utf-8")


# ── Entry points create the data dir explicitly ──────────────


def test_create_app_creates_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.app import create_app

    data = tmp_path / "new" / "data"
    monkeypatch.setenv("KE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))

    create_app()

    assert (data / "registry.db").is_file()


def test_mcp_server_creates_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.mcp.server import Server

    data = tmp_path / "new" / "data"
    monkeypatch.setenv("KE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))

    Server()

    assert (data / "registry.db").is_file()


def test_cli_info_creates_data_dir_and_reports_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from knowledge_engine import cli

    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("KE_HOME", str(repo))
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "info"])

    assert cli.main() == 0

    out = json.loads(capsys.readouterr().out)
    assert out["corpus_root"] == str(repo / "corpus")
    assert out["data_dir"] == str(repo / ".knowledge-engine" / "data")
    assert (repo / ".knowledge-engine" / "data" / "registry.db").is_file()
