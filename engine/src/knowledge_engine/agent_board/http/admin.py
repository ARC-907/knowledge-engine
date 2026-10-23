"""Board HTTP: ADMIN routes (key vault, providers, config, sweep, scopes, bootstrap).

Every route here requires a valid key holding ``board:admin`` (see
:func:`.trust.admin_gate`), except the loopback-only, one-shot
``POST /board/keys/bootstrap-master``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ConfigDict

from ...core.logsafe import log_safe
from .. import keys, provider_registry as providers, scopes, store
from .bodies import _SCOPE_DOC, _BoardBody, _documented, _int, _pop_scope, _required, _string
from .trust import ADMIN_GATE, _is_loopback, _peer_address, _req_sweeper, _require_admin, admin_gate

_logger = logging.getLogger(__name__)

router = APIRouter()


class PermissionGrantIn(_BoardBody):
    """A permission grant on a board key."""

    resource_type: Annotated[Any, _string("Resource type (e.g. `board`).", default="*")] = None
    resource_id: Annotated[Any, _string("Resource id or `*`.", default="*")] = None
    permission: Annotated[
        Any, _string("Permission (e.g. `invoke`, `write`, `admin`).", default="invoke")
    ] = None


class KeyCreateIn(_BoardBody):
    """``POST /board/keys`` body. ``display_name`` is required (max 200 chars)."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, json_schema_extra=_required("display_name")
    )

    display_name: Annotated[Any, _string("Human-readable key name.", maxLength=200)] = None
    notes: Annotated[Any, _string("Free-text notes.")] = None
    expires_at: Annotated[Any, _string("ISO-8601 expiry timestamp.")] = None
    permissions: Annotated[
        Any,
        _documented(
            {
                "type": "array",
                "description": "Grants to attach to the new key.",
                "items": {
                    "type": "object",
                    "properties": {
                        "resource_type": {"type": "string", "default": "*"},
                        "resource_id": {"type": "string", "default": "*"},
                        "permission": {"type": "string", "default": "invoke"},
                    },
                },
            }
        ),
    ] = None


class ProviderBindingIn(_BoardBody):
    """``POST /board/providers`` body: bind a provider to the env var holding its secret."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, json_schema_extra=_required("provider")
    )

    provider: Annotated[
        Any, _string("Provider name.", examples=list(providers.KNOWN_PROVIDERS))
    ] = None
    env_var: Annotated[
        Any, _string("Environment variable that holds the secret (default per provider).")
    ] = None
    display_name: Annotated[Any, _string("Human-readable name.")] = None
    notes: Annotated[Any, _string("Free-text notes.")] = None


#: ``PATCH /board/config`` fields; the store ignores any other key.
CONFIG_FIELDS: dict[str, str] = {
    "engine_port": "Engine HTTP port shown to clients.",
    "standalone_port": "Standalone board service port shown to clients.",
    "sweep_interval_s": "Seconds between sweeper passes.",
    "stale_blocker_hours": "Hours before an unacked blocker gets a reminder.",
    "digest_interval_minutes": "Minutes between per-channel digests (0 = off).",
    "max_messages_before_prune": "Message count that triggers overflow pruning.",
    "default_ttl_hours": "Default message TTL in hours.",
    "sweeper_enabled": "1 to run the sweeper, 0 to stop it.",
    "require_key_for_post": "1 to require X-Board-Key for non-loopback writes.",
    "channels": "Full list of configured channels.",
}


class ConfigPatchIn(_BoardBody):
    """``PATCH /board/config`` body: any subset of the board config fields."""

    engine_port: Annotated[Any, _int(CONFIG_FIELDS["engine_port"])] = None
    standalone_port: Annotated[Any, _int(CONFIG_FIELDS["standalone_port"])] = None
    sweep_interval_s: Annotated[Any, _int(CONFIG_FIELDS["sweep_interval_s"])] = None
    stale_blocker_hours: Annotated[Any, _int(CONFIG_FIELDS["stale_blocker_hours"])] = None
    digest_interval_minutes: Annotated[Any, _int(CONFIG_FIELDS["digest_interval_minutes"])] = None
    max_messages_before_prune: Annotated[Any, _int(CONFIG_FIELDS["max_messages_before_prune"])] = (
        None
    )
    default_ttl_hours: Annotated[Any, _int(CONFIG_FIELDS["default_ttl_hours"])] = None
    sweeper_enabled: Annotated[Any, _int(CONFIG_FIELDS["sweeper_enabled"])] = None
    require_key_for_post: Annotated[Any, _int(CONFIG_FIELDS["require_key_for_post"])] = None
    channels: Annotated[
        Any,
        _documented(
            {"type": "array", "items": {"type": "string"}, "description": CONFIG_FIELDS["channels"]}
        ),
    ] = None
    scope: Annotated[Any, _SCOPE_DOC] = None


# ── Scopes ─────────────────────────────────────────────────────


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
        _logger.info(
            "board scope %r created via API by key_id=%s", log_safe(name), log_safe(key_id)
        )
    return result


# ── Sweeper ────────────────────────────────────────────────────


@router.post("/sweep")
def sweep_now(req: Request) -> dict[str, Any]:
    key_id = _require_admin(req)
    _logger.info("board: manual sweep requested by key_id=%s", key_id)
    return _req_sweeper(req).sweep_once(force=True)


# ── Keys ───────────────────────────────────────────────────────


@router.get("/keys")
def list_keys(req: Request) -> list[dict[str, Any]]:
    _require_admin(req)
    return keys.list_keys(include_master=False)


@router.post("/keys", status_code=201, dependencies=ADMIN_GATE)
def create_key(body: KeyCreateIn) -> dict[str, Any]:
    payload = body.payload()
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
    if not isinstance(permissions, list):
        raise HTTPException(400, "permissions must be a list of grants")
    for grant in permissions:
        if not isinstance(grant, dict):
            raise HTTPException(400, "each permission grant must be an object")
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


@router.post("/keys/{key_id}/permissions", status_code=201, dependencies=ADMIN_GATE)
def grant_permission(
    key_id: str,
    body: PermissionGrantIn,
) -> dict[str, Any]:
    payload = body.payload()
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


@router.post("/providers", status_code=201, dependencies=ADMIN_GATE)
def register_provider(body: ProviderBindingIn) -> dict[str, Any]:
    payload = body.payload()
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


@router.patch("/config")
def patch_config(
    req: Request,
    body: ConfigPatchIn,
    key_id: Annotated[str, Depends(admin_gate)],
    scope: str | None = Query(None),
) -> dict[str, Any]:
    patch = body.payload()
    scope = _pop_scope(patch) or scope
    try:
        cfg = store.update_config(patch, scope=scope)
    except scopes.ScopeLimitError as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _logger.info(
        "board config updated by key_id=%s scope=%s fields=%s",
        log_safe(key_id),
        log_safe(scope),
        sorted(log_safe(field) for field in patch),
    )
    # The sweeper is process-wide (it sweeps the default DB + every scope),
    # so its lifecycle is governed by the default board's config only. A
    # per-scope toggle changes that scope's stored flag without starting or
    # stopping the shared sweeper thread.
    if "sweeper_enabled" in patch and scope is None:
        owned = _req_sweeper(req)
        if cfg.get("sweeper_enabled"):
            owned.start()
        else:
            owned.stop()
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


__all__ = [
    "CONFIG_FIELDS",
    "ConfigPatchIn",
    "KeyCreateIn",
    "PermissionGrantIn",
    "ProviderBindingIn",
    "router",
]
