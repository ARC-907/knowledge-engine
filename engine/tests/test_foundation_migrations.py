"""Pipeline DB schema: numbered migrations, in-place upgrades, connection lifecycle.

The upgrade fixture ``fixtures/pipeline_db/legacy_inline_schema.sql`` is a
dump of a database created by the pre-versioned inline schema (taken on the
base branch before ``runtime/foundation/db.py`` was split). Opening it with
the current code must yield exactly the schema a fresh database gets.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from knowledge_engine.core.util import sqlite as ke_sqlite
from knowledge_engine.runtime.foundation import db
from knowledge_engine.runtime.foundation.db import migrate

FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_db" / "legacy_inline_schema.sql"

#: Every table the pipeline DB has always carried (plus the FTS5 index).
EXPECTED_TABLES = {
    "tasks",
    "workers",
    "messages",
    "events",
    "batch_jobs",
    "batch_items",
    "api_keys",
    "folder_mounts",
    "prompt_guards",
    "hosted_tools",
    "folder_permissions",
    "chat_conversations",
    "chat_messages",
    "agent_api_keys",
    "agent_key_permissions",
    "chat_personas",
    "chat_prompt_templates",
    "context_artifacts",
    "pipeline_runs",
    "kv_store",
    "board_sweeps",
    "board_config",
    "messages_fts",
}


def _normalize(sql: str | None) -> str:
    text = re.sub(r"\s*([,()])\s*", r"\1", sql or "")
    return re.sub(r"\s+", " ", text).strip()


def _schema(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_version'"
    ).fetchall()
    return {(row[0], row[1]): _normalize(row[2]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int, str | None]]:
    return [(r[1], r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _restore_fixture(target: Path) -> None:
    raw = sqlite3.connect(target)
    try:
        raw.executescript(FIXTURE.read_text(encoding="utf-8"))
    finally:
        raw.close()


def test_schema_files_are_numbered_documented_and_complete() -> None:
    found = migrate.migrations()
    assert [m.version for m in found] == list(range(1, migrate.BASELINE_VERSION + 1))
    for migration in found:
        assert "Consumers:" in migration.sql(), migration.path.name
    assert [m.name for m in found if m.optional] == ["messages_fts"]


def test_fresh_database_gets_every_table_and_every_version(tmp_path: Path) -> None:
    conn = db.get_connection(tmp_path / "fresh.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    assert ke_sqlite.applied_versions(conn) == set(range(1, 9))
    assert conn.execute("SELECT COUNT(*) FROM board_config").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_legacy_dump_upgrades_in_place_to_the_fresh_schema(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    _restore_fixture(legacy_path)
    before = sqlite3.connect(legacy_path)
    legacy_schema = _schema(before)
    before.close()

    upgraded = db.get_connection(legacy_path)
    fresh = db.get_connection(tmp_path / "fresh.db")

    assert ke_sqlite.applied_versions(upgraded) == set(range(1, 9))
    # Names + normalized SQL of every table, index, trigger and FTS shadow table.
    assert _schema(upgraded) == _schema(fresh)
    # Nothing that existed was re-created (the stored SQL is unchanged).
    assert _schema(upgraded) == legacy_schema
    for table in EXPECTED_TABLES - {"messages_fts"}:
        assert _columns(upgraded, table) == _columns(fresh, table), table


def test_legacy_rows_survive_the_upgrade_and_stay_searchable(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    _restore_fixture(legacy_path)
    conn = db.get_connection(legacy_path)
    msg = conn.execute("SELECT * FROM messages WHERE message_id = 'legacy-msg-1'").fetchone()
    assert msg["body"] == "legacy body text"
    hits = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'legacy'"
    ).fetchall()
    assert [h[0] for h in hits] == ["legacy-msg-1"]
    assert conn.execute("SELECT task_id FROM tasks").fetchone()[0] == "legacy-task-1"
    tool = conn.execute("SELECT * FROM hosted_tools").fetchone()
    assert tool["name"] == "legacy-tool" and tool["allow_inline"] == 0
    assert conn.execute("SELECT updated_at FROM board_config").fetchone()[0].startswith("2026-01")


def test_ancient_database_gets_missing_columns_and_tables(tmp_path: Path) -> None:
    """A DB last opened by a release before the ALTER-TABLE columns existed."""
    path = tmp_path / "ancient.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE messages (
            message_id TEXT PRIMARY KEY, channel TEXT NOT NULL DEFAULT 'ops',
            task_id TEXT, product_id TEXT, sender_agent_id TEXT, sender_node_id TEXT,
            sender_role TEXT, message_type TEXT NOT NULL, subject TEXT, body TEXT,
            created_at TEXT NOT NULL, expires_at TEXT, visibility_scope TEXT DEFAULT 'all',
            requires_ack INTEGER DEFAULT 0, ack_by TEXT DEFAULT '[]', reply_to TEXT,
            correlation_id TEXT
        );
        INSERT INTO messages (message_id, message_type, body, created_at)
        VALUES ('old-1', 'chatter', 'from the past', '2025-01-01T00:00:00+00:00');
        CREATE TABLE workers (node_id TEXT PRIMARY KEY, hostname TEXT, status TEXT);
        """
    )
    raw.close()

    conn = db.get_connection(path)
    message_cols = {c[0] for c in _columns(conn, "messages")}
    assert {"model_id", "thread_id"} <= message_cols
    worker_cols = {c[0] for c in _columns(conn, "workers")}
    assert {"safeguard_locked", "safeguard_reason", "resources_json"} <= worker_cols
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    # The pre-FTS message was backfilled into the search index.
    hits = conn.execute("SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'past'")
    assert [h[0] for h in hits] == ["old-1"]


