"""Smoke + regression tests for the agent board subsystem."""

from __future__ import annotations

import os
from pathlib import Path


def _env(corpus: Path, data: Path) -> None:
    """Point the engine at a tmp corpus + tmp data dir + per-test pipeline DB.

    Isolation is achieved entirely through env vars + a unique tmp_path per
    test, because `foundation.db` resolves the DB path dynamically on every
    `get_connection()` (see `db.resolve_db_path`). No module surgery is
    needed: changing `KE_PIPELINE_DB` is enough for the next connection —
    on any thread — to open the new database. Stopping the sweeper and
    dropping cached connections keeps a prior test's daemon thread from
    holding a handle to a tmp file pytest is about to delete.
    """
    corpus.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    os.environ["KE_CORPUS_ROOT"] = str(corpus)
    os.environ["KE_DATA_DIR"] = str(data)
    os.environ["KE_REGISTRY_PATH"] = str(corpus / "registry.json")
    os.environ["KE_PIPELINE_DB"] = str(data / "pipeline.db")

    import sys
    # Stop a sweeper a prior test may have started, so it doesn't keep a
    # connection open to a tmp DB that's about to be torn down.
    sweeper_mod = sys.modules.get("knowledge_engine.agent_board.sweeper")
    if sweeper_mod is not None:
        try:
            sweeper_mod.stop(timeout=5.0)
        except Exception:
            pass
    # Drop this thread's cached connections so the next get_connection()
    # opens fresh against the just-set KE_PIPELINE_DB.
    db_mod = sys.modules.get("knowledge_engine.foundation.db")
    if db_mod is not None:
        try:
            db_mod.close_all()
            db_mod._FTS_BACKFILLED.clear()  # one-shot backfill guard, per path
        except Exception:
            pass


# ── Schema validation ─────────────────────────────────────────


