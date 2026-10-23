"""Board sweeper lifecycle: KE_BOARD_SWEEPER gating and test-time state reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowledge_engine.agent_board import sweeper


@pytest.fixture
def board_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))
    return tmp_path


# ── KE_BOARD_SWEEPER gating ──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("0", False), ("off", False), ("", False), ("1", True), (" yes ", True)],
)
def test_env_override_parsing(
    raw: str | None, expected: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is None:
        monkeypatch.delenv("KE_BOARD_SWEEPER", raising=False)
    else:
        monkeypatch.setenv("KE_BOARD_SWEEPER", raw)
    assert sweeper.env_override() is expected


@pytest.mark.usefixtures("board_env")
def test_suite_default_keeps_lifespan_sweeper_off() -> None:
    from knowledge_engine.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert not sweeper.is_running()


@pytest.mark.usefixtures("board_env")
def test_lifespan_sweeper_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.app import create_app

    monkeypatch.setenv("KE_BOARD_SWEEPER", "1")
    with TestClient(create_app()):
        assert sweeper.is_running()
    sweeper.stop()
    assert not sweeper.is_running()


@pytest.mark.usefixtures("board_env")
def test_lifespan_sweeper_defaults_on_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators who never set KE_BOARD_SWEEPER keep today's behaviour: on."""
    from knowledge_engine.app import create_app

    monkeypatch.delenv("KE_BOARD_SWEEPER", raising=False)
    with TestClient(create_app()):
        assert sweeper.is_running()


@pytest.mark.usefixtures("board_env")
@pytest.mark.parametrize(("raw", "running"), [("0", False), ("1", True)])
def test_standalone_service_honours_env(
    raw: str, running: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    monkeypatch.setenv("KE_BOARD_SWEEPER", raw)
    with TestClient(create_standalone_app()):
        assert sweeper.is_running() is running


# ── Module state reset ───────────────────────────────────────


@pytest.mark.usefixtures("board_env")
def test_reset_for_tests_stops_and_clears_state() -> None:
    assert sweeper.start() is True
    old_event = sweeper._stop_event

    sweeper.reset_for_tests()

    assert not sweeper.is_running()
    assert sweeper._thread is None
    assert old_event.is_set()
    assert sweeper._stop_event is not old_event
    assert not sweeper._stop_event.is_set()
    # A fresh start works after a reset.
    assert sweeper.start() is True
    assert sweeper.stop() is True
