"""HTTP surface of the Agent Board: the `/board` router, its trust gate and limits.

This module owns the board's FastAPI router. It is mounted at `/board/*` by
the engine app (``server.app.create_app``, unless `KE_BOARD_ENABLED=0`) and
by the standalone board service (``agent_board.service``) through
:func:`mount_board`. ``knowledge_engine.server.api.board_routes`` re-exports
it for compatibility.

Configuration comes from :class:`~knowledge_engine.core.settings.Settings`:
the app's own instance (``app.state.settings``) when the request has one,
otherwise the current environment. Request bodies are documented by the
Pydantic models below, derived from the field tables in
:mod:`agent_board.schemas`; :func:`schemas.validate` stays the single source
of truth for message rules and its errors keep answering ``400``.

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

On routes that take a body the WRITE / ADMIN gate runs as a FastAPI
dependency (:func:`write_gate`, :func:`admin_gate`), which FastAPI resolves
*before* it parses and validates the body. An untrusted or unauthenticated
peer therefore always gets its 403/401 and never reaches the body models or
their validation messages.

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

Package layout (the router is assembled here; behaviour is identical to the
former single module):

* :mod:`.trust`    -- peer trust, X-Forwarded-For, WRITE / ADMIN gates;
* :mod:`.limits`   -- request-body caps;
* :mod:`.bodies`   -- shared request-body plumbing;
* :mod:`.messages` -- READ routes plus the WRITE routes (post, ack);
* :mod:`.admin`    -- ADMIN routes (keys, providers, config, sweep, scopes).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, FastAPI

from ...core.settings import Settings
from .. import scopes, store
from . import admin, limits, messages
from .admin import CONFIG_FIELDS, ConfigPatchIn, KeyCreateIn, PermissionGrantIn, ProviderBindingIn
from .limits import MAX_REQUEST_BODY_BYTES, _check_body_size as _check_body_size
from .messages import AckIn, MessageIn, _scoped_read as _scoped_read

# Historical module attributes of the single-module ``agent_board.http``
# (imported by tests and operator tooling via ``server.api.board_routes``).
from .trust import (
    _LOOPBACK_NETS as _LOOPBACK_NETS,
    _TAILSCALE_NET as _TAILSCALE_NET,
    ADMIN_GATE,
    TRUSTED_CIDRS_ENV,
    WRITE_GATE,
    _allowed_networks as _allowed_networks,
    _allowed_proxy_networks as _allowed_proxy_networks,
    _client_address as _client_address,
    _describe_networks as _describe_networks,
    _forwarded_chain as _forwarded_chain,
    _in_any as _in_any,
    _is_localhost as _is_localhost,
    _is_loopback as _is_loopback,
    _is_trusted_peer as _is_trusted_peer,
    _parse_cidrs as _parse_cidrs,
    _parse_ip as _parse_ip,
    _peer_address as _peer_address,
    _presented_key as _presented_key,
    _req_settings as _req_settings,
    _req_sweeper as _req_sweeper,
    _require_admin as _require_admin,
    _require_key as _require_key,
    _require_trust as _require_trust,
    _require_write as _require_write,
    _trust_proxy_enabled as _trust_proxy_enabled,
    admin_gate,
    board_settings,
    write_gate,
)

_logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(messages.router)
router.include_router(admin.router)


# ── Trust policy (status route + boot log) ─────────────────────


def trust_policy(settings: Settings | None = None) -> dict[str, Any]:
    """The effective trust configuration, for the status route and boot log."""
    settings = settings if settings is not None else Settings()
    try:
        require_key = bool(store.load_config().get("require_key_for_post"))
    except Exception:
        _logger.exception("could not read board config for trust policy")
        require_key = False
    return {
        "trusted_cidrs": [str(n) for n in _allowed_networks(settings)],
        "require_key_for_post": require_key,
        "trust_proxy": _trust_proxy_enabled(settings),
        "trusted_proxies": [str(n) for n in _allowed_proxy_networks(settings)],
        "max_scopes": scopes.max_scopes(),
        "max_request_body_bytes": MAX_REQUEST_BODY_BYTES,
    }


def log_trust_policy(settings: Settings | None = None) -> None:
    """Log the effective trust policy once at startup."""
    settings = settings if settings is not None else Settings()
    policy = trust_policy(settings)
    # The CIDR lists are read from the allowlist helpers (not the policy dict)
    # and rendered through ``_describe_networks`` so the record states plainly
    # that these are network allowlists, not credentials.
    _logger.info(
        "board trust: trusted_cidrs=%s require_key_for_post=%s trust_proxy=%s "
        "trusted_proxies=%s max_scopes=%s admin_routes=key-required",
        _describe_networks(_allowed_networks(settings)),
        policy["require_key_for_post"],
        policy["trust_proxy"],
        _describe_networks(_allowed_proxy_networks(settings)),
        policy["max_scopes"],
    )


# ── Mounting ───────────────────────────────────────────────────


def mount_board(app: FastAPI, *, prefix: str = "/board") -> None:
    """Include the board router on ``app`` and cap streamed ``/board`` bodies.

    The body-size middleware is added here; callers register CORS *after*
    this so ``CORSMiddleware`` stays the outermost layer (``add_middleware``
    is LIFO) and a 413 still carries the CORS grant.
    """
    app.include_router(router, prefix=prefix, tags=["board"])
    # Streamed-body cap for /board: ``Content-Length`` is checked in the
    # routes too, but only this middleware stops a chunked upload.
    limits.install_body_limit(app, prefix)


__all__ = [
    "ADMIN_GATE",
    "CONFIG_FIELDS",
    "MAX_REQUEST_BODY_BYTES",
    "TRUSTED_CIDRS_ENV",
    "WRITE_GATE",
    "AckIn",
    "ConfigPatchIn",
    "KeyCreateIn",
    "MessageIn",
    "PermissionGrantIn",
    "ProviderBindingIn",
    "admin_gate",
    "board_settings",
    "log_trust_policy",
    "mount_board",
    "router",
    "trust_policy",
    "write_gate",
]
