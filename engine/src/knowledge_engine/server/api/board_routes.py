"""FastAPI routes for the Agent Board.

Mounted at `/board/*` by `app.create_app()` when `KE_BOARD_ENABLED` != `0`.

Authorization model — three route classes:

READ
    Everything not listed below. Open to any *trusted peer* (see the gate
    below); untrusted peers always get 403.
WRITE
    `POST /board/messages`, `POST /board/messages/{id}/ack`. Trusted peers
    post keylessly while `require_key_for_post=0` (the default). With the
    flag on, non-loopback peers must present an `X-Board-Key` holding
    `board:write`; loopback peers stay keyless unless they present a key,
    in which case it must be valid.
ADMIN
    The key vault (`/board/keys*`), provider-registry mutations and
    listing, `PATCH /board/config`, `POST /board/sweep`, and scope creation
    (`PUT /board/scopes/{name}`). These ALWAYS require a valid key with
    `board:admin` (a master key, or a key granted `admin`), independent of
    `require_key_for_post` and of where the request comes from.

    The one exception is `POST /board/keys/bootstrap-master`: loopback-only
    by the real socket peer (`X-Forwarded-For` is refused outright), and
    only while no enabled master key exists — afterwards 409.

Peer-trust gate:
    Loopback (127.0.0.1, ::1) and the Tailscale CGNAT range (100.64.0.0/10)
    are trusted peers by default. Extend / restrict the set with
    `KE_BOARD_TRUSTED_CIDRS` (comma-separated CIDRs; the empty string drops
    Tailscale trust → loopback only).

X-Forwarded-For is **opt-in** via `KE_TRUST_PROXY=1` and only honoured when
the socket peer is itself a trusted proxy (`KE_TRUSTED_PROXIES`, default
loopback). The chain is walked from the right: every address that is a
trusted proxy is skipped and the first one that is not is the client. With
the flag off the header is ignored entirely — provided the ASGI server does
not rewrite the peer first. `knowledge-engine serve` / `board-serve` pass
``core.http.uvicorn_proxy_kwargs()`` to uvicorn so its own proxy-headers
middleware follows the same two env vars; an operator embedding the app in
another server must disable (or align) that server's proxy handling.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response

from ...agent_board import keys, provider_registry as providers, schemas, scopes, store, sweeper
from ...core.http import (
    LOOPBACK_NETS as _LOOPBACK_NETS,
    IPAddress,
    IPNetwork,
    parse_cidrs as _parse_cidrs,
    trust_proxy_enabled as _trust_proxy_enabled,
    trusted_proxies as _trusted_proxies,
)

_logger = logging.getLogger(__name__)

router = APIRouter()


def _pop_scope(payload: dict[str, Any]) -> str | None:
    """Pull an optional ``scope`` out of a POST body so it routes the write
    to a per-scope DB without leaking into the message payload itself.
    """
    val = payload.pop("scope", None)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


# ── Trust gate ─────────────────────────────────────────────────


_LOCALHOST_V4 = ipaddress.ip_address("127.0.0.1")
_LOCALHOST_V6 = ipaddress.ip_address("::1")
# Tailscale CGNAT range — every Tailscale-issued IPv4 lives here. Treating
# the whole /10 as trusted means peers on the operator's private mesh
# don't need to round-trip through key creation just to coordinate.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

TRUSTED_CIDRS_ENV = "KE_BOARD_TRUSTED_CIDRS"

# `_parse_cidrs`, `_trust_proxy_enabled`, `_trusted_proxies`, the two proxy
# env names and `_LOOPBACK_NETS` live in ``core.http`` so the uvicorn entry
# points can derive their proxy settings from the very same policy; they are
# re-exported here under their historical names.


def _trusted_networks() -> list[IPNetwork]:
    """The active trusted-CIDR list.

    Defaults to loopback (v4 + v6) + Tailscale CGNAT. The
    `KE_BOARD_TRUSTED_CIDRS` env var REPLACES the default — set it to e.g.
    `127.0.0.1/32,::1/128` to disable Tailscale trust, or add extra
    private-mesh ranges if you operate outside Tailscale.
    """
    raw = os.environ.get(TRUSTED_CIDRS_ENV)
    if raw is not None:
        return _parse_cidrs(raw)
    return [
        ipaddress.ip_network("127.0.0.1/32"),
        ipaddress.ip_network("::1/128"),
        _TAILSCALE_NET,
    ]


def _parse_ip(text: str) -> IPAddress | None:
    text = text.strip()
    if text.startswith("::ffff:"):
        text = text[7:]
    try:
        return ipaddress.ip_address(text) if text else None
    except (ValueError, TypeError):
        return None


def _in_any(addr: IPAddress, nets: Sequence[IPNetwork]) -> bool:
    return any(addr in net for net in nets)


def _is_loopback(addr: IPAddress | None) -> bool:
    return addr is not None and _in_any(addr, _LOOPBACK_NETS)


def _peer_address(req: Request) -> IPAddress | None:
    """The peer as the ASGI server reports it (``scope["client"]``).

    This function itself never consults `X-Forwarded-For`. Whether the value
    is the raw socket peer depends on the server: uvicorn's proxy-headers
    middleware would rewrite it, which is why both ``serve`` entry points
    configure uvicorn from ``core.http.uvicorn_proxy_kwargs()`` so that
    rewriting happens only under the same `KE_TRUST_PROXY` policy as
    :func:`_client_address`.

    Returns None when the server reports no usable address (e.g. a test
    transport that did not set ``scope["client"]``); such requests are
    untrusted.
    """
    try:
        host = req.client.host if req.client else ""
    except AttributeError:
        return None
    return _parse_ip(str(host or ""))


def _forwarded_chain(req: Request) -> list[str]:
    raw = req.headers.get("X-Forwarded-For", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _client_address(req: Request) -> IPAddress | None:
    """The address the trust gate judges.

    Without `KE_TRUST_PROXY` this is the socket peer. With it, and only when
    the socket peer is a trusted proxy, the `X-Forwarded-For` chain is walked
    from the right: trusted proxies are skipped and the first remaining
    address is the client. A chain made only of trusted proxies resolves to
    its leftmost entry (the proxy's own view of the client). A malformed
    entry makes the request untrusted.
    """
    peer = _peer_address(req)
    if peer is None or not _trust_proxy_enabled():
        return peer
    proxies = _trusted_proxies()
    if not _in_any(peer, proxies):
        return peer
    chain = _forwarded_chain(req)
    if not chain:
        return peer
    parsed: list[IPAddress] = []
    for hop in chain:
        addr = _parse_ip(hop)
        if addr is None:
            _logger.warning("malformed X-Forwarded-For entry %r from proxy %s", hop, peer)
            return None
        parsed.append(addr)
    for addr in reversed(parsed):
        if not _in_any(addr, proxies):
            return addr
    return parsed[0]


def _is_trusted_peer(req: Request) -> bool:
    """Return True if the effective client is on a trusted network."""
    client = _client_address(req)
    if client is None:
        return False
    nets = _trusted_networks()
    if not nets:
        return False
    return _in_any(client, nets)


# Backwards-compatible alias for any external callers that imported the
# old name. Internally everyone now uses `_is_trusted_peer`.
def _is_localhost(req: Request) -> bool:
    return _is_trusted_peer(req)


def _require_trust(req: Request) -> None:
    """Hard gate: reject any peer not on a trusted network."""
    if not _is_trusted_peer(req):
        _logger.warning(
            "board: rejected %s %s from untrusted peer %s",
            req.method,
            req.url.path,
            _peer_address(req),
        )
        raise HTTPException(403, "peer not on a trusted network")


# Hard ceiling on a single board request body. The per-field caps in
# schemas.validate enforce semantic limits; this is the brute-force
# defence against an attacker streaming megabytes at the FastAPI body
# parser before validation has a chance to fire. Enforced for streamed
# bodies by ``core.http.BodySizeLimitMiddleware`` (installed by both app
# factories); the `Content-Length` check below is the in-route fast path.
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


def _presented_key(req: Request) -> str | None:
    raw = req.headers.get("X-Board-Key", "").strip()
    return raw or None


def _require_write(req: Request, resource_id: str = "*") -> None:
    """WRITE gate: trust, body cap, then the `require_key_for_post` decision.

    See :func:`keys.authorize_write` for the exact rule; it is shared with
    the MCP post tools so both surfaces agree.
    """
    _require_trust(req)
    _check_body_size(req)
    cfg = store.load_config()
    decision = keys.authorize_write(
        _presented_key(req),
        loopback=_is_loopback(_client_address(req)),
        require_key=bool(cfg.get("require_key_for_post")),
        resource_id=resource_id,
    )
    if not decision.ok:
        raise HTTPException(decision.status, decision.detail)


def _require_admin(req: Request) -> str:
    """ADMIN gate: trust, body cap, then a valid key holding `board:admin`.

    Loopback never bypasses this gate and `require_key_for_post` has no
    bearing on it. Returns the authenticated key id for logging.
    """
    _require_trust(req)
    _check_body_size(req)
    decision = keys.authorize_key(_presented_key(req), "admin")
    if not decision.ok:
        _logger.warning(
            "board: admin route %s %s denied (%s) for peer %s",
            req.method,
            req.url.path,
            decision.status,
            _peer_address(req),
        )
        raise HTTPException(decision.status, decision.detail)
    return decision.key_id or ""


def _require_key(req: Request, resource_id: str = "*", permission: str = "write") -> None:
    """Compatibility wrapper: dispatch to the WRITE or ADMIN gate."""
    if permission == "admin":
        _require_admin(req)
    else:
        _require_write(req, resource_id=resource_id)


def trust_policy() -> dict[str, Any]:
    """The effective trust configuration, for the status route and boot log."""
    try:
        require_key = bool(store.load_config().get("require_key_for_post"))
    except Exception:
        _logger.exception("could not read board config for trust policy")
        require_key = False
    return {
        "trusted_cidrs": [str(n) for n in _trusted_networks()],
        "require_key_for_post": require_key,
        "trust_proxy": _trust_proxy_enabled(),
        "trusted_proxies": [str(n) for n in _trusted_proxies()],
        "max_scopes": scopes.max_scopes(),
        "max_request_body_bytes": MAX_REQUEST_BODY_BYTES,
    }


def log_trust_policy() -> None:
    """Log the effective trust policy once at startup."""
    policy = trust_policy()
    _logger.info(
        "board trust: trusted_cidrs=%s require_key_for_post=%s trust_proxy=%s "
        "trusted_proxies=%s max_scopes=%s admin_routes=key-required",
        policy["trusted_cidrs"],
        policy["require_key_for_post"],
        policy["trust_proxy"],
        policy["trusted_proxies"],
        policy["max_scopes"],
    )


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
        "trusted_proxies": [str(n) for n in _trusted_proxies()],
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


# `scope` (query or body field) routes a request to a per-project /
# per-branch / per-agent physical database. Omit it (the default) and the
# request hits the shared board DB — fully backward compatible. See
# `agent_board/scopes.py` and `docs/Reference/AGENT-BOARD.md` (Scoped databases).


def _scoped_read[R](fn: store._Scoped[R], *args: Any, scope: str | None, **kwargs: Any) -> R:
    """Call a store read with ``scope``; a malformed scope name is a 400."""
    try:
        return fn(*args, scope=scope, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    scope: str | None = Query(None),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return _scoped_read(
        store.poll,
        since=since,
        channel=channel,
        message_type=message_type,
        task_id=task_id,
        product_id=product_id,
        sender_node_id=sender_node_id,
        limit=limit,
        scope=scope,
    )


@router.get("/messages/{message_id}")
def get_message(req: Request, message_id: str, scope: str | None = Query(None)) -> dict[str, Any]:
    _require_trust(req)
    msg = _scoped_read(store.read, message_id, scope=scope)
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@router.post("/messages", status_code=201)
def post_message(
    req: Request,
    payload: Annotated[dict[str, Any], Body()],
    scope: str | None = Query(None),
) -> dict[str, Any]:
    _require_write(req)
    # Accept scope from the body OR the query string — body wins if both
    # are present. A dev shouldn't have to remember which one the route wants.
    scope = _pop_scope(payload) or scope
    try:
        msg = store.post_with_validation(payload, scope=scope)
    except scopes.ScopeLimitError as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return msg


@router.post("/messages/{message_id}/ack", status_code=200)
def ack_message(
    req: Request,
    message_id: str,
    payload: Annotated[dict[str, Any], Body()],
    scope: str | None = Query(None),
) -> dict[str, Any]:
    _require_write(req)
    scope = _pop_scope(payload) or scope
    acker = str(payload.get("from") or payload.get("acker") or "").strip()
    if not acker:
        raise HTTPException(400, "'from' (acker identity) is required")
    msg = _scoped_read(store.ack_message, message_id, acker, scope=scope)
    if msg is None:
        raise HTTPException(404, "message not found")
    return msg


# ── Threads / search / digest ──────────────────────────────────


@router.get("/threads/{correlation_id}")
def get_thread(
    req: Request,
    correlation_id: str,
    limit: int = Query(100, ge=1, le=500),
    scope: str | None = Query(None),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return _scoped_read(
        store.thread_messages, correlation_id=correlation_id, limit=limit, scope=scope
    )


@router.get("/search")
def search(
    req: Request,
    q: str = Query(..., min_length=1),
    channel: str | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
    scope: str | None = Query(None),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return _scoped_read(store.search_messages, query=q, channel=channel, limit=limit, scope=scope)


@router.get("/digest")
def digest(
    req: Request,
    channel: str | None = Query(None),
    since: str | None = Query(None),
    max_messages: int = Query(200, ge=10, le=2000),
    scope: str | None = Query(None),
) -> dict[str, Any]:
    _require_trust(req)
    return _scoped_read(
        store.digest, channel=channel, since=since, max_messages=max_messages, scope=scope
    )


# ── Stats ──────────────────────────────────────────────────────


@router.get("/stats/channels")
def stats_channels(req: Request, scope: str | None = Query(None)) -> list[dict[str, Any]]:
    _require_trust(req)
    return _scoped_read(store.channel_stats, scope=scope)


@router.get("/stats/types")
def stats_types(
    req: Request,
    channel: str | None = Query(None),
    scope: str | None = Query(None),
) -> list[dict[str, Any]]:
    _require_trust(req)
    return _scoped_read(store.type_stats, channel=channel, scope=scope)


# ── Scopes ─────────────────────────────────────────────────────


@router.get("/scopes")
def list_scopes(req: Request) -> dict[str, Any]:
    """List known per-scope databases (project/branch/agent/loop)."""
    _require_trust(req)
    return {
        "scopes": scopes.list_scopes(),
        "root": str(scopes.scopes_root()),
        "max_scopes": scopes.max_scopes(),
    }


@router.put("/scopes/{name}")
def create_scope(req: Request, name: str, response: Response) -> dict[str, Any]:
    """Explicitly create a per-scope database (ADMIN). Idempotent: 200 when
    the scope already exists, 201 when it was created, 400 on a bad name,
    507 at the `KE_BOARD_MAX_SCOPES` cap."""
    key_id = _require_admin(req)
    try:
        result = scopes.create_scope(name)
    except scopes.ScopeLimitError as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result["created"]:
        response.status_code = 201
        _logger.info("board scope %r created via API by key_id=%s", name, key_id)
    return result


# ── Sweeper ────────────────────────────────────────────────────


@router.post("/sweep")
def sweep_now(req: Request) -> dict[str, Any]:
    key_id = _require_admin(req)
    _logger.info("board: manual sweep requested by key_id=%s", key_id)
    return sweeper.sweep_once(force=True)


# ── Keys ───────────────────────────────────────────────────────


@router.get("/keys")
def list_keys(req: Request) -> list[dict[str, Any]]:
    _require_admin(req)
    return keys.list_keys(include_master=False)


@router.post("/keys", status_code=201)
def create_key(req: Request, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    _require_admin(req)
    name = str(payload.get("display_name") or "").strip()
    if not name:
        raise HTTPException(400, "display_name is required")
    if len(name) > 200:
        raise HTTPException(400, "display_name too long (max 200 chars)")
    notes = payload.get("notes")
    expires_at = payload.get("expires_at")
    key = keys.create_key(
        display_name=name,
        notes=notes,
        expires_at=expires_at,
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
    _require_admin(req)
    summary = keys.get_key_summary(key_id)
    if summary is None:
        raise HTTPException(404, "key not found")
    return summary


@router.patch("/keys/{key_id}/toggle")
def toggle_key(req: Request, key_id: str) -> dict[str, Any]:
    _require_admin(req)
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
    _require_admin(req)
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
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    _require_admin(req)
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
    _require_admin(req)
    ok = keys.revoke_permission(perm_id)
    if not ok:
        raise HTTPException(404, "permission not found")
    return {"deleted": True, "perm_id": perm_id}


# ── Provider credentials (env-var bindings; no secrets stored) ──
# Distinct from /board/keys (board-ACCESS tokens). These bind a provider
# to the env var that holds its secret; the engine resolves the live value
# from the environment at call time. The DB never holds a credential.


@router.get("/providers")
def list_providers(req: Request) -> dict[str, Any]:
    _require_admin(req)
    return {
        "providers": providers.list_providers(),
        "known": list(providers.KNOWN_PROVIDERS),
        "default_env_vars": providers.DEFAULT_ENV_VARS,
    }


@router.post("/providers", status_code=201)
def register_provider(req: Request, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    _require_admin(req)
    provider = str(payload.get("provider") or "").strip()
    if not provider:
        raise HTTPException(400, "provider is required")
    try:
        return providers.register_provider(
            provider=provider,
            env_var=payload.get("env_var"),
            display_name=payload.get("display_name"),
            notes=payload.get("notes"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/providers/{key_id}/toggle")
def toggle_provider(req: Request, key_id: str) -> dict[str, Any]:
    _require_admin(req)
    updated = providers.toggle_provider(key_id)
    if updated is None:
        raise HTTPException(404, "provider binding not found")
    return updated


@router.post("/providers/{key_id}/verify")
def verify_provider(req: Request, key_id: str) -> dict[str, Any]:
    _require_admin(req)
    checked = providers.verify_provider(key_id)
    if checked is None:
        raise HTTPException(404, "provider binding not found")
    return checked


@router.delete("/providers/{key_id}")
def delete_provider(req: Request, key_id: str) -> dict[str, Any]:
    _require_admin(req)
    ok = providers.delete_provider(key_id)
    if not ok:
        raise HTTPException(404, "provider binding not found")
    return {"deleted": True, "key_id": key_id}


# ── Config ─────────────────────────────────────────────────────


@router.get("/config")
def get_config(req: Request, scope: str | None = Query(None)) -> dict[str, Any]:
    _require_trust(req)
    return _scoped_read(store.load_config, scope=scope)


@router.patch("/config")
def patch_config(
    req: Request,
    payload: Annotated[dict[str, Any], Body()],
    scope: str | None = Query(None),
) -> dict[str, Any]:
    key_id = _require_admin(req)
    scope = _pop_scope(payload) or scope
    try:
        cfg = store.update_config(payload, scope=scope)
    except scopes.ScopeLimitError as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _logger.info(
        "board config updated by key_id=%s scope=%s fields=%s", key_id, scope, sorted(payload)
    )
    # The sweeper is process-wide (it sweeps the default DB + every scope),
    # so its lifecycle is governed by the default board's config only. A
    # per-scope toggle changes that scope's stored flag without starting or
    # stopping the shared sweeper thread.
    if "sweeper_enabled" in payload and scope is None:
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
    peer = _peer_address(req)
    _logger.warning("board: bootstrap-master called from peer %s", peer)
    if req.headers.get("X-Forwarded-For"):
        _logger.warning("board: bootstrap-master refused — X-Forwarded-For present")
        raise HTTPException(403, "bootstrap-master refuses X-Forwarded-For")
    if not _is_loopback(peer):
        _logger.warning("board: bootstrap-master refused — peer %s is not loopback", peer)
        raise HTTPException(403, "bootstrap-master is loopback-only")
    if keys.has_master_key():
        raise HTTPException(409, "master key already exists")
    key = keys.ensure_master_key()
    if key is None:
        raise HTTPException(409, "master key already exists")
    return key
