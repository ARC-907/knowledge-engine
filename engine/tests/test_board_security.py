"""Agent Board security model: admin auth, CORS, peer resolution, body cap, scopes, keys."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from knowledge_engine.agent_board import keys, scopes, store
from knowledge_engine.agent_board.mcp_tools import collect_tools
from knowledge_engine.agent_board.mcp_tools.base import BoardContext
from knowledge_engine.app import create_app
from knowledge_engine.core import http as core_http, logging_config
from knowledge_engine.server.api import board_routes

LOOPBACK = ("127.0.0.1", 50000)
TAILSCALE = ("100.64.1.2", 50000)
INTERNET = ("203.0.113.9", 50000)

POST_BODY = {
    "channel": "ops",
    "message_type": "status_update",
    "sender_node_id": "sec-test",
    "body": "hello",
}


@pytest.fixture
def board_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    return data


def _master_headers() -> dict[str, str]:
    master = keys.ensure_master_key()
    assert master is not None
    return {"X-Board-Key": master["raw_key"]}


def _write_key_headers() -> dict[str, str]:
    key = keys.create_key("writer")
    keys.grant_permission(key["key_id"], "board", "*", "write")
    return {"X-Board-Key": key["raw_key"]}


# ── Admin routes always need an admin key ─────────────────────


ADMIN_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/board/keys", None),
    ("POST", "/board/keys", {"display_name": "x"}),
    ("GET", "/board/keys/some-id", None),
    ("PATCH", "/board/keys/some-id/toggle", None),
    ("DELETE", "/board/keys/some-id", None),
    ("POST", "/board/keys/some-id/permissions", {"resource_type": "board"}),
    ("DELETE", "/board/keys/permissions/some-perm", None),
    ("GET", "/board/providers", None),
    ("POST", "/board/providers", {"provider": "ollama"}),
    ("PATCH", "/board/providers/some-id/toggle", None),
    ("POST", "/board/providers/some-id/verify", None),
    ("DELETE", "/board/providers/some-id", None),
    ("PATCH", "/board/config", {"sweep_interval_s": 61}),
    ("POST", "/board/sweep", None),
    ("PUT", "/board/scopes/new-scope", None),
]


@pytest.mark.usefixtures("board_env")
@pytest.mark.parametrize(("method", "path", "body"), ADMIN_CALLS)
def test_admin_routes_require_key_even_from_loopback_with_flag_off(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert store.load_config()["require_key_for_post"] == 0
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = client.request(method, path, json=body)
        assert r.status_code == 401, (path, r.text)
        assert r.json()["detail"] == "Missing X-Board-Key header"


@pytest.mark.usefixtures("board_env")
def test_admin_routes_reject_write_only_key() -> None:
    with TestClient(create_app(), client=LOOPBACK, headers=_write_key_headers()) as client:
        r = client.get("/board/keys")
        assert r.status_code == 403, r.text
        r = client.post("/board/sweep")
        assert r.status_code == 403, r.text


@pytest.mark.usefixtures("board_env")
def test_admin_routes_accept_master_key(tmp_path: Path) -> None:
    with TestClient(create_app(), client=LOOPBACK, headers=_master_headers()) as client:
        assert client.get("/board/keys").status_code == 200
        assert client.post("/board/sweep").status_code == 200
        r = client.patch("/board/config", json={"sweep_interval_s": 61})
        assert r.status_code == 200, r.text
        assert r.json()["sweep_interval_s"] == 61
        assert client.get("/board/providers").status_code == 200
        r = client.put("/board/scopes/proj-a")
        assert r.status_code == 201, r.text
        assert r.json()["created"] is True
        again = client.put("/board/scopes/proj-a")
        assert again.status_code == 200
        assert again.json()["created"] is False


@pytest.mark.usefixtures("board_env")
def test_admin_key_with_admin_grant_but_not_master_is_accepted() -> None:
    key = keys.create_key("ops-admin")
    keys.grant_permission(key["key_id"], "board", "*", "admin")
    headers = {"X-Board-Key": key["raw_key"]}
    with TestClient(create_app(), client=LOOPBACK, headers=headers) as client:
        assert client.get("/board/keys").status_code == 200


@pytest.mark.usefixtures("board_env")
def test_invalid_key_is_401_on_admin_route() -> None:
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = client.get("/board/keys", headers={"X-Board-Key": "keb_not-a-real-key"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid or expired key"


# ── bootstrap-master ──────────────────────────────────────────


@pytest.mark.usefixtures("board_env")
def test_bootstrap_master_once_from_loopback_then_409(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="knowledge_engine.agent_board.http"),
        TestClient(create_app(), client=LOOPBACK) as client,
    ):
        first = client.post("/board/keys/bootstrap-master")
        assert first.status_code == 201, first.text
        raw = first.json()["raw_key"]
        assert raw.startswith(keys.KEY_PREFIX)
        assert "key_hash" not in first.json()

        second = client.post("/board/keys/bootstrap-master")
        assert second.status_code == 409

        # The minted key opens the admin routes.
        assert client.get("/board/keys", headers={"X-Board-Key": raw}).status_code == 200
    assert any("bootstrap-master called" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("board_env")
def test_bootstrap_master_refused_from_non_loopback() -> None:
    with TestClient(create_app(), client=TAILSCALE) as client:
        r = client.post("/board/keys/bootstrap-master")
        assert r.status_code == 403
    assert not keys.has_master_key()


@pytest.mark.usefixtures("board_env")
def test_bootstrap_master_refuses_forwarded_for_even_with_trust_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KE_TRUST_PROXY", "1")
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = client.post("/board/keys/bootstrap-master", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403
    assert not keys.has_master_key()


@pytest.mark.usefixtures("board_env")
def test_default_testclient_peer_is_not_special_cased() -> None:
    """The literal "testclient" host is no longer mapped to loopback."""
    with TestClient(create_app()) as client:
        assert client.get("/board/status").status_code == 403
        assert client.post("/board/keys/bootstrap-master").status_code == 403


# ── CORS ───────────────────────────────────────────────────────


def _preflight(client: TestClient, origin: str, path: str = "/board/keys") -> Any:
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Board-Key, Content-Type",
        },
    )


@pytest.mark.usefixtures("board_env")
def test_cors_preflight_from_foreign_origin_gets_no_allow_origin() -> None:
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = _preflight(client, "https://evil.example")
        assert "access-control-allow-origin" not in r.headers
        assert r.status_code != 200

        # A simple request from a foreign origin: served (same trust gate as
        # before), but without any CORS grant for the browser.
        r = client.get("/board/status", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers


@pytest.mark.usefixtures("board_env")
def test_cors_preflight_from_loopback_origin_succeeds() -> None:
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = _preflight(client, "http://localhost:9210")
        assert r.status_code == 200, r.text
        assert r.headers["access-control-allow-origin"] == "http://localhost:9210"
        allowed_headers = r.headers["access-control-allow-headers"].lower()
        assert "x-board-key" in allowed_headers
        assert "access-control-allow-credentials" not in r.headers


@pytest.mark.usefixtures("board_env")
def test_cors_origins_env_override_and_explicit_star(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CORS_ORIGINS", "https://ui.example , https://other.example")
    with TestClient(create_app(), client=LOOPBACK) as client:
        ok = _preflight(client, "https://ui.example")
        assert ok.headers["access-control-allow-origin"] == "https://ui.example"
        assert "access-control-allow-origin" not in _preflight(client, "http://localhost").headers

    monkeypatch.setenv("KE_CORS_ORIGINS", "*")
    with TestClient(create_app(), client=LOOPBACK) as client:
        assert (
            _preflight(client, "https://evil.example").headers["access-control-allow-origin"] == "*"
        )


@pytest.mark.usefixtures("board_env")
def test_cors_policy_logged_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="knowledge_engine.core.http"):
        create_app()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("CORS: allow_origins=") and "KE_CORS_ORIGINS" in m for m in msgs)


@pytest.mark.usefixtures("board_env")
def test_standalone_app_shares_cors_policy() -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    with TestClient(create_standalone_app(), client=LOOPBACK) as client:
        assert (
            "access-control-allow-origin" not in _preflight(client, "https://evil.example").headers
        )
        ok = _preflight(client, "http://127.0.0.1:11437")
        assert ok.headers["access-control-allow-origin"] == "http://127.0.0.1:11437"


# ── Peer resolution / X-Forwarded-For ─────────────────────────


@pytest.mark.usefixtures("board_env")
def test_xff_spoof_from_untrusted_peer_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_BOARD_TRUSTED_CIDRS", "127.0.0.1/32,::1/128")
    for trust_proxy in ("", "1"):
        monkeypatch.setenv("KE_TRUST_PROXY", trust_proxy)
        with TestClient(create_app(), client=TAILSCALE) as client:
            r = client.get("/board/status", headers={"X-Forwarded-For": "127.0.0.1"})
            assert r.status_code == 403, trust_proxy


@pytest.mark.usefixtures("board_env")
def test_xff_ignored_entirely_when_trust_proxy_off() -> None:
    assert not board_routes._trust_proxy_enabled()
    with TestClient(create_app(), client=LOOPBACK) as client:
        # A loopback peer naming an internet client is still judged as loopback.
        r = client.get("/board/status", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200


@pytest.mark.usefixtures("board_env")
def test_trust_proxy_picks_rightmost_untrusted_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_TRUST_PROXY", "1")
    with TestClient(create_app(), client=LOOPBACK) as client:
        # Client-controlled leftmost entry is trusted-looking; the proxy
        # appended the real (internet) client on the right → untrusted.
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, 203.0.113.9"})
        assert r.status_code == 403
        # Real client is on the mesh; the loopback hop on the right is a
        # trusted proxy and is skipped.
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, 127.0.0.1"})
        assert r.status_code == 200
        # Internet client behind a mesh hop that is NOT a trusted proxy by
        # default: the mesh address is the effective client.
        r = client.get("/board/status", headers={"X-Forwarded-For": "203.0.113.9, 100.64.1.2"})
        assert r.status_code == 200
        # Malformed hop → untrusted.
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, not-an-ip"})
        assert r.status_code == 403


@pytest.mark.usefixtures("board_env")
def test_trusted_proxies_env_extends_the_proxy_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_TRUST_PROXY", "1")
    monkeypatch.setenv("KE_TRUSTED_PROXIES", "127.0.0.1/32,100.64.0.0/10")
    with TestClient(create_app(), client=LOOPBACK) as client:
        # Now the mesh hop is a proxy too, so the internet client is exposed.
        r = client.get("/board/status", headers={"X-Forwarded-For": "203.0.113.9, 100.64.1.2"})
        assert r.status_code == 403
        status = client.get("/board/status").json()
        assert "100.64.0.0/10" in status["trusted_proxies"]
    # A non-proxy peer still cannot speak for anyone.
    with TestClient(create_app(), client=INTERNET) as client:
        r = client.get("/board/status", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


@pytest.mark.usefixtures("board_env")
def test_trust_policy_logged_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="knowledge_engine.agent_board.http"),
        TestClient(create_app(), client=LOOPBACK),
    ):
        pass
    trust = [r.getMessage() for r in caplog.records if r.getMessage().startswith("board trust:")]
    assert len(trust) == 1
    assert "trusted_cidrs=['127.0.0.1/32', '::1/128', '100.64.0.0/10']" in trust[0]
    assert "require_key_for_post=False" in trust[0]
    assert "trust_proxy=False" in trust[0]


# ── WRITE gate: require_key_for_post ──────────────────────────


@pytest.mark.usefixtures("board_env")
def test_write_gate_with_flag_on() -> None:
    store.update_config({"require_key_for_post": 1})
    write_headers = _write_key_headers()
    app = create_app()
    with TestClient(app, client=TAILSCALE) as mesh:
        assert mesh.post("/board/messages", json=POST_BODY).status_code == 401
        bad = mesh.post("/board/messages", json=POST_BODY, headers={"X-Board-Key": "keb_x"})
        assert bad.status_code == 401
        ok = mesh.post("/board/messages", json=POST_BODY, headers=write_headers)
        assert ok.status_code == 201, ok.text
    with TestClient(app, client=LOOPBACK) as local:
        # Loopback writes stay keyless (documented) ...
        assert local.post("/board/messages", json=POST_BODY).status_code == 201
        # ... but a presented key must still be valid.
        bad = local.post("/board/messages", json=POST_BODY, headers={"X-Board-Key": "keb_x"})
        assert bad.status_code == 401


@pytest.mark.usefixtures("board_env")
def test_write_gate_with_flag_off_is_keyless_for_trusted_peers() -> None:
    with TestClient(create_app(), client=TAILSCALE) as mesh:
        assert mesh.post("/board/messages", json=POST_BODY).status_code == 201
    with TestClient(create_app(), client=INTERNET) as outsider:
        assert outsider.post("/board/messages", json=POST_BODY).status_code == 403


# ── Body cap ───────────────────────────────────────────────────


def _chunks(total: int, size: int = 64 * 1024) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        yield b"x" * n
        sent += n


@pytest.mark.usefixtures("board_env")
def test_chunked_oversize_body_is_rejected_with_413(caplog: pytest.LogCaptureFixture) -> None:
    too_big = board_routes.MAX_REQUEST_BODY_BYTES + 1
    with (
        caplog.at_level(logging.WARNING, logger="knowledge_engine.core.http"),
        TestClient(create_app(), client=LOOPBACK) as client,
    ):
        r = client.post(
            "/board/messages",
            content=_chunks(too_big),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        assert r.status_code == 413, r.text
        assert "request body too large" in r.json()["detail"]
        # The app is still healthy afterwards.
        assert client.get("/board/status").status_code == 200
    # No Content-Length was sent, so the streamed counter did the rejecting.
    assert any("streamed body exceeded cap" in rec.getMessage() for rec in caplog.records)


@pytest.mark.usefixtures("board_env")
def test_body_cap_applies_to_standalone_app() -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    with TestClient(create_standalone_app(), client=LOOPBACK) as client:
        r = client.post(
            "/board/messages",
            content=_chunks(board_routes.MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        assert r.status_code == 413


@pytest.mark.usefixtures("board_env")
def test_declared_length_413_carries_cors_grant_for_allowed_origin() -> None:
    """The body-cap middleware sits inside CORS, so its 413 is not opaque."""
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = client.post(
            "/board/messages",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://localhost:9210",
                "Content-Length": str(board_routes.MAX_REQUEST_BODY_BYTES + 10),
            },
        )
        assert r.status_code == 413
        assert r.headers["access-control-allow-origin"] == "http://localhost:9210"


@pytest.mark.usefixtures("board_env")
def test_body_cap_leaves_non_board_routes_alone() -> None:
    with TestClient(create_app(), client=LOOPBACK) as client:
        big = {"id": "k", "kind": "kit", "name": "x" * 2_000, "path": "kits/x"}
        r = client.post(
            "/registry",
            json=big,
            headers={"Content-Length": str(board_routes.MAX_REQUEST_BODY_BYTES + 10)},
        )
        assert r.status_code != 413


# ── Scopes ────────────────────────────────────────────────────


def test_reads_never_create_a_scope(board_env: Path) -> None:
    root = scopes.scopes_root()
    with TestClient(create_app(), client=LOOPBACK) as client:
        assert client.get("/board/messages", params={"scope": "brand-new"}).json() == []
        assert client.get("/board/messages/x", params={"scope": "brand-new"}).status_code == 404
        assert client.get("/board/search", params={"q": "x", "scope": "brand-new"}).json() == []
        digest = client.get("/board/digest", params={"scope": "brand-new"}).json()
        assert digest["scanned"] == 0
        cfg = client.get("/board/config", params={"scope": "brand-new"}).json()
        assert cfg["require_key_for_post"] == 0
        assert "ops" in cfg["channels"]
        assert client.get("/board/stats/channels", params={"scope": "brand-new"}).json() == []
        assert client.get("/board/stats/types", params={"scope": "brand-new"}).json() == []
        assert client.get("/board/threads/c", params={"scope": "brand-new"}).json() == []
        ack = client.post(
            "/board/messages/x/ack", json={"from": "n"}, params={"scope": "brand-new"}
        )
        assert ack.status_code == 404
        assert client.get("/board/scopes").json()["scopes"] == []
    assert not (root / "brand-new.db").exists()
    assert not root.exists()


@pytest.mark.usefixtures("board_env")
def test_mcp_reads_never_create_a_scope() -> None:
    _, dispatch = collect_tools()
    ctx = BoardContext.from_config()
    env = dispatch["board_read"]("board_read", {"scope": "brand-new"}, ctx)
    assert json.loads(env["content"][0]["text"]) == []
    env = dispatch["board_digest"]("board_digest", {"scope": "brand-new"}, ctx)
    assert json.loads(env["content"][0]["text"])["scanned"] == 0
    assert not scopes.scopes_root().exists()


@pytest.mark.usefixtures("board_env")
def test_post_creates_scope_and_reads_follow() -> None:
    with TestClient(create_app(), client=LOOPBACK) as client:
        r = client.post("/board/messages", json={**POST_BODY, "scope": "proj_1"})
        assert r.status_code == 201, r.text
        assert (scopes.scopes_root() / "proj_1.db").exists()
        assert [
            m["body"] for m in client.get("/board/messages", params={"scope": "proj_1"}).json()
        ] == ["hello"]


@pytest.mark.usefixtures("board_env")
@pytest.mark.parametrize(
    "bad",
    ["Branch/Feat", "../etc", "has space", "UPPER", "-leading", "dot.ted", "x" * 65, "_u"],
)
def test_scope_name_validation_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError, match=r"invalid scope|non-empty"):
        scopes.validate_scope(bad)
    with TestClient(create_app(), client=LOOPBACK, headers=_master_headers()) as client:
        assert client.get("/board/messages", params={"scope": bad}).status_code == 400
        assert client.post("/board/messages", json={**POST_BODY, "scope": bad}).status_code == 400
        assert client.put(f"/board/scopes/{bad}").status_code in (400, 404)
    assert not scopes.scopes_root().exists()


@pytest.mark.usefixtures("board_env")
def test_scope_name_validation_accepts_good_names() -> None:
    for good in ("a", "proj-a", "branch_feat-auth", "0abc", "x" * 64):
        assert scopes.validate_scope(good) == good


@pytest.mark.usefixtures("board_env")
def test_mcp_post_rejects_bad_scope() -> None:
    _, dispatch = collect_tools()
    env = dispatch["board_post"]("board_post", {**POST_BODY, "scope": "Bad/Scope"}, None)
    assert "invalid scope" in json.loads(env["content"][0]["text"])["error"]
    env = dispatch["board_read"]("board_read", {"scope": "Bad/Scope"}, None)
    assert "invalid scope" in json.loads(env["content"][0]["text"])["error"]


@pytest.mark.usefixtures("board_env")
def test_max_scopes_enforced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KE_BOARD_MAX_SCOPES", "2")
    with (
        caplog.at_level(logging.WARNING, logger="knowledge_engine.agent_board.scopes"),
        TestClient(create_app(), client=LOOPBACK, headers=_master_headers()) as client,
    ):
        assert client.put("/board/scopes/s-one").status_code == 201
        assert client.put("/board/scopes/s-one").status_code == 200  # idempotent
        assert (
            client.post("/board/messages", json={**POST_BODY, "scope": "s-two"}).status_code == 201
        )

        third = client.put("/board/scopes/s-three")
        assert third.status_code == 507, third.text
        assert "scope limit reached" in third.json()["detail"]
        posted = client.post("/board/messages", json={**POST_BODY, "scope": "s-four"})
        assert posted.status_code == 507
        patched = client.patch("/board/config", json={"sweep_interval_s": 5, "scope": "s-five"})
        assert patched.status_code == 507

        # Existing scopes remain fully usable.
        assert (
            client.post("/board/messages", json={**POST_BODY, "scope": "s-two"}).status_code == 201
        )
        listed = client.get("/board/scopes").json()
        assert {s["scope"] for s in listed["scopes"]} == {"s-one", "s-two"}
        assert listed["max_scopes"] == 2
    assert any("refusing to create scope" in r.getMessage() for r in caplog.records)


def test_max_scopes_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KE_BOARD_MAX_SCOPES", raising=False)
    assert scopes.max_scopes() == scopes.DEFAULT_MAX_SCOPES
    monkeypatch.setenv("KE_BOARD_MAX_SCOPES", "junk")
    assert scopes.max_scopes() == scopes.DEFAULT_MAX_SCOPES
    monkeypatch.setenv("KE_BOARD_MAX_SCOPES", "0")
    assert scopes.max_scopes() == 0


# ── Keys ──────────────────────────────────────────────────────


@pytest.mark.usefixtures("board_env")
def test_key_responses_never_contain_key_hash() -> None:
    headers = _master_headers()
    with TestClient(create_app(), client=LOOPBACK, headers=headers) as client:
        created = client.post(
            "/board/keys",
            json={
                "display_name": "agent-1",
                "permissions": [{"resource_type": "board", "permission": "write"}],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert "key_hash" not in body
        assert "raw_key" in body
        key_id = body["key_id"]

        listed = client.get("/board/keys").json()
        assert listed and all("key_hash" not in k for k in listed)

        summary = client.get(f"/board/keys/{key_id}").json()
        assert "key_hash" not in summary
        assert "raw_key" not in summary
        assert summary["permissions"][0]["permission"] == "write"

        toggled = client.patch(f"/board/keys/{key_id}/toggle").json()
        assert "key_hash" not in toggled
        assert toggled["enabled"] == 0

    record = keys.verify_key(body["raw_key"])
    assert record is None  # disabled above
    assert "key_hash" not in (keys.get_key(key_id) or {})
    master_record = keys.verify_key(headers["X-Board-Key"])
    assert master_record is not None
    assert "key_hash" not in master_record


@pytest.mark.usefixtures("board_env")
def test_key_lifecycle_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="knowledge_engine.agent_board.keys"):
        key = keys.create_key("logged")
        keys.delete_key(key["key_id"])
    msgs = [r.getMessage() for r in caplog.records]
    assert any("board key created" in m and key["key_id"] in m and "logged" in m for m in msgs)
    assert any("board key revoked" in m and key["key_id"] in m for m in msgs)
    assert all(key["raw_key"] not in m for m in msgs)


def test_master_key_file_is_created_0600(board_env: Path) -> None:
    master = keys.ensure_master_key()
    assert master is not None
    key_file = board_env / keys.MASTER_KEY_FILENAME
    assert key_file.is_file()
    assert master["raw_key"] in key_file.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_master_key_file_failure_is_logged_not_raised(
    board_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    board_env.mkdir(parents=True, exist_ok=True)
    (board_env / keys.MASTER_KEY_FILENAME).mkdir()  # not a regular file
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.agent_board.keys"):
        master = keys.ensure_master_key()
    assert master is not None
    assert any("not a regular file" in r.getMessage() for r in caplog.records)


def test_master_key_file_replaces_stale_copy(board_env: Path) -> None:
    first = keys.ensure_master_key()
    assert first is not None
    conn = keys.db.get_connection()
    conn.execute("DELETE FROM agent_api_keys WHERE key_id = ?", (first["key_id"],))
    conn.commit()
    second = keys.ensure_master_key()
    assert second is not None
    text = (board_env / keys.MASTER_KEY_FILENAME).read_text(encoding="utf-8")
    assert second["raw_key"] in text
    assert first["raw_key"] not in text


# ── MCP write gate ────────────────────────────────────────────


def _mcp_text(env: dict[str, Any]) -> dict[str, Any]:
    return json.loads(env["content"][0]["text"])


@pytest.mark.usefixtures("board_env")
def test_mcp_post_tools_advertise_optional_board_key() -> None:
    defs, _ = collect_tools()
    for tool in defs:
        if tool["name"] in (
            "board_post",
            "board_claim",
            "board_release",
            "board_blocker",
            "board_ack",
        ):
            props = tool["inputSchema"]["properties"]
            assert "board_key" in props, tool["name"]
            assert "board_key" not in tool["inputSchema"].get("required", [])


@pytest.mark.usefixtures("board_env")
def test_mcp_post_keyless_when_flag_off_and_invalid_key_ignored() -> None:
    _, dispatch = collect_tools()
    ctx = BoardContext.from_config()
    msg = _mcp_text(dispatch["board_post"]("board_post", dict(POST_BODY), ctx))
    assert msg["body"] == "hello"
    msg = _mcp_text(
        dispatch["board_post"]("board_post", {**POST_BODY, "board_key": "keb_bogus"}, ctx)
    )
    assert msg["body"] == "hello"
    assert "board_key" not in msg


@pytest.mark.usefixtures("board_env")
def test_mcp_post_with_flag_on_validates_supplied_key() -> None:
    _, dispatch = collect_tools()
    ctx = BoardContext.from_config()  # built while the flag is still off
    store.update_config({"require_key_for_post": 1})

    # Loopback-equivalent: keyless still allowed.
    ok = _mcp_text(dispatch["board_post"]("board_post", dict(POST_BODY), ctx))
    assert ok["body"] == "hello"
    # A supplied key must be valid ...
    bad = _mcp_text(
        dispatch["board_post"]("board_post", {**POST_BODY, "board_key": "keb_bogus"}, ctx)
    )
    assert bad["error"] == "Invalid or expired key"
    assert bad["status"] == 401
    # ... and hold board:write.
    reader = keys.create_key("reader")
    keys.grant_permission(reader["key_id"], "board", "*", "read")
    denied = _mcp_text(
        dispatch["board_claim"](
            "board_claim",
            {"sender_node_id": "n", "task_id": "t", "board_key": reader["raw_key"]},
            ctx,
        )
    )
    assert denied["status"] == 403
    writer = keys.create_key("writer")
    keys.grant_permission(writer["key_id"], "board", "*", "write")
    claimed = _mcp_text(
        dispatch["board_claim"](
            "board_claim",
            {"sender_node_id": "n", "task_id": "t", "board_key": writer["raw_key"]},
            ctx,
        )
    )
    assert claimed["message_type"] == "claim"
    acked = _mcp_text(
        dispatch["board_ack"](
            "board_ack",
            {"message_id": claimed["message_id"], "acker": "m", "board_key": writer["raw_key"]},
            None,
        )
    )
    assert "m" in acked["ack_by"]


# ── Server wiring: logging + uvicorn proxy headers ─────────────
#
# The policy lines above are only useful if the real server emits them, and
# the X-Forwarded-For rules only hold if uvicorn does not rewrite the peer
# first. Both `serve` entry points must therefore hand uvicorn a log config
# with a root handler and proxy settings derived from KE_TRUST_PROXY.


def _capture_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return seen


def test_uvicorn_proxy_kwargs_follow_trust_proxy_policy() -> None:
    off = core_http.uvicorn_proxy_kwargs({})
    assert off == {"proxy_headers": False, "forwarded_allow_ips": []}
    on = core_http.uvicorn_proxy_kwargs({"KE_TRUST_PROXY": "1"})
    assert on["proxy_headers"] is True
    assert on["forwarded_allow_ips"] == ["127.0.0.0/8", "::1/128"]
    custom = core_http.uvicorn_proxy_kwargs(
        {"KE_TRUST_PROXY": "yes", "KE_TRUSTED_PROXIES": "100.64.0.0/10, 10.1.2.3/32"}
    )
    assert custom["forwarded_allow_ips"] == ["100.64.0.0/10", "10.1.2.3/32"]
    # An empty proxy set with the flag on: nothing is trusted, nothing rewritten.
    assert core_http.uvicorn_proxy_kwargs({"KE_TRUST_PROXY": "1", "KE_TRUSTED_PROXIES": ""}) == {
        "proxy_headers": True,
        "forwarded_allow_ips": [],
    }


def test_uvicorn_log_config_adds_root_handler_for_engine_loggers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = logging_config.uvicorn_log_config()
    assert cfg["root"]["handlers"] == ["knowledge_engine"]
    assert cfg["loggers"]["knowledge_engine"] == {"level": "INFO", "propagate": True}
    # uvicorn's own loggers are untouched.
    assert cfg["loggers"]["uvicorn"]["propagate"] is False
    assert cfg["handlers"]["knowledge_engine"]["stream"] == "ext://sys.stderr"
    assert logging_config.uvicorn_log_config("DEBUG")["loggers"]["knowledge_engine"]["level"] == (
        "DEBUG"
    )
    assert logging_config.resolve_log_level({"KE_LOG_LEVEL": " warning "}) == "WARNING"
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.logging_config"):
        assert logging_config.resolve_log_level({"KE_LOG_LEVEL": "loud"}) == "INFO"
    assert any("invalid KE_LOG_LEVEL" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("board_env")
def test_serve_passes_log_config_and_proxy_policy_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from knowledge_engine import cli
    from knowledge_engine.agent_board import service

    seen = _capture_uvicorn_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "serve", "--port", "0"])
    assert cli.main() == 0
    assert seen["app"] == "knowledge_engine.app:create_app"
    assert seen["factory"] is True
    assert seen["log_config"]["root"]["handlers"] == ["knowledge_engine"]
    assert seen["proxy_headers"] is False
    assert seen["forwarded_allow_ips"] == []

    monkeypatch.setenv("KE_TRUST_PROXY", "1")
    monkeypatch.setenv("KE_TRUSTED_PROXIES", "100.64.0.0/10")
    monkeypatch.setenv("KE_LOG_LEVEL", "debug")
    seen = _capture_uvicorn_run(monkeypatch)
    assert service.serve(host="127.0.0.1", port=0) == 0
    assert seen["app"] == "knowledge_engine.agent_board.service:create_standalone_app"
    assert seen["proxy_headers"] is True
    assert seen["forwarded_allow_ips"] == ["100.64.0.0/10"]
    assert seen["log_config"]["loggers"]["knowledge_engine"]["level"] == "DEBUG"


@pytest.mark.usefixtures("board_env")
def test_uvicorn_proxy_middleware_agrees_with_app_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the app behind uvicorn's ProxyHeadersMiddleware exactly as `serve`
    would configure it and check the trust gate's verdicts do not change."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    def served_app() -> Any:
        kwargs = core_http.uvicorn_proxy_kwargs()
        app: Any = create_app()
        if kwargs["proxy_headers"]:
            app = ProxyHeadersMiddleware(app, trusted_hosts=kwargs["forwarded_allow_ips"])
        return app

    # KE_TRUST_PROXY unset: no middleware at all, so a loopback peer naming an
    # internet client is still judged as loopback (the documented behaviour).
    with TestClient(served_app(), client=LOOPBACK) as client:
        r = client.get("/board/status", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200

    monkeypatch.setenv("KE_TRUST_PROXY", "1")
    with TestClient(served_app(), client=LOOPBACK) as client:
        # Same verdicts as test_trust_proxy_picks_rightmost_untrusted_hop.
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, 203.0.113.9"})
        assert r.status_code == 403
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, 127.0.0.1"})
        assert r.status_code == 200
        r = client.get("/board/status", headers={"X-Forwarded-For": "203.0.113.9, 100.64.1.2"})
        assert r.status_code == 200
        r = client.get("/board/status", headers={"X-Forwarded-For": "100.64.1.2, not-an-ip"})
        assert r.status_code == 403
    # uvicorn only trusts the configured proxies, so a non-proxy peer still
    # cannot speak for anyone.
    with TestClient(served_app(), client=INTERNET) as client:
        r = client.get("/board/status", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


_STARTUP_LOG_SCRIPT = """
import logging.config, sys
from fastapi.testclient import TestClient
from knowledge_engine.app import create_app
from knowledge_engine.core.logging_config import uvicorn_log_config

