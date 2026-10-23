"""Agent-board substrate: digest analytics, per-field validators, scope creation,
key-use bookkeeping and connection hygiene of the board test modules."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from knowledge_engine.agent_board import digest, keys, schemas, scopes, store
from knowledge_engine.runtime.foundation import db

ENGINE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def board_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    return data


# ── digest analytics ─────────────────────────────────────────────────


def _msg(**fields: object) -> dict[str, object]:
    return {"message_type": "chatter", "sender_node_id": "a", **fields}


def test_summarize_counts_blockers_senders_and_threads() -> None:
    msgs = [
        _msg(message_id="5", message_type="blocker", requires_ack=1, ack_by=[], subject="s5"),
        _msg(message_id="4", message_type="blocker", requires_ack=1, ack_by=["x"]),
        _msg(message_id="3", correlation_id="c1", sender_node_id="b"),
        _msg(message_id="2", correlation_id="c1", sender_node_id="b"),
        _msg(message_id="1", correlation_id="c1", message_type=None),
    ]
    out = digest.summarize(msgs, channel="ops", since="t0")
    assert out["channel"] == "ops" and out["since"] == "t0" and out["scanned"] == 5
    assert out["counts_by_type"] == {"blocker": 2, "chatter": 2, "unknown": 1}
    assert out["most_recent_per_type"]["blocker"]["message_id"] == "5"
    assert [b["message_id"] for b in out["open_blockers"]] == ["5"]
    assert out["top_senders"] == [{"from": "a", "count": 3}, {"from": "b", "count": 2}]
    assert out["busy_threads"] == [{"correlation_id": "c1", "count": 3}]


def test_empty_digest_has_the_summary_shape() -> None:
    assert digest.empty_digest("ops", "t") == digest.summarize([], channel="ops", since="t")
    assert digest.empty_digest()["channel"] == "(all)"


def test_sweeper_digests_are_filtered() -> None:
    own = _msg(sender_node_id=digest.SWEEPER_NODE_ID, message_type="digest")
    other = _msg(sender_node_id=digest.SWEEPER_NODE_ID, message_type="reminder")
    assert digest.without_sweeper_digests([own, other]) == [other]
    assert digest.has_ack({"ack_by": '["x"]'}) and not digest.has_ack({"ack_by": "[]"})


def test_store_digest_uses_the_analytics(board_db: Path) -> None:
    store.post_with_validation(
        {"message_type": "status_update", "sender_node_id": "n", "body": "b"}
    )
    out = store.digest(channel="ops")
    assert out["scanned"] == 1 and out["counts_by_type"] == {"status_update": 1}
    assert store.digest(scope="never-created")["scanned"] == 0


# ── per-field validators ─────────────────────────────────────────────


def test_validate_reports_errors_in_field_order() -> None:
    draft, errors = schemas.validate(
        {
            "channel": "nope",
            "body": "",
            "subject": "s" * (schemas.MAX_LEN_SUBJECT + 1),
            "visibility_scope": "everyone",
            "task_id": "t" * (schemas.MAX_LEN_TASK_ID + 1),
            "ttl_hours": "soon",
        }
    )
    assert draft is None
    assert [e.split(" ")[0] for e in errors] == [
        "channel",
        "message_type",
        "sender_node_id",
        "body",
        "subject",
        "visibility_scope",
        "task_id",
        "ttl_hours",
    ]


def test_validate_normalizes_a_good_payload() -> None:
    draft, errors = schemas.validate(
        {
            "message_type": " claim ",
            "sender_node_id": "n1",
            "body": " hi ",
            "thread_id": "  ",
            "ttl_hours": "0",
            "requires_ack": 1,
        }
    )
    assert errors == []
    assert draft is not None
    assert (draft.channel, draft.message_type, draft.body) == ("ops", "claim", "hi")
    assert draft.thread_id is None and draft.ttl_hours == 0 and draft.requires_ack is True


@pytest.mark.parametrize(
    ("raw", "message"),
    [(-1, "ttl_hours must be >= 0"), (schemas.MAX_TTL_HOURS + 1, "ttl_hours too large")],
)
def test_validate_ttl_bounds(raw: int, message: str) -> None:
    errors: list[str] = []
    schemas.validate_ttl({"ttl_hours": raw}, errors)
    assert errors and errors[0].startswith(message)


def test_channel_membership_can_be_skipped() -> None:
    errors: list[str] = []
    assert schemas.validate_channel({"channel": "x"}, errors, ("ops",), check_membership=False)
    assert errors == []


# ── scope creation ───────────────────────────────────────────────────


def test_concurrent_scope_creation_respects_the_cap(
    board_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(scopes.MAX_SCOPES_ENV, "3")
    barrier = threading.Barrier(8, timeout=30)
    created: list[str] = []
    refused: list[str] = []

    def post(n: int) -> None:
        barrier.wait()
        try:
            store.post_with_validation(
                {"message_type": "chatter", "sender_node_id": "n", "body": "b"}, scope=f"s{n}"
            )
            created.append(f"s{n}")
        except scopes.ScopeLimitError:
            refused.append(f"s{n}")

    threads = [threading.Thread(target=post, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 3 and len(refused) == 5
    assert len(scopes.list_scopes()) == 3


def test_create_scope_reports_creation_once(board_db: Path) -> None:
    first = scopes.create_scope("feat-x")
    again = scopes.create_scope("feat-x")
    assert first["created"] is True and again["created"] is False
    assert first["db_path"] == again["db_path"]
    assert Path(str(first["db_path"])).is_file()


def test_nonconforming_scope_names_are_reported(
    board_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = scopes.scopes_root()
    root.mkdir(parents=True)
    (root / "v1.2.db").write_bytes(b"")
    (root / "Feature_A.db").write_bytes(b"")
    (root / "ok-name.db").write_bytes(b"")
    legacy = {e["scope"]: e["suggested"] for e in scopes.nonconforming_scopes()}
    assert legacy == {"v1.2": "v1-2", "Feature_A": "feature_a"}
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.agent_board.scopes"):
        assert scopes.warn_nonconforming_scopes() == 2
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "v1-2.db" in text and "feature_a.db" in text


# ── key use bookkeeping ──────────────────────────────────────────────


def test_verify_key_refreshes_last_used_at_at_most_once_per_window(board_db: Path) -> None:
    created = keys.create_key("k")
    conn = db.get_connection()

    def last_used() -> str | None:
        row = conn.execute(
            "SELECT last_used_at FROM agent_api_keys WHERE key_id = ?", (created["key_id"],)
        ).fetchone()
        return row[0]

    assert last_used() is None
    assert keys.verify_key(created["raw_key"]) is not None
    first = last_used()
    assert first is not None
    assert keys.verify_key(created["raw_key"]) is not None
    assert last_used() == first  # within the window: no write
    conn.execute(
        "UPDATE agent_api_keys SET last_used_at = '2000-01-01T00:00:00+00:00' WHERE key_id = ?",
        (created["key_id"],),
    )
    conn.commit()
    keys.verify_key(created["raw_key"])
    assert last_used() != "2000-01-01T00:00:00+00:00"
    assert keys.verify_key("keb_wrong") is None


# ── connection hygiene ───────────────────────────────────────────────


def test_board_test_modules_leave_no_unclosed_connections() -> None:
    """The board suites run clean with ResourceWarning promoted to an error.

    Runs in a subprocess so the warning filter (and pytest's unraisable-
    exception hook, which reports connections finalized by the GC) applies
    to exactly these modules.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("KE_")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-W",
            "error::ResourceWarning",
            "-W",
            "error::pytest.PytestUnraisableExceptionWarning",
            "tests/test_agent_board.py",
            "tests/test_board_http.py",
            "tests/test_board_sweeper.py",
            "tests/test_sweeper_lease.py",
        ],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "unclosed" not in proc.stdout + proc.stderr
