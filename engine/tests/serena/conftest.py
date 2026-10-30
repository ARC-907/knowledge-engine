"""Shared fixtures of the Serena adapter tests: git projects and a fake Serena server.

The fake (``fake_serena.py``) is a real stdio MCP server built with the MCP
SDK; it impersonates Serena 2.1.0 (name, version, tool list, recorded input
schemas) and answers from the Python files of its ``--project``. Projects are
temporary git repositories written with exact bytes (no newline translation).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge_engine.serena.config import SerenaConfig, SerenaProject
from knowledge_engine.serena.manager import SerenaManager
from knowledge_engine.serena.session import SerenaSession

FAKE = Path(__file__).resolve().parent / "fake_serena.py"

SHAPES_A = b"""class Shape:
    def area(self):
        return 0


def make_a():
    return Shape()
"""
SHAPES_B = b"""class Shape:
    def perimeter(self):
        return 4


def use_shape():
    return Shape().perimeter()
"""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_repo(root: Path, files: dict[str, bytes]) -> Path:
    """A git repository at ``root`` with ``files`` committed (exact bytes)."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.org")
    git(root, "config", "user.name", "Tests")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def kit() -> SimpleNamespace:
    """Helpers for test modules (they cannot import this conftest directly)."""
    return SimpleNamespace(
        git=git,
        make_repo=make_repo,
        read_log=read_log,
        SHAPES_A=SHAPES_A,
        SHAPES_B=SHAPES_B,
        FAKE=FAKE,
        has_git=shutil.which("git") is not None,
    )


@pytest.fixture
def projects(tmp_path: Path) -> dict[str, SerenaProject]:
    a = make_repo(tmp_path / "repo-a", {"pkg/shapes.py": SHAPES_A})
    b = make_repo(tmp_path / "repo-b", {"pkg/shapes.py": SHAPES_B, "pkg/use.py": b"x = 1\n"})
    return {
        "proj-a": SerenaProject("proj-a", a.resolve(), "example.org/a"),
        "proj-b": SerenaProject("proj-b", b.resolve(), "example.org/b"),
    }


@pytest.fixture
def call_log(tmp_path: Path) -> Path:
    return tmp_path / "fake-calls.jsonl"


@pytest.fixture
def make_config(tmp_path: Path, projects: dict[str, SerenaProject]) -> Callable[..., SerenaConfig]:
    def make(**overrides: Any) -> SerenaConfig:
        values: dict[str, Any] = {
            "enabled": True,
            "command": (sys.executable, str(FAKE)),
            "expected_version": "2.1.0",
            "projects_file": tmp_path / "serena-projects.toml",
            "state_dir": tmp_path / "serena-state",
            "request_timeout_s": 20.0,
            "startup_timeout_s": 60.0,
            "max_sessions": 4,
            "idle_shutdown_s": 0,
            "projects": dict(projects),
        }
        values.update(overrides)
        return SerenaConfig(**values)

    return make


@pytest.fixture
def fake_env(call_log: Path, tmp_path: Path) -> Callable[..., dict[str, str]]:
    """The base environment for fake sessions (mode switches on top of os.environ)."""
    import os

    def make(mode: str = "normal", **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["FAKE_SERENA_MODE"] = mode
        env["FAKE_SERENA_LOG"] = str(call_log)
        env["FAKE_SERENA_STATE"] = str(tmp_path / "fake-state")
        env.update(extra)
        return env

    return make


@pytest.fixture
def make_manager(
    make_config: Callable[..., SerenaConfig], fake_env: Callable[..., dict[str, str]]
) -> Iterator[Callable[..., SerenaManager]]:
    opened: list[SerenaManager] = []

    def make(
        mode: str = "normal", env: dict[str, str] | None = None, **config: Any
    ) -> SerenaManager:
        cfg = make_config(**config)
        environment = env if env is not None else fake_env(mode)

        def factory(project: SerenaProject, conf: SerenaConfig) -> SerenaSession:
            return SerenaSession(project, conf, environment=environment)

        manager = SerenaManager(cfg, session_factory=factory, start_reaper=False)
        opened.append(manager)
        return manager

    yield make
    for manager in opened:
        manager.close()