logging.config.dictConfig(uvicorn_log_config())
with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
    assert client.get("/board/status").status_code == 200
sys.stdout.write("done")
"""


@pytest.mark.usefixtures("board_env")
def test_startup_policy_lines_reach_stderr_under_server_log_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The INFO policy lines must surface with the server's real logging
    configuration — no caplog, no test-level overrides — which is what
    SECURITY.md tells operators to watch for."""
    monkeypatch.setenv("KE_BOARD_SWEEPER", "0")
    monkeypatch.setenv("NO_COLOR", "1")

    def run(level: str | None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("KE_LOG_LEVEL", None)
        if level is not None:
            env["KE_LOG_LEVEL"] = level
        return subprocess.run(
            [sys.executable, "-c", _STARTUP_LOG_SCRIPT],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )

    proc = run(None)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "done"
    assert "CORS: allow_origins=" in proc.stderr, proc.stderr
    assert "board trust: trusted_cidrs=" in proc.stderr, proc.stderr
    assert "knowledge_engine.agent_board.http:" in proc.stderr
    assert "INFO" in proc.stderr

    quiet = run("WARNING")
    assert quiet.returncode == 0, quiet.stderr
    assert "board trust:" not in quiet.stderr
    assert "CORS: allow_origins=" not in quiet.stderr
