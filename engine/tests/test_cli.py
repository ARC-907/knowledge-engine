"""The ``knowledge-engine`` CLI package: help snapshot, lazy dispatch, logging flags.

The ``--help`` text of every subcommand is pinned in ``tests/snapshots/cli_help.txt``
(rendered at 80 columns without colour). Changes must be additions only; to
accept one, regenerate and review the diff::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_cli.py

(run from ``engine/``). Python 3.12's argparse wraps long usage lines slightly
differently from 3.13+, so on 3.12 the comparison ignores line wrapping.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine import cli

SNAPSHOT = Path(__file__).parent / "snapshots" / "cli_help.txt"
SRC = Path(__file__).resolve().parents[1] / "src"

COMMANDS: list[list[str]] = [
    [],
    ["info"],
    ["bootstrap"],
    ["capabilities"],
    ["reindex"],
    ["watch"],
    ["config"],
    ["config", "show"],
    ["mcp"],
    ["search"],
    ["serve"],
    ["project-context"],
    ["board"],
    ["board-serve"],
]


def _render_help(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("PYTHON_COLORS", "0")
    chunks: list[str] = []
    for command in COMMANDS:
        argv = [*command, "--help"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(argv)
        assert exc.value.code == 0
        chunks.append(f"### knowledge-engine {' '.join(argv)}\n{buf.getvalue()}")
    return "".join(chunks)


def _unwrapped(text: str) -> str:
    return " ".join(text.split())


def test_help_matches_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = _render_help(monkeypatch)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    expected = SNAPSHOT.read_text(encoding="utf-8")
    if sys.version_info >= (3, 13):
        assert rendered == expected, "CLI help changed; see the module docstring"
    else:
        assert _unwrapped(rendered) == _unwrapped(expected), "CLI help changed"


def test_every_command_is_routed() -> None:
    parser = cli.build_parser()
    for command, extra in (
        (["info"], []),
        (["bootstrap"], []),
        (["capabilities"], []),
        (["reindex"], []),
        (["watch"], []),
        (["config", "show"], []),
        (["mcp"], []),
        (["search"], ["q"]),
        (["serve"], []),
        (["project-context"], ["info"]),
        (["board"], ["status"]),
        (["board-serve"], []),
    ):
        args = parser.parse_args([*command, *extra])
        module, func = args._handler
        assert callable(cli._load_handler(module, func)), command


def test_importing_the_cli_is_lazy() -> None:
    probe = (
        "import sys\n"
        "import knowledge_engine.cli as cli\n"
        "cli.build_parser()\n"
        "heavy = [m for m in ('fastapi', 'uvicorn', 'mcp.server', 'pydantic', 'httpx',\n"
        "    'knowledge_engine.core.registry', 'knowledge_engine.retrieval.fts5',\n"
        "    'knowledge_engine.agent_board', 'knowledge_engine.project_context',\n"
        "    'knowledge_engine.cli.corpus') if m in sys.modules]\n"
        "assert heavy == [], heavy\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_python_dash_m_runs_the_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "knowledge_engine.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "board-serve" in proc.stdout


def test_legacy_module_attributes() -> None:
    from knowledge_engine.cli import _capability_inventory

    assert cli.BASE_MCP_TOOLS == ["search", "registry_list", "registry_toggle", "registry_get"]
    assert callable(_capability_inventory)
    assert callable(cli._env_port)
    assert callable(cli._bootstrap)
    with pytest.raises(AttributeError):
        _ = cli.nope  # type: ignore[attr-defined]


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "libraries" / "Alpha Lib").mkdir(parents=True)
    (corpus / "libraries" / "Alpha Lib" / "a.md").write_text("alpha fox", encoding="utf-8")
    (corpus / "kits" / "kit one").mkdir(parents=True)
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(tmp_path / "pipeline"))
    return corpus


def test_bootstrap_reindex_search_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    assert cli.main(["bootstrap"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "library": 1,
        "skill": 0,
        "kit": 1,
        "tool": 0,
        "skipped": 0,
    }
    assert cli.main(["bootstrap"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 2
    assert cli.main(["reindex"]) == 0
    assert json.loads(capsys.readouterr().out) == {"entries": 2, "files": 1, "missing": 0}
    assert cli.main(["search", "fox"]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert [h["entry_id"] for h in hits] == ["library-alpha-lib"]
    assert cli.main(["info"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["counts"] == {"libraries": 1, "skills": 0, "kits": 1, "tools": 0}
    assert not (tmp_path / "data" / "registry.db").exists()


def test_search_syntax_fallback_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    cli.main(["bootstrap"])
    cli.main(["reindex"])
    capsys.readouterr()
    assert cli.main(["search", '"fox']) == 0
    captured = capsys.readouterr()
    assert isinstance(json.loads(captured.out), list)
    assert "searched it as a phrase" in captured.err


def test_config_show_does_not_open_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    from knowledge_engine.core import registry

    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("config show must not load the registry")

    monkeypatch.setattr(registry.Registry, "__init__", forbidden)
    assert cli.main(["config", "show"]) == 0
    assert "settings" in json.loads(capsys.readouterr().out)
    assert not (tmp_path / "data").exists()


def test_main_loads_the_pipeline_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / ".env").write_text("KE_CLI_DOTENV_PROBE=1\n", encoding="utf-8")
    monkeypatch.delenv("KE_CLI_DOTENV_PROBE", raising=False)
    assert cli.main(["config", "show"]) == 0
    assert os.environ.pop("KE_CLI_DOTENV_PROBE") == "1"


@pytest.mark.parametrize(
    ("flags", "level"),
    [
        ([], logging.WARNING),
        (["-v"], logging.INFO),
        (["-vv"], logging.DEBUG),
        (["-q"], logging.ERROR),
    ],
)
def test_verbosity_flags_configure_root_logging_once(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], level: int
) -> None:
    seen: list[tuple[int, int]] = []

    def probe(_args: Any, _parser: Any) -> int:
        root = logging.getLogger()
        handlers = [h for h in root.handlers if type(h).__name__ == "_StderrHandler"]
        seen.append((root.level, len(handlers)))
        return 0

    monkeypatch.setattr(cli, "_load_handler", lambda _m, _f: probe)
    before = logging.getLogger().level
    assert cli.main([*flags, "info"]) == 0
    assert seen == [(level, 1)]
    after = logging.getLogger()
    assert after.level == before
    assert not [h for h in after.handlers if type(h).__name__ == "_StderrHandler"]


def test_long_running_commands_default_to_ke_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(
        cli,
        "_load_handler",
        lambda _m, _f: lambda _a, _p: seen.append(logging.getLogger().level) or 0,
    )
    monkeypatch.setenv("KE_LOG_LEVEL", "DEBUG")
    assert cli.main(["watch"]) == 0
    assert cli.main(["-q", "watch"]) == 0
    monkeypatch.delenv("KE_LOG_LEVEL")
    assert cli.main(["board-serve"]) == 0
    assert seen == [logging.DEBUG, logging.ERROR, logging.INFO]


def test_verbose_and_quiet_are_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["-v", "-q", "info"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_cli_log_level_mapping() -> None:
    from knowledge_engine.core.logging_config import cli_log_level

    assert [cli_log_level(n) for n in (-3, -2, -1, 0, 1, 2, 5)] == [
        "CRITICAL",
        "CRITICAL",
        "ERROR",
        "WARNING",
        "INFO",
        "DEBUG",
        "DEBUG",
    ]


def test_cli_handler_follows_swapped_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    from knowledge_engine.core.logging_config import configure_cli_logging

    restore = configure_cli_logging("INFO")
    try:
        logging.getLogger("knowledge_engine.test").info("hello from the cli handler")
    finally:
        restore()
    assert "hello from the cli handler" in capsys.readouterr().err


def test_serve_and_board_serve_pass_the_verbosity_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    from knowledge_engine.agent_board import service

    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    assert cli.main(["-vv", "serve"]) == 0
    assert runs[0]["log_config"]["loggers"]["knowledge_engine"]["level"] == "DEBUG"

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(service, "serve", lambda **kw: calls.append(kw) or 0)
    assert cli.main(["-q", "board-serve"]) == 0
    assert calls[0]["log_level"] == "ERROR"
