"""Typed settings layer: env names, parsing, masking, paths, CLI and the generated reference."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest

from knowledge_engine.core import settings as settings_mod
from knowledge_engine.core.config import Config
from knowledge_engine.core.settings import MASK, Settings, env_flag, parse_env_bool

SRC = Path(__file__).resolve().parents[1] / "src" / "knowledge_engine"
REFERENCE = Path(__file__).resolve().parents[2] / "01-user-docs" / "CONFIGURATION.md"

# ``KE_*`` string literals in the source that are not environment variables.
NOT_ENV_VARS = {"KE_DOC_SCHEME", "KE_"}


def test_every_env_var_read_by_the_package_is_declared() -> None:
    """Grep the package for ``KE_*`` names; each must be a Settings field."""
    declared = set(Settings.env_names().values())
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        found |= set(re.findall(r"\bKE_[A-Z0-9_]+\b", path.read_text(encoding="utf-8")))
    missing = sorted(found - declared - NOT_ENV_VARS)
    assert not missing, f"KE_* names read in the package but not declared on Settings: {missing}"
    for legacy in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_TIMEOUT", "BOARD_HOST", "BOARD_PORT"):
        assert legacy in declared


def test_defaults_match_the_historical_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py sets these two for isolation; check the real defaults.
    monkeypatch.delenv("KE_BOARD_SWEEPER", raising=False)
    monkeypatch.delenv("KE_PROVIDER_STARTUP_PROBE", raising=False)
    s = Settings()
    assert s.provider_startup_probe is True
    assert s.bind_host == "127.0.0.1"
    assert s.board_enabled is True
    assert s.board_sweeper is None
    assert s.board_trusted_cidrs is None
    assert s.trust_proxy is False
    assert s.trusted_proxies is None
    assert s.mcp_http is False
    assert s.log_level == "INFO"
    assert s.board_max_scopes == 64
    assert s.board_standalone_port == 11437
    assert s.provider_probe_ttl == 30.0
    assert s.cloud_timeout == 60.0
    assert s.ollama_base_url == "http://127.0.0.1:11434"
    assert s.ollama_model == "llama3.2"
    assert s.ollama_timeout == 30.0
    assert s.cloud_configured() is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        (" TRUE ", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("off", False),
        ("", False),
    ],
)
def test_shared_bool_parser(raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_env_bool(raw) is expected
    for env, field in (
        ("KE_TRUST_PROXY", "trust_proxy"),
        ("KE_BOARD_ENABLED", "board_enabled"),
        ("KE_MCP_HTTP", "mcp_http"),
        ("KE_BOARD_SWEEPER", "board_sweeper"),
    ):
        monkeypatch.setenv(env, raw)
        assert getattr(Settings(), field) is expected, env


def test_unknown_flag_values_warn_and_use_the_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KE_BOARD_ENABLED", "maybe")
    monkeypatch.setenv("KE_TRUST_PROXY", "sure")
    monkeypatch.setenv("KE_BOARD_SWEEPER", "perhaps")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.settings"):
        s = Settings()
    assert s.board_enabled is True
    assert s.trust_proxy is False
    assert s.board_sweeper is None
    messages = " ".join(r.getMessage() for r in caplog.records)
    for name in ("KE_BOARD_ENABLED", "KE_TRUST_PROXY", "KE_BOARD_SWEEPER"):
        assert name in messages
    assert env_flag("garbage", default=True) is True
    assert env_flag(None, default=False) is False


def test_invalid_numbers_fall_back_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KE_PROVIDER_PROBE_TTL", "-5")
    monkeypatch.setenv("KE_CLOUD_TIMEOUT", "soon")
    monkeypatch.setenv("KE_PORT", "lots")
    monkeypatch.setenv("KE_LOG_LEVEL", "loud")
    monkeypatch.setenv("KE_BOARD_MAX_SCOPES", "-3")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.settings"):
        s = Settings()
    assert s.provider_probe_ttl == 30.0
    assert s.cloud_timeout == 60.0
    assert s.port is None
    assert s.log_level == "INFO"
    # Same rule as agent_board.scopes.max_scopes(): negative means unlimited.
    assert s.board_max_scopes == 0
    assert len(caplog.records) == 4


def test_secrets_are_masked_and_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KE_CLOUD_API_KEY", "sk-very-secret")
    monkeypatch.setenv("KE_BOARD_KEY", "keb_secret")
    with caplog.at_level(logging.DEBUG):
        s = Settings()
        shown = s.masked()
    assert shown["KE_CLOUD_API_KEY"] == MASK
    assert shown["KE_BOARD_KEY"] == MASK
    assert shown["KE_OPENAI_API_KEY"] is None
    assert "sk-very-secret" not in json.dumps(shown)
    assert "sk-very-secret" not in repr(s)
    assert "sk-very-secret" not in caplog.text
    assert "KE_CLOUD_API_KEY" in s.explicitly_set()


def test_exact_env_names_are_case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ke_bind_host", "10.0.0.1")
    assert Settings().bind_host == "127.0.0.1"
    monkeypatch.setenv("KE_BIND_HOST", "  ")
    assert Settings().bind_host == "127.0.0.1"
    monkeypatch.setenv("KE_BIND_HOST", "0.0.0.0")  # noqa: S104 -- value under test
    assert Settings().bind_host == "0.0.0.0"  # noqa: S104


def test_paths_compose_the_core_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "c"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "d"))
    s = Settings()
    assert s.paths.corpus_root == corpus.resolve()
    assert s.paths.registry_path == corpus.resolve() / "registry.json"
    assert s.paths.data_dir == (tmp_path / "d").resolve()
    assert Config.from_settings(s) == Config.from_env()
    # Keyword construction (tests, embedding code) overrides the environment.
    other = Settings(data_dir=str(tmp_path / "elsewhere"))
    assert Config.from_settings(other).data_dir == (tmp_path / "elsewhere").resolve()
    assert not (tmp_path / "d").exists()


def test_provider_order_and_cloud_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_PROVIDER_ORDER", " Ollama , ,echo")
    monkeypatch.setenv("KE_CLOUD_ENDPOINT", "https://api.example/v1")
    s = Settings()
    assert s.provider_order_list() == ["ollama", "echo"]
    assert s.cloud_configured() is False
    monkeypatch.setenv("KE_CLOUD_API_KEY", "k")
    assert Settings().cloud_configured() is True


def test_ollama_variables_keep_their_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "5")
    s = Settings()
    assert s.ollama_base_url == "http://gpu-box:11434"
    assert s.ollama_timeout == 5.0


def test_config_show_cli_masks_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from knowledge_engine import cli

    data = tmp_path / "never-created"
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_CLOUD_API_KEY", "sk-cli-secret")
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "config", "show"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["settings"]["KE_CLOUD_API_KEY"] == MASK
    assert report["settings"]["KE_BIND_HOST"] == "127.0.0.1"
    assert report["paths"]["data_dir"] == str(data.resolve())
    assert "KE_DATA_DIR" in report["set_in_environment"]
    assert "sk-cli-secret" not in out
    # `config show` is read-only.
    assert not data.exists()


def test_config_show_markdown_and_module_entry_point(capsys: pytest.CaptureFixture[str]) -> None:
    from knowledge_engine import cli

    sys_argv = ["knowledge-engine", "config", "show", "--markdown"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", sys_argv)
        assert cli.main() == 0
    via_cli = capsys.readouterr().out
    assert settings_mod.main(["--markdown"]) == 0
    assert capsys.readouterr().out == via_cli == settings_mod.markdown_reference()
    assert settings_mod.main(["--show"]) == 0
    assert "KE_BIND_HOST" in json.loads(capsys.readouterr().out)


def test_reference_lists_every_setting() -> None:
    text = settings_mod.markdown_reference()
    for env_name in Settings.env_names().values():
        assert f"| `{env_name}` |" in text


def test_committed_configuration_reference_is_current() -> None:
    """01-user-docs/CONFIGURATION.md is generated; regenerate it after changing Settings:

    ``python -m knowledge_engine.core.settings --markdown > 01-user-docs/CONFIGURATION.md``
    """
    if not REFERENCE.is_file():
        pytest.skip("repository docs not present (sdist install)")
    assert REFERENCE.read_text(encoding="utf-8") == settings_mod.markdown_reference()
