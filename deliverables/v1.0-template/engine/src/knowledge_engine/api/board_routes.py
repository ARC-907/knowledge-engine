"""FastAPI routes for the Agent Board.

Mounted at `/board/*` by `app.create_app()` when `KE_BOARD_ENABLED` != `0`.
Local-trust by default (CORS open, no auth) per the existing engine convention;
flip `board_config.require_key_for_post=1` to gate non-localhost writes.

The full route inventory is documented in `ARCHITECTURE.md`. Each handler is
deliberately thin — the heavy lifting is in `agent_board.store`.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ..agent_board import keys as kb_keys
from ..agent_board import schemas as kb_schemas
from ..agent_board import store as kb_store
from ..agent_board import sweeper as kb_sweeper


router = APIRouter()


# ── Trust gate ─────────────────────────────────────────────────


_LOCALHOST_V4 = ipaddress.ip_address("127.0.0.1")
_LOCALHOST_V6 = ipaddress.ip_address("::1")


def _is_localhost(req: Request) -> bool:
    """Return True if the request originates from localhost.

    Used as a soft gate — config-tab key requirement is only enforced for
    non-localhost POSTs. Honors `X-Forwarded-For` only if the immediate peer
    is itself localhost (i.e. a local reverse proxy).
    """
    try:
        client = req.client.host if req.client else ""
        if client.startswith("::ffff:"):
            client = client[7:]
        peer = ipaddress.ip_address(client) if client else None
    except (ValueError, TypeError):
        return False
    if peer is None:
        return False
    if peer in (_LOCALHOST_V4, _LOCALHOST_V6):
        forwarded = req.headers.get("X-Forwarded-For")
        if forwarded:
            try:
                inner = ipaddress.ip_address(forwarded.split(",", 1)[0].strip())
                return inner in (_LOCALHOST_V4, _LOCALHOST_V6)
            except (ValueError, TypeError):
                return False
        return True
    return False


def _require_key(req: Request, resource_id: str = "*", permission: str = "write") -> None:
    """If a key is required for posting and the caller is non-localhost,
    verify the `X-Board-Key` header. Raises 401 / 403 on failure.
    """
    cfg = kb_store.load_config()
    if not cfg.get("require_key_for_post"):
        return
    if _is_localhost(req):
        return
    raw = req.headers.get("X-Board-Key", "").strip()
    if not raw:
        raise HTTPException(401, "Missing X-Board-Key header")
    record = kb_keys.verify_key(raw)
    if record is None:
        raise HTTPException(401, "Invalid or expired key")
    if not kb_keys.check_permission(record["key_id"], "board", resource_id, permission):
        raise HTTPException(403, "Key lacks board:write permission")


# ── Status / metadata ──────────────────────────────────────────


@router.get("/status")
def status() -> dict[str, Any]:
    cfg = kb_store.load_config()
    return {
        "service": "knowledge-engine-board",
        "messages_total": kb_store.total_count(),
        "channel_stats": kb_store.channel_stats(),
        "last_sweep": kb_store.last_sweep(),
        "sweeper_running": kb_sweeper.is_running(),
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
def list_channels() -> dict[str, Any]:
    cfg = kb_store.load_config()
    return {"channels": cfg["channels"], "defaults": list(kb_schemas.DEFAULT_CHANNELS)}


@router.get("/message_types")
def list_message_types() -> dict[str, Any]:
    return {
        "message_types": list(kb_schemas.MESSAGE_TYPES),
        "visibility_scopes": list(kb_schemas.VISIBILITY_SCOPES),
    }


# ── Messages ───────────────────────────────────────────────────


@router.get("/messages")
def list_messages(
    since: str | None = Query(None),
    channel: str | None = Query(None),
    message_type: str | None = Query(None),
    task_id: str | None = Query(None),
    product_id: str | None = Query(None),
    sender_node_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    return kb_store.poll(
        since=since, channel=channel, message_type=message_type,
        task_id=task_id, product_id=product_id, sender_node_id=sender_node_id,
        limit=limit,
    )


@router.get("/messages/{message_id}")
def get_message(message_id: str) -> dict[str, Any]:
    msg = kb_store.read(message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@router.post("/messages", status_code=201)
def post_message(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="write")
    try:
        msg = kb_store.post_with_validation(payload)
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
    msg = kb_store.ack_message(message_id, acker)
    if msg is None:
        raise HTTPException(404, "message not found")
    return msg


# ── Threads / search / digest ──────────────────────────────────


@router.get("/threads/{correlation_id}")
def get_thread(correlation_id: str, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return kb_store.thread_messages(correlation_id=correlation_id, limit=limit)


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    channel: str | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
) -> list[dict[str, Any]]:
    return kb_store.search_messages(query=q, channel=channel, limit=limit)


@router.get("/digest")
def digest(
    channel: str | None = Query(None),
    since: str | None = Query(None),
    max_messages: int = Query(200, ge=10, le=2000),
) -> dict[str, Any]:
    return kb_store.digest(channel=channel, since=since, max_messages=max_messages)


# ── Stats ──────────────────────────────────────────────────────


@router.get("/stats/channels")
def stats_channels() -> list[dict[str, Any]]:
    return kb_store.channel_stats()


@router.get("/stats/types")
def stats_types(channel: str | None = Query(None)) -> list[dict[str, Any]]:
    return kb_store.type_stats(channel=channel)


# ── Sweeper ────────────────────────────────────────────────────


@router.post("/sweep")
def sweep_now(req: Request) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    return kb_sweeper.sweep_once()


# ── Keys ───────────────────────────────────────────────────────


@router.get("/keys")
def list_keys(req: Request) -> list[dict[str, Any]]:
    # Reading the key list is admin-only when key gating is enabled.
    _require_key(req, resource_id="*", permission="admin")
    return kb_keys.list_keys(include_master=False)


@router.post("/keys", status_code=201)
def create_key(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    name = str(payload.get("display_name") or "").strip()
    if not name:
        raise HTTPException(400, "display_name is required")
    notes = payload.get("notes")
    expires_at = payload.get("expires_at")
    key = kb_keys.create_key(
        display_name=name, notes=notes, expires_at=expires_at,
    )
    # Optional initial permission grant in the same payload.
    permissions = payload.get("permissions") or []
    for grant in permissions:
        try:
            kb_keys.grant_permission(
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
    summary = kb_keys.get_key_summary(key_id)
    if summary is None:
        raise HTTPException(404, "key not found")
    return summary


@router.patch("/keys/{key_id}/toggle")
def toggle_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    updated = kb_keys.toggle_key(key_id)
    if updated is None:
        raise HTTPException(404, "key not found")
    return updated


@router.delete("/keys/{key_id}")
def delete_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    ok = kb_keys.delete_key(key_id)
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
        return kb_keys.grant_permission(
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
    ok = kb_keys.revoke_permission(perm_id)
    if not ok:
        raise HTTPException(404, "permission not found")
    return {"deleted": True, "perm_id": perm_id}


# ── Config ─────────────────────────────────────────────────────


@router.get("/config")
def get_config() -> dict[str, Any]:
    return kb_store.load_config()


@router.patch("/config")
def patch_config(req: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_key(req, resource_id="*", permission="admin")
    cfg = kb_store.update_config(payload)
    # Apply runtime side-effects immediately so the operator sees changes.
    if "sweeper_enabled" in payload:
        if cfg.get("sweeper_enabled"):
            kb_sweeper.start()
        else:
            kb_sweeper.stop()
    return cfg


# ── Bootstrap master key (no auth — first time only) ────────────


@router.post("/keys/bootstrap-master", status_code=201)
def bootstrap_master(req: Request) -> dict[str, Any]:
    """One-shot master-key bootstrap.

    Localhost-only. If a master key already exists, returns 409. The raw key
    is also written to `<data_dir>/board-master-key.txt` for retrieval.
    """
    if not _is_localhost(req):
        raise HTTPException(403, "bootstrap-master is localhost-only")
    key = kb_keys.ensure_master_key()
    if key is None:
        raise HTTPException(409, "master key already exists")
    return key