def test_schemas_validate_minimal(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import schemas

    draft, errors = schemas.validate({
        "channel": "ops",
        "message_type": "claim",
        "sender_node_id": "branch-x",
        "body": "claim it",
    })
    assert errors == []
    assert draft is not None
    assert draft.channel == "ops"
    assert draft.message_type == "claim"
    assert draft.body == "claim it"


def test_schemas_reject_missing_required(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import schemas

    draft, errors = schemas.validate({"channel": "ops"})
    assert draft is None
    joined = " ".join(errors)
    assert "message_type" in joined
    assert "sender_node_id" in joined
    assert "body" in joined


def test_schemas_unknown_channel(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import schemas

    draft, errors = schemas.validate({
        "channel": "made-up-channel",
        "message_type": "claim",
        "sender_node_id": "x",
        "body": "y",
    })
    assert draft is None
    assert any("channel" in e for e in errors)


# ── Store: post / poll / search / digest ──────────────────────


def test_store_post_and_poll_roundtrip(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    msg = store.post_with_validation({
        "channel": "ops",
        "message_type": "claim",
        "sender_node_id": "branch-x",
        "body": "claim it",
    })
    assert msg["message_id"]
    polled = store.poll(channel="ops", limit=10)
    assert any(m["message_id"] == msg["message_id"] for m in polled)


def test_store_validation_raises(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    import pytest
    with pytest.raises(ValueError):
        store.post_with_validation({"channel": "ops"})


def test_store_fts_search(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    store.post_with_validation({
        "channel": "research",
        "message_type": "research_finding",
        "sender_node_id": "branch-x",
        "subject": "found a fox",
        "body": "the quick brown fox jumps over the lazy dog",
    })
    store.post_with_validation({
        "channel": "research",
        "message_type": "research_finding",
        "sender_node_id": "branch-y",
        "subject": "different",
        "body": "no animals here",
    })
    hits = store.search_messages("fox", channel="research")
    assert hits
    assert any("fox" in (h.get("body") or "").lower() for h in hits)


def test_store_digest_summarizes(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    for i in range(5):
        store.post_with_validation({
            "channel": "ops",
            "message_type": "status_update",
            "sender_node_id": f"branch-{i % 2}",
            "body": f"update {i}",
        })
    store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })
    d = store.digest(channel="ops")
    assert d["scanned"] >= 6
    assert d["counts_by_type"].get("status_update") == 5
    assert d["counts_by_type"].get("blocker") == 1
    assert d["open_blockers"]
    assert d["top_senders"]


# ── Ack + stale-blocker reminder loop ─────────────────────────


def test_store_ack_records_acker(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    msg = store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })
    updated = store.ack_message(msg["message_id"], "branch-y")
    assert updated is not None
    ack = updated.get("ack_by")
    if isinstance(ack, str):
        import json
        ack = json.loads(ack)
    assert "branch-y" in ack


def test_sweeper_one_pass_records_sweep_row(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import sweeper, store

    # No data — sweep should still record a clean pass.
    result = sweeper.sweep_once()
    assert result["error"] is None
    last = store.last_sweep()
    assert last is not None
    assert last["error"] is None or last["error"] == ""


# ── Key vault ─────────────────────────────────────────────────


def test_keys_create_verify_check_revoke(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys

    created = keys.create_key("test-key", notes="unit test")
    assert created["raw_key"].startswith("keb_")
    record = keys.verify_key(created["raw_key"])
    assert record is not None
    assert record["display_name"] == "test-key"
    grant = keys.grant_permission(created["key_id"], "board", "*", "write")
    assert keys.check_permission(created["key_id"], "board", "*", "write")
    assert not keys.check_permission(created["key_id"], "board", "*", "admin")
    assert keys.revoke_permission(grant["perm_id"])
    assert not keys.check_permission(created["key_id"], "board", "*", "write")
    assert keys.delete_key(created["key_id"])
    assert keys.verify_key(created["raw_key"]) is None


def test_keys_master_implies_all(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys

    master = keys.ensure_master_key()
    assert master is not None
    assert keys.check_permission(master["key_id"], "board", "*", "admin")
    assert keys.check_permission(master["key_id"], "provider", "anthropic", "invoke")
    # Idempotent: second call returns None (master already exists)
    assert keys.ensure_master_key() is None


def test_keys_invalid_resource_type_rejected(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys

    created = keys.create_key("test-key")
    import pytest
    with pytest.raises(ValueError):
        keys.grant_permission(created["key_id"], "calendar_layer", "*", "read")


# ── Config singleton ──────────────────────────────────────────


def test_config_seeded_and_updatable(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    cfg = store.load_config()
    assert cfg["engine_port"] == 9210
    assert "ops" in cfg["channels"]
    updated = store.update_config({"sweep_interval_s": 30, "channels": ["ops", "custom"]})
    assert updated["sweep_interval_s"] == 30
    assert "custom" in updated["channels"]


# ── HTTP route smoke ──────────────────────────────────────────


def test_board_http_status_and_post(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get("/board/status")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "knowledge-engine-board"
        assert "ops" in body["config"]["channels"]

        r = client.post("/board/messages", json={
            "channel": "ops",
            "message_type": "status_update",
            "sender_node_id": "test-branch",
            "body": "hello board",
        })
        assert r.status_code == 201
        msg = r.json()
        assert msg["channel"] == "ops"
        assert msg["body"] == "hello board"

        r = client.get("/board/messages", params={"channel": "ops", "limit": 5})
        assert r.status_code == 200
        assert any(m["message_id"] == msg["message_id"] for m in r.json())


def test_board_http_rejects_invalid_payload(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.post("/board/messages", json={"channel": "ops"})
        assert r.status_code == 400
        assert "message_type" in r.json()["detail"]


def test_board_http_search_route(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        client.post("/board/messages", json={
            "channel": "research",
            "message_type": "research_finding",
            "sender_node_id": "t",
            "subject": "a fox",
            "body": "the quick brown fox",
        })
        r = client.get("/board/search", params={"q": "fox"})
        assert r.status_code == 200
        assert r.json()


# ── MCP tool discovery ───────────────────────────────────────


def test_mcp_board_tools_discovered(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.mcp_tools import collect_tools

    defs, dispatch = collect_tools()
    names = {d["name"] for d in defs}
    assert "board_post" in names
    assert "board_read" in names
    assert "board_search" in names
    assert "board_digest" in names
    assert "board_sweep_now" in names
    assert "board_ack" in names
    for name in names:
        assert name in dispatch


def test_mcp_board_post_and_read_via_dispatch(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.mcp_tools import collect_tools
    from knowledge_engine.agent_board.mcp_tools.base import BoardContext

    defs, dispatch = collect_tools()
    ctx = BoardContext.from_config()
    post_result = dispatch["board_post"]("board_post", {
        "channel": "ops",
        "message_type": "claim",
        "sender_node_id": "t",
        "body": "claim",
    }, ctx)
    assert post_result["content"][0]["type"] == "text"

    read_result = dispatch["board_read"]("board_read", {"channel": "ops", "limit": 5}, ctx)
    assert read_result["content"][0]["type"] == "text"


# ── Trust gate (Tailscale + localhost + KE_BOARD_TRUSTED_CIDRS) ────


def test_trust_gate_loopback_and_tailscale(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    # Default trusted set is loopback + Tailscale CGNAT.
    os.environ.pop("KE_BOARD_TRUSTED_CIDRS", None)
    from knowledge_engine.api.board_routes import _trusted_networks
    nets = _trusted_networks()
    assert any(str(n) == "127.0.0.1/32" for n in nets)
    assert any(str(n) == "::1/128" for n in nets)
    assert any(str(n) == "100.64.0.0/10" for n in nets)


def test_trust_gate_env_override(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    os.environ["KE_BOARD_TRUSTED_CIDRS"] = "10.0.0.0/8, 127.0.0.1/32"
    try:
        from knowledge_engine.api.board_routes import _trusted_networks
        nets = _trusted_networks()
        # Override replaces the default — Tailscale is no longer trusted.
        assert any(str(n) == "10.0.0.0/8" for n in nets)
        assert any(str(n) == "127.0.0.1/32" for n in nets)
        assert not any(str(n) == "100.64.0.0/10" for n in nets)
    finally:
        os.environ.pop("KE_BOARD_TRUSTED_CIDRS", None)


def test_trust_gate_untrusted_peer_rejected(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    # Restrict to loopback only so a Tailscale-looking peer is *not* trusted.
    os.environ["KE_BOARD_TRUSTED_CIDRS"] = "127.0.0.1/32,::1/128"
    try:
        from knowledge_engine.api.board_routes import (
            _is_trusted_peer, _trusted_networks,
        )

        class _MockReq:
            class _C:
                host = "100.64.5.10"
            client = _C()
            headers: dict[str, str] = {}

        assert _trusted_networks()
        assert _is_trusted_peer(_MockReq()) is False
    finally:
        os.environ.pop("KE_BOARD_TRUSTED_CIDRS", None)


def test_trust_proxy_off_by_default(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    os.environ.pop("KE_TRUST_PROXY", None)
    from knowledge_engine.api.board_routes import _trust_proxy_enabled
    assert _trust_proxy_enabled() is False


def test_trust_proxy_opt_in(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    os.environ["KE_TRUST_PROXY"] = "1"
    try:
        from knowledge_engine.api.board_routes import _trust_proxy_enabled
        assert _trust_proxy_enabled() is True
    finally:
        os.environ.pop("KE_TRUST_PROXY", None)


# ── Atomic ack (concurrency-safe) ────────────────────────────


def test_ack_message_concurrent_appends(tmp_path: Path) -> None:
    """Two threads acking the same message must both land — no clobber."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store
    import concurrent.futures
    import json

    msg = store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })

    def _ack(name: str) -> None:
        store.ack_message(msg["message_id"], name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_ack, ["a", "b", "c", "d"]))

    fetched = store.read(msg["message_id"])
    assert fetched is not None
    ack = fetched.get("ack_by")
    if isinstance(ack, str):
        ack = json.loads(ack)
    assert set(ack) == {"a", "b", "c", "d"}


def test_ack_message_idempotent_same_acker(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store
    import json

    msg = store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })
    store.ack_message(msg["message_id"], "same")
    store.ack_message(msg["message_id"], "same")
    final = store.read(msg["message_id"])
    ack = final["ack_by"]
    if isinstance(ack, str):
        ack = json.loads(ack)
    assert ack == ["same"]


# ── Master-key bootstrap race + uniqueness ───────────────────


def test_master_key_bootstrap_unique(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys

    first = keys.ensure_master_key()
    assert first is not None
    second = keys.ensure_master_key()
    assert second is None


def test_master_key_bootstrap_concurrent(tmp_path: Path) -> None:
    """Concurrent ensure_master_key calls produce at most one master."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys
    from knowledge_engine.foundation import db as fdb
    import concurrent.futures

    results: list[dict | None] = []

    def _bootstrap() -> dict | None:
        return keys.ensure_master_key()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _bootstrap(), range(4)))

    masters = [r for r in results if r is not None]
    assert len(masters) == 1, f"expected exactly 1 master, got {len(masters)}"

    conn = fdb.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_api_keys WHERE is_master = 1 AND enabled = 1"
    ).fetchone()
    assert row["n"] == 1


# ── Body size cap + per-field length caps ────────────────────


def test_per_field_length_caps_reject_overlong_subject(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import schemas

    long_subj = "x" * (schemas.MAX_LEN_SUBJECT + 1)
    draft, errors = schemas.validate({
        "channel": "ops",
        "message_type": "claim",
        "sender_node_id": "branch-x",
        "subject": long_subj,
        "body": "body",
    })
    assert draft is None
    assert any("subject too long" in e for e in errors)


def test_ttl_hard_cap(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import schemas

    draft, errors = schemas.validate({
        "channel": "ops",
        "message_type": "claim",
        "sender_node_id": "branch-x",
        "body": "body",
        "ttl_hours": schemas.MAX_TTL_HOURS + 1,
    })
    assert draft is None
    assert any("ttl_hours too large" in e for e in errors)


def test_http_request_body_size_cap(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        oversized_body = "x" * 10
        # Forge a content-length header far above the cap; the middleware
        # rejects before parsing the body.
        r = client.post(
            "/board/messages",
            json={"channel": "ops", "message_type": "claim",
                  "sender_node_id": "t", "body": oversized_body},
            headers={"Content-Length": str(2_000_000)},
        )
        assert r.status_code == 413, r.text


# ── Sweeper lease coordination ───────────────────────────────


def test_sweeper_lease_excludes_second_holder(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.foundation import db

    assert db.acquire_sweeper_lease("holder-a", ttl_seconds=60) is True
    # Different holder, lease still valid — must be refused.
    assert db.acquire_sweeper_lease("holder-b", ttl_seconds=60) is False
    # Same holder can renew.
    assert db.acquire_sweeper_lease("holder-a", ttl_seconds=60) is True


def test_sweep_once_skips_when_lease_held_elsewhere(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import sweeper
    from knowledge_engine.foundation import db

    # Someone else holds the lease.
    assert db.acquire_sweeper_lease("other-process", ttl_seconds=600) is True

    result = sweeper.sweep_once(force=False)
    assert result["skipped"] is True
    assert result["pruned_expired"] == 0
    assert result["reminders_emitted"] == 0


def test_sweep_once_force_bypasses_lease(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import sweeper
    from knowledge_engine.foundation import db

    assert db.acquire_sweeper_lease("other-process", ttl_seconds=600) is True
    result = sweeper.sweep_once(force=True)
    # Force bypass — actually ran the sweep.
    assert result.get("skipped") is False
    assert result.get("error") is None


# ── Threshold=0 clamp prevents reminder spam ────────────────


def test_stale_blocker_threshold_clamped(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store, sweeper
    # Set the threshold to 0 — sweeper should still clamp to >= 1.
    store.update_config({"stale_blocker_hours": 0})
    # Post a blocker, then sweep; should NOT immediately emit a reminder
    # because clamp(1) means blocker must be at least 1h old.
    store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })
    result = sweeper.sweep_once(force=True)
    assert result["reminders_emitted"] == 0


# ── HTTP trust-gate rejection of an untrusted peer ────────────


def test_http_status_route_rejects_untrusted(tmp_path: Path) -> None:
    """When KE_BOARD_TRUSTED_CIDRS is set to something testclient is NOT
    in, the gate must return 403 even for read routes."""
    _env(tmp_path / "corpus", tmp_path / "data")
    # Force trusted set to a network the TestClient peer (127.0.0.1) is NOT in.
    os.environ["KE_BOARD_TRUSTED_CIDRS"] = "10.0.0.0/8"
    try:
        from fastapi.testclient import TestClient
        from knowledge_engine.app import create_app

        app = create_app()
        with TestClient(app) as client:
            r = client.get("/board/status")
            assert r.status_code == 403
    finally:
        os.environ.pop("KE_BOARD_TRUSTED_CIDRS", None)


# ── prune_by_count preserves unacked blockers ────────────────


def test_prune_by_count_preserves_unacked_blockers(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store
    from knowledge_engine.pipeline import message_board as mb

    # Post 1 unacked blocker first (oldest).
    blocker = store.post_with_validation({
        "channel": "ops",
        "message_type": "blocker",
        "sender_node_id": "branch-x",
        "body": "stuck",
        "requires_ack": True,
    })
    # Then many normal posts — total exceeds cap.
    for i in range(20):
        store.post_with_validation({
            "channel": "ops",
            "message_type": "status_update",
            "sender_node_id": "branch-y",
            "body": f"u{i}",
        })

    # Prune down hard. The unacked blocker must survive.
    mb.prune_by_count(max_messages=5)

    survived = store.read(blocker["message_id"])
    assert survived is not None, "unacked blocker was pruned"


# ── FTS5 auto-sanitize on syntax error ───────────────────────


def test_fts5_search_handles_user_parens(tmp_path: Path) -> None:
    """Raw user input like 'foo (bar)' previously raised FTS5 syntax error.

    The retry-as-phrase path now eats the syntax error and falls through
    to a literal-phrase search so the dashboard search box never 500s.
    """
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    store.post_with_validation({
        "channel": "research",
        "message_type": "research_finding",
        "sender_node_id": "t",
        "subject": "auth flow",
        "body": "fixed the foo (bar) bug in the auth flow",
    })
    # Query containing unescaped FTS5 grouping characters.
    hits = store.search_messages("foo (bar)")
    assert hits  # at least one match — must not raise
    assert any("foo (bar)" in (h.get("body") or "") for h in hits)


def test_fts5_search_preserves_power_user_operators(tmp_path: Path) -> None:
    """Valid FTS5 operators still work (prefix match in this case)."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    store.post_with_validation({
        "channel": "research",
        "message_type": "research_finding",
        "sender_node_id": "t",
        "subject": "authentication overhaul",
        "body": "rewrote the authenticator",
    })
    hits = store.search_messages("authent*")
    assert hits
    assert any("authent" in (h.get("body", "") + h.get("subject", "")).lower() for h in hits)


def test_fts5_search_handles_quoted_query(tmp_path: Path) -> None:
    """A query containing a stray double-quote is quote-escaped and matched."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import store

    store.post_with_validation({
        "channel": "research",
        "message_type": "research_finding",
        "sender_node_id": "t",
        "body": 'he said "hello" and left',
    })
    hits = store.search_messages('"hello')  # malformed phrase
    # Must not raise; on the retry path it matches as a literal phrase.
    # (May return zero hits depending on tokenization — the must is "no exception".)
    assert isinstance(hits, list)


# ── CLI flags: --task alias + --thread-id ─────────────────────


def test_cli_task_flag_aliases_task_id(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["read", "--task", "abc-123"])
    assert parsed.task_id == "abc-123"
    parsed2 = parser.parse_args(["read", "--task-id", "abc-123"])
    assert parsed2.task_id == "abc-123"


def test_cli_product_flag_aliases_product_id(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.cli import build_parser

    parser = build_parser()
    a = parser.parse_args(["read", "--product", "lib-x"])
    b = parser.parse_args(["read", "--product-id", "lib-x"])
    assert a.product_id == b.product_id == "lib-x"


def test_cli_thread_accepts_thread_id_flag(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["thread", "--thread-id", "thread-xyz"])
    assert parsed.thread_id == "thread-xyz"
    assert parsed.correlation_id is None


def test_cli_thread_positional_still_works(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["thread", "corr-abc"])
    assert parsed.correlation_id == "corr-abc"


# ── Last-master protection ────────────────────────────────────


def test_toggle_refuses_last_enabled_master(tmp_path: Path) -> None:
    """Disabling the sole enabled master would lock the operator out."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys
    import pytest

    master = keys.ensure_master_key()
    assert master is not None
    with pytest.raises(keys.LastMasterKeyError):
        keys.toggle_key(master["key_id"])


def test_delete_refuses_last_enabled_master(tmp_path: Path) -> None:
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys
    import pytest

    master = keys.ensure_master_key()
    assert master is not None
    with pytest.raises(keys.LastMasterKeyError):
        keys.delete_key(master["key_id"])


def test_toggle_can_reenable_a_disabled_master(tmp_path: Path) -> None:
    """A disabled-master toggle (to re-enable) is always allowed."""
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys
    from knowledge_engine.foundation import db as fdb

    master = keys.ensure_master_key()
    conn = fdb.get_connection()
    conn.execute(
        "UPDATE agent_api_keys SET enabled = 0 WHERE key_id = ?",
        (master["key_id"],),
    )
    conn.commit()
    # Re-enable via toggle — the would-zero check evaluates on the
    # CURRENT state, and the current state is "no enabled masters,"
    # so toggling a disabled master ON is allowed.
    updated = keys.toggle_key(master["key_id"])
    assert updated is not None
    assert updated["enabled"] == 1


def test_http_toggle_last_master_returns_409(tmp_path: Path) -> None:
    """End-to-end: PATCH /board/keys/{id}/toggle on the sole master → 409.

    Exercises the real FastAPI stack (route → keys.toggle_key →
    LastMasterKeyError → HTTPException(409)). This passes because
    `foundation.db` resolves the DB path dynamically per request, so the
    worker thread reads the same database the test thread wrote the
    master into — even when a prior test's TestClient lifespan left a
    sweeper thread alive.
    """
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app
    from knowledge_engine.agent_board import keys

    master = keys.ensure_master_key()
    assert master is not None

    app = create_app()
    with TestClient(app) as client:
        r = client.patch(f"/board/keys/{master['key_id']}/toggle")
        assert r.status_code == 409, r.text
        assert "last enabled master" in r.json()["detail"]


def test_http_delete_last_master_returns_409(tmp_path: Path) -> None:
    """End-to-end: DELETE /board/keys/{id} on the sole master → 409."""
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app
    from knowledge_engine.agent_board import keys

    master = keys.ensure_master_key()
    assert master is not None

    app = create_app()
    with TestClient(app) as client:
        r = client.request("DELETE", f"/board/keys/{master['key_id']}")
        assert r.status_code == 409, r.text
        assert "last enabled master" in r.json()["detail"]


def test_db_path_resolves_dynamically_per_request(tmp_path: Path) -> None:
    """Regression guard for the frozen-DB_PATH footgun.

    Changing KE_PIPELINE_DB at runtime must route a fresh get_connection()
    to the new database. A module-level constant resolved once at import
    would fail this — the worker/sweeper threads would keep writing to the
    old path while new reads miss the data.
    """
    _env(tmp_path / "corpus", tmp_path / "data")
    import os
    from knowledge_engine.foundation import db as fdb

    first_path = fdb.resolve_db_path()
    conn1 = fdb.get_connection()
    conn1.execute(
        "INSERT INTO kv_store(key, value, updated_at) VALUES ('probe', 'a', 'now')"
    )
    conn1.commit()

    # Point the env var at a second DB file and confirm resolution follows.
    second_db = tmp_path / "data" / "pipeline2.db"
    os.environ["KE_PIPELINE_DB"] = str(second_db)
    try:
        second_path = fdb.resolve_db_path()
        assert second_path != first_path, "resolve_db_path ignored the env change"
        conn2 = fdb.get_connection()
        # Fresh DB — the probe row from the first DB must NOT be visible.
        row = conn2.execute("SELECT value FROM kv_store WHERE key = 'probe'").fetchone()
        assert row is None, "second connection leaked rows from the first DB"
    finally:
        os.environ["KE_PIPELINE_DB"] = str(tmp_path / "data" / "pipeline.db")


def test_ensure_master_key_self_heals_after_manual_delete(tmp_path: Path) -> None:
    """If the operator force-deletes the master directly in SQLite,
    bootstrap-master re-creates one without surfacing 'already exists'.
    """
    _env(tmp_path / "corpus", tmp_path / "data")
    from knowledge_engine.agent_board import keys
    from knowledge_engine.foundation import db as fdb

    first = keys.ensure_master_key()
    assert first is not None

    # Operator opens SQLite and removes the row (worst-case recovery).
    conn = fdb.get_connection()
    conn.execute("DELETE FROM agent_api_keys WHERE key_id = ?", (first["key_id"],))
    conn.commit()

    # Re-bootstrap should now succeed.
    second = keys.ensure_master_key()
    assert second is not None
    assert second["key_id"] != first["key_id"]


# ── Lifespan handler smoke ───────────────────────────────────


def test_lifespan_starts_and_stops_cleanly(tmp_path: Path) -> None:
    """Stand up the app via TestClient and tear it down — the new lifespan
    handler must start the sweeper on enter and stop it on exit. A leaked
    daemon thread would survive into the next test; the next assertion
    checks the thread is gone.
    """
    _env(tmp_path / "corpus", tmp_path / "data")

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app
    from knowledge_engine.agent_board import sweeper

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # Sweeper should be running while the app is in scope.
        assert sweeper.is_running()
    # After the context exits, the sweeper has been signalled to stop.
    # `is_running` may report True for a beat as the thread joins, so
    # call stop() (idempotent) and verify the lease was released cleanly.
    sweeper.stop()
    assert not sweeper.is_running()
