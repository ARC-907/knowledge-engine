"""Shared fixtures of the module tests: an isolated engine tree with copies of the modules.

The two reference modules (``examples/modules``) and the ``probe`` test
module (``tests/fixtures/modules/probe``) are copied into a per-test
``KE_MODULES_DIR`` so nothing is written into the repository.
"""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "examples" / "modules"
PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "modules" / "probe"
SECRET = "tok-SECRET-9f8e7d6c5b4a"


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


@dataclass
class ModEnv:
    root: Path
    corpus: Path
    data: Path
    modules: Path

    @property
    def news(self) -> Path:
        return self.modules / "news-fetcher"

    @property
    def transcription(self) -> Path:
        return self.modules / "transcription"

    @property
    def probe(self) -> Path:
        return self.modules / "probe"

    def fixture_uri(self, module: str, name: str) -> str:
        return (self.modules / module / "fixtures" / name).as_uri()


@pytest.fixture
def mod_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModEnv:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    modules = tmp_path / "operator-modules"
    corpus.mkdir()
    data.mkdir()
    modules.mkdir()
    shutil.copytree(EXAMPLES / "news-fetcher", modules / "news-fetcher")
    shutil.copytree(EXAMPLES / "transcription", modules / "transcription")
    shutil.copytree(PROBE, modules / "probe")
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_MODULES_DIR", str(modules))
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent.toml"))
    monkeypatch.chdir(tmp_path)
    return ModEnv(tmp_path, corpus, data, modules)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ingestion() -> Any:
    from knowledge_engine.modules.ingestion import InMemoryIngestion

    return InMemoryIngestion(workspaces={"default", "research"}, projects={("research", "p1")})


@pytest.fixture
def make_host(mod_env: ModEnv, clock: Clock, ingestion: Any) -> Iterator[Callable[..., Any]]:
    from knowledge_engine.modules.config import ModulesConfig
    from knowledge_engine.modules.dispatcher import ModuleHost

    opened: list[Any] = []

    def make(**overrides: Any) -> Any:
        config = ModulesConfig.from_env()
        if "host_capabilities" in overrides:
            from dataclasses import replace

            config = replace(
                config, host_capabilities=frozenset(overrides.pop("host_capabilities"))
            )
        kwargs: dict[str, Any] = {
            "ingestion": ingestion,
            "clock": clock,
            "environ": {"PROBE_TOKEN": SECRET},
        }
        kwargs.update(overrides)
        host = ModuleHost.open(config, **kwargs)
        opened.append(host)
        return host

    yield make
    for host in opened:
        host.close(drain_s=2.0)


@pytest.fixture
def host(make_host: Callable[..., Any]) -> Any:
    return make_host()


def enable(host: Any, directory: Path, **settings: Any) -> dict[str, Any]:
    """Register, optionally configure, and enable the module at ``directory``."""
    info = host.register(directory)
    if settings:
        host.configure(info["module_id"], settings)
    return host.enable(info["module_id"])  # type: ignore[no-any-return]


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def wait_job(
    host: Any, job_id: str, workspace: str = "default", timeout: float = 20.0
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def done() -> bool:
        nonlocal result
        result = host.job_status(job_id, workspace)
        return result["state"] in {"succeeded", "failed", "cancelled", "expired"}

    wait_for(done, timeout)
    return result


def process_alive(pid: int) -> bool:
    """True while ``pid`` runs (a zombie counts as dead)."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    if sys.platform == "win32":
        return _windows_process_alive(pid)
    import os

    try:
        os.kill(pid, 0)  # POSIX only: on Windows signal 0 is CTRL_C_EVENT
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@pytest.fixture
def helpers() -> Any:
    """The helper functions above (test modules cannot import a conftest)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        enable=enable,
        wait_for=wait_for,
        wait_job=wait_job,
        process_alive=process_alive,
        SECRET=SECRET,
        EXAMPLES=EXAMPLES,
        PROBE=PROBE,
    )
