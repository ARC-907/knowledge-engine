"""FastAPI routes for the Agent Board.

Mounted at `/board/*` by `app.create_app()` when `KE_BOARD_ENABLED` != `0`.

Peer-trust gate:
    Local-trust by default. The board treats loopback (127.0.0.1, ::1) and
    the Tailscale CGNAT range (100.64.0.0/10) as trusted peers — write
    routes are open to those without a key when `require_key_for_post=0`
    (the default). Everyone else needs an `X-Board-Key`.

    Extend / restrict the trusted set with `KE_BOARD_TRUSTED_CIDRS`
    (comma-separated CIDRs). Set the env var to the empty string to drop
    Tailscale trust entirely (loopback-only).

X-Forwarded-For is **opt-in** via `KE_TRUST_PROXY=1` — set this only when
the engine sits behind a reverse proxy you control. Untrusted proxies can
forge XFF.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ..agent_board import keys, schemas, store, sweeper


router = APIRouter()


# ── Trust gate ─────────────────────────────────────────────────


_LOCALHOST_V4 = ipaddress.ip_address("127.0.0.1")
_LOCALHOST_V6 = ipaddress.ip_address("::1")
# Tailscale CGNAT range — every Tailscale-issued IPv4 lives here. Treating
# the whole /10 as trusted means peers on the operator's private mesh
# don't need to round-trip through key creation just to coordinate.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _parse_cidrs(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of CIDRs. Bad entries are silently
    dropped — the trust gate is a defence in depth on top of key auth, so
    a typo costs nothing more than an over-eager 403."""
    if raw is None or not raw.strip():
        return []
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(ipaddress.ip_network(chunk, strict=False))
        except (ValueError, TypeError):
            continue
    return out


