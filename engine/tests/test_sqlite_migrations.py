"""The shared numbered-SQL migration runner and connection cache (core.util.sqlite)."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from knowledge_engine.core.util import sqlite as ke_sqlite


def _write(directory: Path, name: str, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


@pytest.fixture
def conn(tmp_path: Path):
    connection = ke_sqlite.connect(tmp_path / "m.db")
    yield connection
    connection.close()


# ── discovery ────────────────────────────────────────────────────────


def test_discover_orders_by_version_and_ignores_other_files(tmp_path: Path) -> None:
    _write(tmp_path, "010_later.sql", "SELECT 1;")
    _write(tmp_path, "002_second.sql", "SELECT 1;")
    _write(tmp_path, "001_first.sql", "SELECT 1;")
    _write(tmp_path, "README.sql", "SELECT 1;")
    _write(tmp_path, "notes.txt", "x")
    found = ke_sqlite.discover_migrations(tmp_path)
    assert [(m.version, m.name) for m in found] == [(1, "first"), (2, "second"), (10, "later")]


def test_discover_prefix_filter_and_recursion(tmp_path: Path) -> None:
    _write(tmp_path, "002_b.sql", "SELECT 1;")
    _write(tmp_path / "sub", "001_a.sql", "SELECT 1;")
    assert [m.version for m in ke_sqlite.discover_migrations(tmp_path)] == [2]
    assert [m.version for m in ke_sqlite.discover_migrations(tmp_path, recursive=True)] == [1, 2]
    only = ke_sqlite.discover_migrations(tmp_path, recursive=True, only_prefixes=("001_",))
    assert [m.version for m in only] == [1]


def test_discover_rejects_duplicate_versions_and_missing_dir(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql", "SELECT 1;")
    _write(tmp_path, "001_b.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="duplicate migration version 1"):
        ke_sqlite.discover_migrations(tmp_path)
    with pytest.raises(FileNotFoundError):
        ke_sqlite.discover_migrations(tmp_path / "missing")


# ── statement splitting ──────────────────────────────────────────────


def test_split_statements_keeps_triggers_whole_and_drops_comment_only_chunks() -> None:
    script = """
    -- header; with a semicolon in a comment
    CREATE TABLE t (v TEXT);
    CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN
        INSERT INTO t2 VALUES (new.v);
        INSERT INTO t3 VALUES (new.v);
    END;
    -- trailing comment
    """
    statements = ke_sqlite.split_statements(script)
    assert len(statements) == 2
    assert statements[1].startswith("CREATE TRIGGER") and statements[1].endswith("END;")


def test_split_statements_rejects_an_incomplete_tail() -> None:
    with pytest.raises(ValueError, match="incomplete SQL statement"):
        ke_sqlite.split_statements("CREATE TABLE t (v TEXT);\nCREATE TABLE u (")


# ── applying ─────────────────────────────────────────────────────────


def test_apply_records_versions_and_is_idempotent(tmp_path: Path, conn) -> None:
    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE t (v INTEGER);")
    _write(d, "002_u.sql", "CREATE TABLE u (v INTEGER);")
    assert ke_sqlite.apply_migrations(conn, d) == 2
    assert ke_sqlite.applied_versions(conn) == {1, 2}
    assert ke_sqlite.schema_version(conn) == 2
    assert ke_sqlite.apply_migrations(conn, d) == 0
    _write(d, "003_w.sql", "CREATE TABLE w (v INTEGER);")
    assert ke_sqlite.apply_migrations(conn, d) == 1
    assert {"t", "u", "w", "schema_version"} <= _tables(conn)


def test_failed_migration_rolls_back_completely(tmp_path: Path, conn) -> None:
    d = tmp_path / "mig"
    _write(d, "001_ok.sql", "CREATE TABLE ok (v INTEGER);")
    _write(
        d,
        "002_broken.sql",
        "CREATE TABLE half (v INTEGER);\nINSERT INTO ok VALUES (1);\nINSERT INTO nope VALUES (1);",
    )
    with pytest.raises(ke_sqlite.MigrationError, match="002_broken") as info:
        ke_sqlite.apply_migrations(conn, d)
    assert info.value.migration.version == 2
    # 001 stayed applied; nothing of 002 survived (no table, no row, no version).
    assert ke_sqlite.applied_versions(conn) == {1}
    assert "half" not in _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM ok").fetchone()[0] == 0
    assert not conn.in_transaction


def test_pending_caller_writes_are_committed_not_lost(tmp_path: Path, conn) -> None:
    conn.execute("CREATE TABLE pre (v INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO pre VALUES (7)")
    assert conn.in_transaction
    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE t (v INTEGER);")
    ke_sqlite.apply_migrations(conn, d)
    assert conn.execute("SELECT v FROM pre").fetchone()[0] == 7


def test_optional_migration_failure_is_skipped_and_retried(
    tmp_path: Path, conn, caplog: pytest.LogCaptureFixture
) -> None:
    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE t (v INTEGER);")
    _write(d, "002_opt.sql", "-- ke:optional\nCREATE VIRTUAL TABLE v USING no_such_module(x);")
    _write(d, "003_u.sql", "CREATE TABLE u (v INTEGER);")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.util.sqlite"):
        assert ke_sqlite.apply_migrations(conn, d) == 2
    assert ke_sqlite.applied_versions(conn) == {1, 3}
    assert any("optional migration 002_opt skipped" in r.getMessage() for r in caplog.records)
    # Later migrations ran; the optional one is attempted again next time.
    _write(d, "002_opt.sql", "-- ke:optional\nCREATE TABLE v (x INTEGER);")
    assert ke_sqlite.apply_migrations(conn, d) == 1
    assert ke_sqlite.applied_versions(conn) == {1, 2, 3}


def test_concurrent_migrators_apply_each_migration_once(tmp_path: Path) -> None:
    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE t (v INTEGER);")
    # A non-idempotent migration: applying it twice would fail.
    _write(d, "002_add.sql", "ALTER TABLE t ADD COLUMN extra TEXT;")
    path = tmp_path / "shared.db"
    counts: list[int] = []
    errors: list[Exception] = []
    ke_sqlite.connect(path).close()  # create the file + switch it to WAL up front
    barrier = threading.Barrier(6, timeout=30)

    def worker() -> None:
        try:
            c = ke_sqlite.connect(path)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
            barrier.abort()
            return
        try:
            barrier.wait()
            counts.append(ke_sqlite.apply_migrations(c, d))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(counts) == 2
    check = ke_sqlite.connect(path)
    assert ke_sqlite.applied_versions(check) == {1, 2}
    check.close()


# ── pre-versioned databases ──────────────────────────────────────────


def test_legacy_baseline_adopts_without_recreating(tmp_path: Path, conn) -> None:
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    assert ke_sqlite.is_pre_versioned(conn)

    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE IF NOT EXISTS t (v INTEGER, extra TEXT);")
    _write(d, "002_u.sql", "CREATE TABLE IF NOT EXISTS u (v INTEGER);")
    _write(d, "003_new.sql", "CREATE TABLE w (v INTEGER);")
    prepared: list[str] = []

    def prepare(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE t ADD COLUMN extra TEXT")
        prepared.append("t.extra")

    baseline = ke_sqlite.LegacyBaseline(version=2, prepare=prepare)
    assert ke_sqlite.apply_migrations(conn, d, legacy=baseline) == 1  # only 003 "ran"
    assert prepared == ["t.extra"]
    assert ke_sqlite.applied_versions(conn) == {1, 2, 3}
    assert conn.execute("SELECT v FROM t").fetchone()[0] == 42  # data kept
    assert {"t", "u", "w"} <= _tables(conn)  # missing baseline table created
    assert not ke_sqlite.is_pre_versioned(conn)
    # A second run neither adopts again nor re-runs anything.
    assert ke_sqlite.apply_migrations(conn, d, legacy=baseline) == 0
    assert prepared == ["t.extra"]


def test_empty_database_is_not_pre_versioned(conn) -> None:
    assert not ke_sqlite.is_pre_versioned(conn)


def test_failed_adoption_rolls_back(tmp_path: Path, conn) -> None:
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    d = tmp_path / "mig"
    _write(d, "001_t.sql", "CREATE TABLE IF NOT EXISTS t (v INTEGER);")

    def prepare(_c: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("boom")

    with pytest.raises(sqlite3.OperationalError):
        ke_sqlite.apply_migrations(conn, d, legacy=ke_sqlite.LegacyBaseline(1, prepare))
    assert ke_sqlite.is_pre_versioned(conn)  # no schema_version table left behind


# ── connection cache ─────────────────────────────────────────────────


def test_cache_returns_one_connection_per_thread_and_path(tmp_path: Path) -> None:
    cache = ke_sqlite.ConnectionCache()
    a = cache.get(tmp_path / "a.db")
    assert cache.get(tmp_path / "a.db") is a
    assert cache.get(tmp_path / "b.db") is not a
    other: list[sqlite3.Connection] = []
    t = threading.Thread(target=lambda: other.append(cache.get(tmp_path / "a.db")))
    t.start()
    t.join()
    assert other[0] is not a
    assert cache.open_count() == 3
    assert cache.close_all() == 3
    with pytest.raises(sqlite3.ProgrammingError):
        other[0].execute("SELECT 1")
    assert cache.get(tmp_path / "a.db") is not a
    cache.close_all()


def test_cache_is_bounded_per_thread_lru(tmp_path: Path) -> None:
    cache = ke_sqlite.ConnectionCache(max_per_thread=2)
    first = cache.get(tmp_path / "1.db")
    second = cache.get(tmp_path / "2.db")
    assert cache.get(tmp_path / "1.db") is first  # 1 is now most recently used
    cache.get(tmp_path / "3.db")  # evicts 2
    assert cache.open_count() == 2
    with pytest.raises(sqlite3.ProgrammingError):
        second.execute("SELECT 1")
    first.execute("SELECT 1")
    assert cache.paths() == {str(tmp_path / "1.db"), str(tmp_path / "3.db")}
    cache.close_all()


def test_cache_closes_connections_of_finished_threads(tmp_path: Path) -> None:
    cache = ke_sqlite.ConnectionCache()
    held: list[sqlite3.Connection] = []
    t = threading.Thread(target=lambda: held.append(cache.get(tmp_path / "t.db")))
    t.start()
    t.join()
    assert cache.open_count() == 1
    cache.get(tmp_path / "main.db")  # any new open prunes dead threads
    assert cache.open_count() == 1
    with pytest.raises(sqlite3.ProgrammingError):
        held[0].execute("SELECT 1")
    cache.close_all()


def test_cache_discard_and_failing_init(tmp_path: Path) -> None:
    calls: list[str] = []

    def init(c: sqlite3.Connection) -> None:
        calls.append("init")
        if len(calls) == 2:
            raise sqlite3.OperationalError("init failed")

    cache = ke_sqlite.ConnectionCache(init=init)
    conn = cache.get(tmp_path / "d.db")
    discarded = cache.discard(tmp_path / "d.db")
    assert discarded == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.OperationalError):
        cache.get(tmp_path / "d.db")
    assert cache.open_count() == 0
    assert cache.get(tmp_path / "d.db") is not conn
    cache.close_all()


def test_cache_rejects_a_zero_bound() -> None:
    with pytest.raises(ValueError):
        ke_sqlite.ConnectionCache(max_per_thread=0)
