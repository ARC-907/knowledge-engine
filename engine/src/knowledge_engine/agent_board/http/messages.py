"""Board HTTP: READ routes and the WRITE routes (post, ack).

`scope` (query or body field) routes a request to a per-project /
per-branch / per-agent physical database. Omit it (the default) and the
request hits the shared board DB -- fully backward compatible. See
`agent_board/scopes.py` and `docs/Reference/AGENT-BOARD.md` (Scoped databases).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ConfigDict, Field, model_validator

from ...core.errors import payload_invalid
from .. import schemas, scopes, store
from .bodies import _SCOPE_DOC, _BoardBody, _message_field, _pop_scope, _required, _string
from .trust import (
    WRITE_GATE,
    _allowed_networks,
    _allowed_proxy_networks,
    _req_settings,
    _req_sweeper,
    _require_trust,
    _trust_proxy_enabled,
)

router = APIRouter()


class MessageIn(_BoardBody):
    """``POST /board/messages`` body (see ``agent_board.schemas.MESSAGE_FIELDS``)."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        json_schema_extra=_required(*(f.name for f in schemas.MESSAGE_FIELDS if f.required)),
    )

    channel: Annotated[Any, _message_field("channel")] = None
    message_type: Annotated[Any, _message_field("message_type")] = None
    sender_node_id: Annotated[Any, _message_field("sender_node_id")] = None
    body: Annotated[Any, _message_field("body")] = None
    subject: Annotated[Any, _message_field("subject")] = None
    visibility_scope: Annotated[Any, _message_field("visibility_scope")] = None
    sender_role: Annotated[Any, _message_field("sender_role")] = None
    task_id: Annotated[Any, _message_field("task_id")] = None
    product_id: Annotated[Any, _message_field("product_id")] = None
    target_node_id: Annotated[Any, _message_field("target_node_id")] = None
    target_role: Annotated[Any, _message_field("target_role")] = None
    requires_ack: Annotated[Any, _message_field("requires_ack")] = None
    reply_to: Annotated[Any, _message_field("reply_to")] = None
    correlation_id: Annotated[Any, _message_field("correlation_id")] = None
    thread_id: Annotated[Any, _message_field("thread_id")] = None
    ttl_hours: Annotated[Any, _message_field("ttl_hours")] = None
    model_id: Annotated[Any, _message_field("model_id")] = None
    scope: Annotated[Any, _SCOPE_DOC] = None

    @model_validator(mode="after")
    def _validate_with_schema(self) -> MessageIn:
        # Same rules as the store (single source of truth). Channel membership
        # depends on the target scope's configured channels, so the store
        # checks it when the message is written.
        _draft, errors = schemas.validate(self.payload(), check_channel=False)
        if errors:
            raise payload_invalid("; ".join(errors))
        return self


class AckIn(_BoardBody):
    """``POST /board/messages/{id}/ack`` body. ``from`` (or ``acker``) is required."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    from_: Annotated[Any, _string("Identity of the acknowledging agent.")] = Field(
        None, alias="from"
    )
    acker: Annotated[Any, _string("Alias of `from`.")] = None
    scope: Annotated[Any, _SCOPE_DOC] = None


# ── Status / metadata ──────────────────────────────────────────


@router.get("/status")
def status(req: Request) -> dict[str, Any]:
    settings = _req_settings(req)
    _require_trust(req, settings)
    cfg = store.load_config()
    return {
        "service": "knowledge-engine-board",
        "messages_total": store.total_count(),
        "channel_stats": store.channel_stats(),
        "last_sweep": store.last_sweep(),
        "sweeper_running": _req_sweeper(req).is_running(),
        "trusted_networks": [str(n) for n in _allowed_networks(settings)],
        "trust_proxy": _trust_proxy_enabled(settings),
        "trusted_proxies": [str(n) for n in _allowed_proxy_networks(settings)],
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


@router.post("/messages", status_code=201, dependencies=WRITE_GATE)
def post_message(
    body: MessageIn,
    scope: str | None = Query(None),
) -> dict[str, Any]:
    payload = body.payload()
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


@router.post("/messages/{message_id}/ack", status_code=200, dependencies=WRITE_GATE)
def ack_message(
    message_id: str,
    body: AckIn,
    scope: str | None = Query(None),
) -> dict[str, Any]:
    payload = body.payload()
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


# ── Config (read) ───────────────────────────────────────────────


@router.get("/config")
def get_config(req: Request, scope: str | None = Query(None)) -> dict[str, Any]:
    _require_trust(req)
    return _scoped_read(store.load_config, scope=scope)


__all__ = ["AckIn", "MessageIn", "router"]