def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The active trusted-CIDR list.

    Defaults to loopback (v4 + v6) + Tailscale CGNAT. The
    `KE_BOARD_TRUSTED_CIDRS` env var REPLACES the default — set it to e.g.
    `127.0.0.1/32,::1/128` to disable Tailscale trust, or add extra
    private-mesh ranges if you operate outside Tailscale.
    """
    raw = os.environ.get("KE_BOARD_TRUSTED_CIDRS")
    if raw is not None:
        return _parse_cidrs(raw)
    return [
        ipaddress.ip_network("127.0.0.1/32"),
        ipaddress.ip_network("::1/128"),
        _TAILSCALE_NET,
    ]


def _trust_proxy_enabled() -> bool:
    raw = os.environ.get("KE_TRUST_PROXY", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _peer_address(req: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        client = req.client.host if req.client else ""
        # Starlette's TestClient reports a literal "testclient" host that
        # isn't a valid IP. Treat it as loopback for ASGI test transports;
        # production servers never send this literal.
        if client == "testclient":
            return _LOCALHOST_V4
        if client.startswith("::ffff:"):
            client = client[7:]
        return ipaddress.ip_address(client) if client else None
    except (ValueError, TypeError):
        return None


def _is_trusted_peer(req: Request) -> bool:
    """Return True if the immediate request peer is on a trusted network.

    When `KE_TRUST_PROXY=1`, also accepts an `X-Forwarded-For` first-hop
    that is itself trusted, allowing a reverse proxy on the operator's
    own machine to forward Tailscale or loopback peers transparently.
    """
    peer = _peer_address(req)
    if peer is None:
        return False
    nets = _trusted_networks()
    if not nets:
        return False
    direct = any(peer in net for net in nets)
    if direct and _trust_proxy_enabled():
        forwarded = req.headers.get("X-Forwarded-For")
        if forwarded:
            head = forwarded.split(",", 1)[0].strip()
            try:
                if head.startswith("::ffff:"):
                    head = head[7:]
                inner = ipaddress.ip_address(head)
            except (ValueError, TypeError):
                return False
            return any(inner in net for net in nets)
    return direct


# Backwards-compatible alias for any external callers that imported the
# old name. Internally everyone now uses `_is_trusted_peer`.
def _is_localhost(req: Request) -> bool:
    return _is_trusted_peer(req)


def _require_trust(req: Request) -> None:
    """Hard gate: reject any peer not on a trusted network."""
    if not _is_trusted_peer(req):
        raise HTTPException(403, "peer not on a trusted network")


# Hard ceiling on a single board request body. The per-field caps in
# schemas.validate enforce semantic limits; this is the brute-force
# defence against an attacker streaming megabytes at the FastAPI body
# parser before validation has a chance to fire.
MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MiB


def _check_body_size(req: Request) -> None:
    cl = req.headers.get("content-length")
    if not cl:
        return
    try:
        size = int(cl)
    except (TypeError, ValueError):
        return
    if size > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(
            413,
            f"request body too large (max {MAX_REQUEST_BODY_BYTES} bytes; got {size})",
        )


def _require_key(req: Request, resource_id: str = "*", permission: str = "write") -> None:
    """Trust-gate the request, then conditionally key-gate writes.

    Even when `require_key_for_post=0`, only trusted peers reach the
    endpoint logic — untrusted peers always get 403. Setting
    `require_key_for_post=1` additionally requires a valid `X-Board-Key`
    for non-loopback writes, so an operator who shares the mesh with
    untrusted machines can still lock the board down.
    """
    _require_trust(req)
    _check_body_size(req)
    cfg = store.load_config()
    if not cfg.get("require_key_for_post"):
        return
    # Loopback callers are still trusted-without-key by default — change
    # via require_key_for_post=1 + an additional gate if you want to
    # require keys for loopback too.
    peer = _peer_address(req)
    if peer is not None and (peer == _LOCALHOST_V4 or peer == _LOCALHOST_V6):
        return
    raw = req.headers.get("X-Board-Key", "").strip()
    if not raw:
        raise HTTPException(401, "Missing X-Board-Key header")
    record = keys.verify_key(raw)
    if record is None:
        raise HTTPException(401, "Invalid or expired key")
    if not keys.check_permission(record["key_id"], "board", resource_id, permission):
        raise HTTPException(403, "Key lacks board:write permission")


# ── Status / metadata ──────────────────────────────────────────


@router.get("/status")
def status(req: Request) -> dict[str, Any]:
    _require_trust(req)
    cfg = store.load_config()
    return {
        "service": "knowledge-engine-board",
        "messages_total": store.total_count(),
        "channel_stats": store.channel_stats(),
        "last_sweep": store.last_sweep(),
        "sweeper_running": sweeper.is_running(),
        "trusted_networks": [str(n) for n in _trusted_networks()],
        "trust_proxy": _trust_proxy_enabled(),
        "config": {
            "engine_port": cfg["engine_port"],
            "standalone_port": cfg["standalone_port"],
            "sweep_interval_s": cfg["sweep_interval_s"],
            "stale_blocker_hours": cfg["stale_blocker_hours"],
            "digest_interval_minutes": cfg["digest_interval_minutes"],
            "max_messages_before_prune": cfg["max_messages_before_prune"],
            "default_ttl_hours": cfg["default_ttl_hours"],
            "sweeper_enabled": bool(cfg["sweeper_enabled"]),
            "require_key_for_post": bool(cfg["require_key_for_post"]),
            "channels": cfg["channels"],
        },
    }


@router.get("/channels")
def list_channels(req: Request) -> dict[str, Any]:
    _require_trust(req)
    cfg = store.load_config()
    return {"channels": cfg["channels"], "defaults": list(schemas.DEFAULT_CHANNELS)}


@router.get("/message_types")
def list_message_types(req: Request) -> dict[str, Any]:
    _require_trust(req)
    return {
        "message_types": list(schemas.MESSAGE_TYPES),
        "visibility_scopes": list(schemas.VISIBILITY_SCOPES),
    }


# ── Messages ───────────────────────────────────────────────────


@router.get("/messages")
def list_messages(
    req: Request,
    since: str | None = Query(None),
    channel: str | None = Query(None),
    message_type: str | None = Query(None),
    task_id: str | None = Query(None),
    product_id: str | None = Query(None),
    sender_node_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return store.poll(
        since=since, channel=channel, message_type=message_type,
        task_id=task_id, product_id=product_id, sender_node_id=sender_node_id,
        limit=limit,
    )


@router.get("/messages/{message_id}")
def get_message(req: Request, message_id: str) -> dict[str, Any]:
    _require_trust(req)
    msg = store.read(message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@router.post("/messages", status_code=201)
def post_message(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="write")
    try:
        msg = store.post_with_validation(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return msg


@router.post("/messages/{message_id}/ack", status_code=200)
def ack_message(
    req: Request,
    message_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="write")
    acker = str(payload.get("from") or payload.get("acker") or "").strip()
    if not acker:
        raise HTTPException(400, "'from' (acker identity) is required")
    msg = store.ack_message(message_id, acker)
    if msg is None:
        raise HTTPException(404, "message not found")
    return msg


# ── Threads / search / digest ──────────────────────────────────


@router.get("/threads/{correlation_id}")
def get_thread(
    req: Request,
    correlation_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return store.thread_messages(correlation_id=correlation_id, limit=limit)


@router.get("/search")
def search(
    req: Request,
    q: str = Query(..., min_length=1),
    channel: str | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return store.search_messages(query=q, channel=channel, limit=limit)


@router.get("/digest")
def digest(
    req: Request,
    channel: str | None = Query(None),
    since: str | None = Query(None),
    max_messages: int = Query(200, ge=10, le=2000),
) -> dict[str, Any]:
    _require_trust(req)
    return store.digest(channel=channel, since=since, max_messages=max_messages)


# ── Stats ──────────────────────────────────────────────────────


@router.get("/stats/channels")
def stats_channels(req: Request) -> list[dict[str, Any]]:
    _require_trust(req)
    return store.channel_stats()


@router.get("/stats/types")
def stats_types(req: Request, channel: str | None = Query(None)) -> list[dict[str, Any]]:
    _require_trust(req)
    return store.type_stats(channel=channel)


# ── Sweeper ────────────────────────────────────────────────────


@router.post("/sweep")
def sweep_now(req: Request) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    return sweeper.sweep_once(force=True)


# ── Keys ───────────────────────────────────────────────────────


@router.get("/keys")
def list_keys(req: Request) -> list[dict[str, Any]]:
    _require_key(req, resource_id="*", permission="admin")
    return keys.list_keys(include_master=False)


@router.post("/keys", status_code=201)
def create_key(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    name = str(payload.get("display_name") or "").strip()
    if not name:
        raise HTTPException(400, "display_name is required")
    if len(name) > 200:
        raise HTTPException(400, "display_name too long (max 200 chars)")
    notes = payload.get("notes")
    expires_at = payload.get("expires_at")
    key = keys.create_key(
        display_name=name, notes=notes, expires_at=expires_at,
    )
    permissions = payload.get("permissions") or []
    for grant in permissions:
        try:
            keys.grant_permission(
                key_id=key["key_id"],
                resource_type=str(grant.get("resource_type", "*")),
                resource_id=str(grant.get("resource_id", "*")),
                permission=str(grant.get("permission", "invoke")),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return key


@router.get("/keys/{key_id}")
def get_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    summary = keys.get_key_summary(key_id)
    if summary is None:
        raise HTTPException(404, "key not found")
    return summary


@router.patch("/keys/{key_id}/toggle")
def toggle_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    try:
        updated = keys.toggle_key(key_id)
    except keys.LastMasterKeyError as exc:
        # 409 (conflict) carries the recovery hint so the dashboard /
        # CLI can show it verbatim without inventing its own copy.
        raise HTTPException(409, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "key not found")
    return updated


@router.delete("/keys/{key_id}")
def delete_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    try:
        ok = keys.delete_key(key_id)
    except keys.LastMasterKeyError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "key not found")
    return {"deleted": True, "key_id": key_id}


@router.post("/keys/{key_id}/permissions", status_code=201)
def grant_permission(
    req: Request,
    key_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    try:
        return keys.grant_permission(
            key_id=key_id,
            resource_type=str(payload.get("resource_type", "*")),
            resource_id=str(payload.get("resource_id", "*")),
            permission=str(payload.get("permission", "invoke")),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/keys/permissions/{perm_id}")
def revoke_permission(req: Request, perm_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    ok = keys.revoke_permission(perm_id)
    if not ok:
        raise HTTPException(404, "permission not found")
    return {"deleted": True, "perm_id": perm_id}


# ── Config ─────────────────────────────────────────────────────


@router.get("/config")
def get_config(req: Request) -> dict[str, Any]:
    _require_trust(req)
    return store.load_config()


@router.patch("/config")
def patch_config(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    cfg = store.update_config(payload)
    if "sweeper_enabled" in payload:
        if cfg.get("sweeper_enabled"):
            sweeper.start()
        else:
            sweeper.stop()
    return cfg


# ── Bootstrap master key (no auth — first time only) ────────────


@router.post("/keys/bootstrap-master", status_code=201)
def bootstrap_master(req: Request) -> dict[str, Any]:
    """One-shot master-key bootstrap.

    Hardened: only the immediate peer's address counts — `X-Forwarded-For`
    is rejected on this route regardless of `KE_TRUST_PROXY` so a
    misconfigured proxy can't escalate. Peer must be loopback (not
    Tailscale) — bootstrap on the box, not the mesh.
    """
    if req.headers.get("X-Forwarded-For"):
        raise HTTPException(403, "bootstrap-master refuses X-Forwarded-For")
    peer = _peer_address(req)
    if peer is None or peer not in (_LOCALHOST_V4, _LOCALHOST_V6):
        raise HTTPException(403, "bootstrap-master is loopback-only")
    key = keys.ensure_master_key()
    if key is None:
        raise HTTPException(409, "master key already exists")
    return key
