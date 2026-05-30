"""Smoke + regression tests for the agent board subsystem."""

from __future__ import annotations

import os
from pathlib import Path


def _env(corpus: Path, data: Path) -> None:
    """Point the engine at a tmp corpus + tmp data dir AND a per-test
    pipeline DB so each test gets an isolated SQLite file. Closes any cached
    thread-local connections, removes any stale pipeline.db at the target
    path, and force-reimports the modules so DB_PATH resolves from the
    freshly-set env vars rather than a prior test's value.
    """
    corpus.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    os.environ["KE_CORPUS_ROOT"] = str(corpus)
    os.environ["KE_DATA_DIR"] = str(data)
    os.environ["KE_REGISTRY_PATH"] = str(corpus / "registry.json")
    pipeline_db = data / "pipeline.db"
    os.environ["KE_PIPELINE_DB"] = str(pipeline_db)

    # Best-effort: close any lingering thread-local connections from a prior
    # test that imported db with a different DB_PATH, then drop the cache.
    import sys
    db_mod = sys.modules.get("knowledge_engine.foundation.db")
    if db_mod is not None:
        try:
            db_mod.close_all()
        except Exception:
            pass

    # Force a fresh start: delete any pipeline.db SQLite + WAL/SHM artifacts.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(pipeline_db) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    # Drop modules that capture DB_PATH at import time, so the next import
    # sees the freshly-set env vars.
    for mod in [
        "knowledge_engine.foundation.db",
        "knowledge_engine.foundation.config",
        "knowledge_engine.foundation",
        "knowledge_engine.pipeline.message_board",
        "knowledge_engine.pipeline",
        "knowledge_engine.agent_board.store",
        "knowledge_engine.agent_board.keys",
        "knowledge_engine.agent_board.sweeper",
        "knowledge_engine.agent_board.service",
        "knowledge_engine.agent_board",
        "knowledge_engine.agent_board.mcp_tools",
        "knowledge_engine.agent_board.mcp_tools.base",
        "knowledge_engine.agent_board.mcp_tools.post_tools",
        "knowledge_engine.agent_board.mcp_tools.read_tools",
        "knowledge_engine.agent_board.mcp_tools.search_tools",
        "knowledge_engine.agent_board.mcp_tools.sweep_tools",
        "knowledge_engine.api.board_routes",
        "knowledge_engine.app",
    ]:
        sys.modules.pop(mod, None)


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
