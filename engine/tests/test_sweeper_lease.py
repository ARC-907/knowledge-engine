"""Sweeper lease robustness: a corrupt `board.sweeper_lease` row is no lease."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_engine.runtime.foundation import db

LEASE_KEY = "board.sweeper_lease"


@pytest.fixture
def board_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))
    return tmp_path


def _store_lease(value: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO kv_store(key, value, updated_at) VALUES (?, ?, ?)",
        (LEASE_KEY, value, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _stored_lease() -> str | None:
    row = db.get_connection().execute("SELECT value FROM kv_store WHERE key = ?", (LEASE_KEY,))
    found = row.fetchone()
    return None if found is None else str(found["value"])


# ── Lease ────────────────────────────────────────────────────


@pytest.mark.usefixtures("board_env")
@pytest.mark.parametrize("corrupt", ["{not json", "[1, 2, 3]", '"just a string"', "null"])
def test_corrupt_lease_is_treated_as_no_lease(
    corrupt: str, caplog: pytest.LogCaptureFixture
) -> None:
    _store_lease(corrupt)

    with caplog.at_level(logging.WARNING, logger="knowledge_engine.runtime.foundation.db"):
        acquired = db.acquire_sweeper_lease("me")

    assert acquired is True
    assert json.loads(_stored_lease() or "")["holder"] == "me"
    assert any("corrupt" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("board_env")
def test_lease_with_non_string_expiry_is_stolen() -> None:
    _store_lease(json.dumps({"holder": "other", "expires_at": 12345}))

    assert db.acquire_sweeper_lease("me") is True
    assert json.loads(_stored_lease() or "")["holder"] == "me"


@pytest.mark.usefixtures("board_env")
def test_live_lease_held_by_another_still_blocks() -> None:
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    _store_lease(json.dumps({"holder": "other", "expires_at": expires}))

    assert db.acquire_sweeper_lease("me") is False
    assert json.loads(_stored_lease() or "")["holder"] == "other"


@pytest.mark.usefixtures("board_env")
def test_release_ignores_corrupt_lease() -> None:
    _store_lease("{not json")

    db.release_sweeper_lease("me")  # must not raise

    assert _stored_lease() == "{not json"


@pytest.mark.usefixtures("board_env")
def test_release_drops_own_lease() -> None:
    assert db.acquire_sweeper_lease("me") is True
    db.release_sweeper_lease("someone-else")
    assert _stored_lease() is not None
    db.release_sweeper_lease("me")
    assert _stored_lease() is None
