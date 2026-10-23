"""Unit tests for the shared helpers in ``knowledge_engine.core.util``."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_engine.core.util import (
    env,
    hashing,
    proc,
    slug,
    sqlite as ke_sqlite,
    time as ke_time,
)

# ── time ─────────────────────────────────────────────────────────────


def test_utc_now_iso_is_aware_utc_isoformat() -> None:
    before = datetime.now(UTC)
    text = ke_time.utc_now_iso()
    after = datetime.now(UTC)
    parsed = datetime.fromisoformat(text)
    assert text.endswith("+00:00")
    assert parsed.tzinfo is not None
    assert before <= parsed <= after


def test_utc_now_iso_offset() -> None:
    shifted = datetime.fromisoformat(ke_time.utc_now_iso(timedelta(hours=-2)))
    delta = datetime.now(UTC) - shifted
    assert timedelta(hours=1, minutes=59) < delta < timedelta(hours=2, minutes=1)


def test_utc_now_is_aware() -> None:
    assert ke_time.utc_now().utcoffset() == timedelta(0)


# ── hashing ──────────────────────────────────────────────────────────


def test_sha256_hex_text_bytes_and_file(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"h\xc3\xa9llo").hexdigest()
    assert hashing.sha256_hex("héllo") == expected
    assert hashing.sha256_hex(b"h\xc3\xa9llo") == expected
    assert hashing.sha256_hex(bytearray(b"h\xc3\xa9llo")) == expected
    blob = tmp_path / "f.bin"
    payload = b"x" * 200_000  # larger than one read block
    blob.write_bytes(payload)
    assert hashing.sha256_hex(blob) == hashlib.sha256(payload).hexdigest()


def test_sha256_hex_str_is_text_not_path(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("contents", encoding="utf-8")
    assert hashing.sha256_hex(str(target)) == hashlib.sha256(str(target).encode()).hexdigest()


def test_sha256_hex_length_and_digest() -> None:
    assert hashing.sha256_hex("a", length=16) == hashlib.sha256(b"a").hexdigest()[:16]
    assert hashing.sha256_digest("a") == hashlib.sha256(b"a").digest()
    with pytest.raises(ValueError, match="positive"):
        hashing.sha256_hex("a", length=0)
    with pytest.raises(TypeError, match="cannot hash int"):
        hashing.sha256_hex(5)  # type: ignore[arg-type]


def test_sha256_hex_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        hashing.sha256_hex(tmp_path / "missing")


def test_project_context_hashing_reexports() -> None:
    from knowledge_engine.project_context import hashing as pc_hashing

    assert pc_hashing.content_hash is hashing.content_hash
    assert pc_hashing.short_hash("abc", 4) == hashlib.sha256(b"abc").hexdigest()[:4]


# ── slug ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("My Library", "my-library"),
        ("Docs & Notes", "docs-notes"),
        ("a__b--c", "a-b-c"),
        ("  --x--  ", "x"),
        ("v1.2", "v12"),
        ("Café", "café"),
        ("!!!", ""),
    ],
)
def test_slugify_default_rule(name: str, expected: str) -> None:
    assert slug.slugify(name) == expected


def test_slugify_options() -> None:
    assert slug.slugify("v1.2", separators=" -_&.") == "v1-2"
    assert slug.slugify("", fallback="project") == "project"
    assert slug.slugify("A/B", replace_unknown=True) == "a-b"
    assert slug.slugify("Café", ascii_only=True, replace_unknown=True) == "caf"
    assert slug.slugify(".hidden.", keep=".", strip="-.") == "hidden"
    assert slug.slugify("abcdef-", max_length=4) == "abcd"
    assert slug.slugify("abc-def", max_length=4) == "abc"


def test_watcher_and_project_paths_share_slugify() -> None:
    from knowledge_engine.core import watcher
    from knowledge_engine.project_context import paths

    assert watcher.slugify is slug.slugify
    assert paths.slugify("Weird/Name!!") == "weirdname"
    assert paths.slugify("my.project") == "my-project"
    assert paths.slugify("") == "project"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Branch/Feat-Auth", "branch-feat-auth"),
        ("../../etc/passwd", "etc-passwd"),
        ("proj a  b", "proj-a-b"),
        ("v1.2_x", "v1.2_x"),
        ("Ünïcode", "n-code"),
        ("a" * 90, "a" * 80),
    ],
)
def test_slugify_scope_keeps_its_rules(raw: str, expected: str) -> None:
    from knowledge_engine.agent_board import scopes

    assert scopes.slugify_scope(raw) == expected


# ── env ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["1", "true", " YES ", "On"])
def test_parse_bool_true(raw: str) -> None:
    assert env.parse_bool(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", "", "  "])
def test_parse_bool_false(raw: str) -> None:
    assert env.parse_bool(raw) is False


def test_parse_bool_passthrough() -> None:
    assert env.parse_bool("maybe") == "maybe"
    assert env.parse_bool(None) is None
    assert env.parse_bool(True) is True
    assert env.parse_bool_or("maybe", True) is True
    assert env.parse_bool_or(None, False) is False
    assert env.parse_bool_or("off", True) is False


def test_env_bool(caplog: pytest.LogCaptureFixture) -> None:
    assert env.env_bool("X", env={"X": "yes"}) is True
    assert env.env_bool("X", True, env={}) is True
    with caplog.at_level("WARNING", logger="knowledge_engine.core.util.env"):
        assert env.env_bool("X", True, env={"X": "perhaps"}) is True
    assert "ignoring invalid X='perhaps'" in caplog.text


def test_env_list() -> None:
    assert env.parse_list(" a, ,b ,") == ["a", "b"]
    assert env.parse_list(None) == []
    assert env.parse_list("A;B", sep=";", lower=True) == ["a", "b"]
    assert env.env_list("L", env={"L": "x,y"}) == ["x", "y"]
    assert env.env_list("L", env={}) == []


def test_settings_use_the_shared_parsers() -> None:
    from knowledge_engine.core import settings

    assert settings.parse_env_bool is env.parse_bool
    assert settings.env_flag is env.parse_bool_or
    assert settings.Settings(provider_order=" Ollama, ,echo").provider_order_list() == [
        "ollama",
        "echo",
    ]


# ── proc ─────────────────────────────────────────────────────────────


def test_run_git_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    proc.run_git(["log", "-1"], tmp_path)
    assert seen["argv"] == ["git", "log", "-1"]
    assert seen["shell"] is False
    assert seen["timeout"] == proc.GIT_TIMEOUT_SECONDS
    assert seen["cwd"] == str(tmp_path)


def test_git_context_reexports_run_git() -> None:
    from knowledge_engine.project_context import git_context

    assert git_context.run_git is proc.run_git
    assert git_context.GIT_TIMEOUT_SECONDS == proc.GIT_TIMEOUT_SECONDS


# ── sqlite ───────────────────────────────────────────────────────────


def test_connect_applies_standard_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "x.db"
    conn = ke_sqlite.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_connect_options(tmp_path: Path) -> None:
    conn = ke_sqlite.connect(
        tmp_path / "y.db", wal=False, busy_timeout_ms=10, foreign_keys=False, row_factory=None
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert isinstance(conn.execute("SELECT 1").fetchone(), tuple)
    finally:
        conn.close()
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        ke_sqlite.connect(tmp_path / "z.db", busy_timeout_ms=-1)


def test_connect_memory_and_check_same_thread() -> None:
    conn = ke_sqlite.connect(":memory:")
    errors: list[BaseException] = []

    def use() -> None:
        try:
            conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=use)
    thread.start()
    thread.join()
    conn.close()
    assert errors == []


def test_project_context_and_foundation_use_shared_connect() -> None:
    from knowledge_engine.project_context import db as pc_db

    assert pc_db.connect is ke_sqlite.connect


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    conn = ke_sqlite.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    with ke_sqlite.transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError), ke_sqlite.transaction(conn, immediate=True):
        conn.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("boom")
    assert [row[0] for row in conn.execute("SELECT v FROM t")] == [1]
    conn.close()


def test_thread_local_connections(tmp_path: Path) -> None:
    inits: list[int] = []

    def init(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
        inits.append(1)

    cache = ke_sqlite.thread_local_connections(tmp_path / "tl.db", init=init)
    main = cache.get()
    assert cache.get() is main
    others: list[sqlite3.Connection] = []
    thread = threading.Thread(target=lambda: others.append(cache.get()))
    thread.start()
    thread.join()
    assert others[0] is not main
    assert len(inits) == 2
    assert cache.close_all() == 2
    with pytest.raises(sqlite3.ProgrammingError):
        main.execute("SELECT 1")
    fresh = cache.get()
    assert fresh is not main
    fresh.execute("SELECT 1")
    assert cache.close_all() == 1


def test_thread_local_init_failure_closes(tmp_path: Path) -> None:
    def bad(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("nope")

    cache = ke_sqlite.ThreadLocalConnections(tmp_path / "bad.db", init=bad)
    with pytest.raises(sqlite3.OperationalError):
        cache.get()
    assert cache.close_all() == 0