def test_reopening_runs_no_migration(tmp_path: Path) -> None:
    path = tmp_path / "again.db"
    db.get_connection(path)
    db.close_all()
    conn = ke_sqlite.connect(path)
    try:
        assert (
            ke_sqlite.apply_migrations(conn, migrate.migrations(), legacy=migrate.LEGACY_BASELINE)
            == 0
        )
    finally:
        conn.close()


def test_wal_allows_concurrent_writers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.runtime.pipeline import message_board as mb

    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "busy.db"))
    db.get_connection()  # create + migrate once, up front
    writers, per_writer = 6, 25
    barrier = threading.Barrier(writers, timeout=30)
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            barrier.wait()
            for i in range(per_writer):
                mb.post_message(message_type="chatter", sender_node_id=f"w{n}", body=f"m{i}")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert mb.message_count() == writers * per_writer
    # The writers' connections are closed by close_all from this thread.
    assert db.open_connection_count() >= 1
    db.close_all()
    assert db.open_connection_count() == 0


def test_close_connection_and_close_all(tmp_path: Path) -> None:
    a = db.get_connection(tmp_path / "a.db")
    b = db.get_connection(tmp_path / "b.db")
    assert db.close_connection(tmp_path / "a.db") == 1
    with pytest.raises(sqlite3.ProgrammingError):
        a.execute("SELECT 1")
    b.execute("SELECT 1")
    assert db.get_connection(tmp_path / "a.db") is not a
    assert db.close_all() == 2
    with pytest.raises(sqlite3.ProgrammingError):
        b.execute("SELECT 1")


def test_public_names_are_still_exported() -> None:
    for name in (
        "BOARD_CONFIG_DEFAULTS",
        "BOARD_CONFIG_DEFAULT_CHANNELS",
        "PIPELINE_DB_FILENAME",
        "acquire_sweeper_lease",
        "active_scope_db_path",
        "close_all",
        "current_db_path",
        "default_board_config",
        "dict_from_row",
        "get_connection",
        "log_event",
        "master_key_lock",
        "release_sweeper_lease",
        "resolve_db_path",
        "rows_to_dicts",
        "seed_board_config",
        "using_db",
    ):
        assert hasattr(db, name), name


def test_require_row() -> None:
    row = {"a": 1}
    assert db.require_row(row, "task", "t1") is row
    with pytest.raises(db.RowNotFoundError, match="task 't1' not found"):
        db.require_row(None, "task", "t1")
