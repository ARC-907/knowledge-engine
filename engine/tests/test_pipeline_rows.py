"""Typed pipeline rows (runtime.pipeline.rows) match what the stores return.

The ``TypedDict`` shapes are only checked by mypy; these tests pin them to
the live schema and to real rows, so a column added to (or dropped from) a
table without updating its row type fails here instead of drifting silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.agent_board import store
from knowledge_engine.runtime.foundation import db
from knowledge_engine.runtime.pipeline import message_board, queue, rows, worker_registry


@pytest.fixture
def pipeline_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path / "config"))  # no YAML: defaults
    return data


def _declared(shape: Any) -> set[str]:
    keys = set(shape.__annotations__)
    # Every column is always present in a ``SELECT *`` row: no optional keys.
    assert set(shape.__required_keys__) == keys
    assert not shape.__optional_keys__
    return keys


def _columns(table: str) -> set[str]:
    info = db.get_connection().execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in info}


@pytest.mark.parametrize(
    ("table", "shape"),
    [
        ("tasks", rows.TaskRow),
        ("workers", rows.WorkerRow),
        ("messages", rows.MessageRow),
    ],
)
def test_row_types_match_table_columns(pipeline_db: Path, table: str, shape: Any) -> None:
    declared = _declared(shape)
    columns = _columns(table)
    assert declared == columns


def test_task_rows_have_exactly_the_declared_keys(pipeline_db: Path) -> None:
    expected = _declared(rows.TaskRow)
    created = queue.create_task("audit", "lib", chapter_key="ch1")
    assert set(created) == expected
    tid = created["task_id"]
    claimed = queue.transition_task(tid, "claimed", worker="w1")
    assert set(claimed) == expected
    listed = queue.list_tasks()
    assert [set(t) for t in listed] == [expected]
    read = queue.read_task(tid)
    assert read is not None
    assert set(read) == expected
    entry = read["audit_trail"][-1]
    audit_keys = _declared(rows.AuditEntry)
    assert set(entry) == audit_keys


def test_worker_rows_have_exactly_the_declared_keys(pipeline_db: Path) -> None:
    expected = _declared(rows.WorkerRow)
    registered = worker_registry.register_worker("node-a", capabilities=["audit"])
    assert set(registered) == expected
    beat = worker_registry.send_heartbeat("node-a", current_tasks=["t1"], resources={"cpu": 1})
    assert beat is not None
    assert set(beat) == expected
    # Not a JSON_FIELDS column: the resource snapshot stays raw JSON text.
    assert beat["resources_json"] == '{"cpu": 1}'
    listed = worker_registry.list_workers()
    assert [set(w) for w in listed] == [expected]

    safeguard_keys = _declared(rows.SafeguardStatus)
    status = worker_registry.get_safeguard_status("node-a")
    assert status is not None
    assert set(status) == safeguard_keys
    statuses = worker_registry.list_safeguard_statuses()
    assert [set(s) for s in statuses] == [safeguard_keys]


def test_message_rows_have_exactly_the_declared_keys(pipeline_db: Path) -> None:
    expected = _declared(rows.MessageRow)
    posted = message_board.post_blocker("node-a", "task-1234567890", "stuck")
    assert set(posted) == expected
    read = message_board.read_message(posted["message_id"])
    assert read is not None
    assert set(read) == expected
    polled = message_board.poll_messages()
    assert [set(m) for m in polled] == [expected]
    relevant = message_board.get_relevant_messages("node-a")
    assert [set(m) for m in relevant] == [expected]

    # The agent-board facades return the same shape.
    acked = store.ack_message(posted["message_id"], "node-b")
    assert acked is not None
    assert set(acked) == expected
    assert acked["ack_by"] == ["node-b"]
    thread = store.thread_messages(correlation_id=posted["message_id"])
    assert [set(m) for m in thread] == [expected]


def test_payload_helpers_return_the_row_itself(pipeline_db: Path) -> None:
    posted = message_board.post_message("status", "node-a", subject="hi")
    payload = rows.as_payload(posted)
    assert payload is posted
    polled = message_board.poll_messages()
    payloads = rows.as_payloads(polled)
    assert payloads is polled
