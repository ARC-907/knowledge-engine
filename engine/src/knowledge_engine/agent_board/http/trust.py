"""Board HTTP: peer trust, X-Forwarded-For handling and the WRITE / ADMIN key gates.

See :mod:`knowledge_engine.agent_board.http` for the authorization model.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence

from fastapi import Depends, HTTPException, Request

from ...core import http as core_http
from ...core.http import IPAddress, IPNetwork
from ...core.logsafe import log_safe
from ...core.settings import Settings
from .. import keys, store, sweeper
from .limits import _check_body_size

_logger = logging.getLogger(__name__)

# Re-exported under their historical names (see ``core.http``).
_LOOPBACK_NETS = core_http.LOOPBACK_NETS
_parse_cidrs = core_http.parse_cidrs


# ── Settings / sweeper resolution ──────────────────────────────


def _req_settings(req: Request | None) -> Settings:
    """The app's settings (``app.state.settings``), else the current environment."""
    try:
        found = getattr(req.app.state, "settings", None) if req is not None else None
    except (AttributeError, KeyError):
        found = None
    return found if isinstance(found, Settings) else Settings()


def board_settings(request: Request) -> Settings:
    """FastAPI dependency: the settings the board routes run under."""
    return _req_settings(request)


def _req_sweeper(req: Request) -> sweeper.Sweeper:
    """The sweeper the app's lifespan owns, else the process default."""
    try:
        found = getattr(req.app.state, "sweeper", None)
    except (AttributeError, KeyError):
        found = None
    return found if isinstance(found, sweeper.Sweeper) else sweeper.default_sweeper()


def _trust_proxy_enabled(settings: Settings | None = None) -> bool:
    """``KE_TRUST_PROXY`` (see ``core.http.trust_proxy_enabled``)."""
    return core_http.trust_proxy_enabled(settings=settings)


def _allowed_proxy_networks(settings: Settings | None = None) -> list[IPNetwork]:
    """``KE_TRUSTED_PROXIES`` (see ``core.http.allowed_proxy_networks``)."""
    return core_http.allowed_proxy_networks(settings=settings)


# ── Trust gate ─────────────────────────────────────────────────


# Tailscale CGNAT range — every Tailscale-issued IPv4 lives here. Treating
# the whole /10 as trusted means peers on the operator's private mesh
# don't need to round-trip through key creation just to coordinate.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

TRUSTED_CIDRS_ENV = "KE_BOARD_TRUSTED_CIDRS"

# `_parse_cidrs`, `_trust_proxy_enabled`, `_allowed_proxy_networks`, the two proxy
# env names and `_LOOPBACK_NETS` live in ``core.http`` so the uvicorn entry
# points can derive their proxy settings from the very same policy; they are
# re-exported here under their historical names.


def _allowed_networks(settings: Settings | None = None) -> list[IPNetwork]:
    """The active trusted-CIDR list.

    Defaults to loopback (v4 + v6) + Tailscale CGNAT. The
    `KE_BOARD_TRUSTED_CIDRS` env var REPLACES the default — set it to e.g.
    `127.0.0.1/32,::1/128` to disable Tailscale trust, or add extra
    private-mesh ranges if you operate outside Tailscale.
    """
    raw = (settings if settings is not None else Settings()).board_peer_allowlist
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


def _client_address(req: Request, settings: Settings | None = None) -> IPAddress | None:
    """The address the trust gate judges.

    Without `KE_TRUST_PROXY` this is the socket peer. With it, and only when
    the socket peer is a trusted proxy, the `X-Forwarded-For` chain is walked
    from the right: trusted proxies are skipped and the first remaining
    address is the client. A chain made only of trusted proxies resolves to
    its leftmost entry (the proxy's own view of the client). A malformed
    entry makes the request untrusted.
    """
    settings = settings if settings is not None else _req_settings(req)
    peer = _peer_address(req)
    if peer is None or not _trust_proxy_enabled(settings):
        return peer
    proxies = _allowed_proxy_networks(settings)
    if not _in_any(peer, proxies):
        return peer
    chain = _forwarded_chain(req)
    if not chain:
        return peer
    parsed: list[IPAddress] = []
    for hop in chain:
        addr = _parse_ip(hop)
        if addr is None:
            _logger.warning("malformed X-Forwarded-For entry %r from proxy %s", log_safe(hop), peer)
            return None
        parsed.append(addr)
    for addr in reversed(parsed):
        if not _in_any(addr, proxies):
            return addr
    return parsed[0]


def _is_trusted_peer(req: Request, settings: Settings | None = None) -> bool:
    """Return True if the effective client is on a trusted network."""
    settings = settings if settings is not None else _req_settings(req)
    client = _client_address(req, settings)
    if client is None:
        return False
    nets = _allowed_networks(settings)
    if not nets:
        return False
    return _in_any(client, nets)


# Backwards-compatible alias for any external callers that imported the
# old name. Internally everyone now uses `_is_trusted_peer`.
def _is_localhost(req: Request) -> bool:
    return _is_trusted_peer(req)


def _require_trust(req: Request, settings: Settings | None = None) -> None:
    """Hard gate: reject any peer not on a trusted network."""
    if not _is_trusted_peer(req, settings):
        _logger.warning(
            "board: rejected %s %s from untrusted peer %s",
            log_safe(req.method),
            log_safe(req.url.path),
            _peer_address(req),
        )
        raise HTTPException(403, "peer not on a trusted network")


def _presented_key(req: Request) -> str | None:
    raw = req.headers.get("X-Board-Key", "").strip()
    return raw or None


def _require_write(req: Request, resource_id: str = "*") -> None:
    """WRITE gate: trust, body cap, then the `require_key_for_post` decision.

    See :func:`keys.authorize_write` for the exact rule; it is shared with
    the MCP post tools so both surfaces agree.
    """
    settings = _req_settings(req)
    _require_trust(req, settings)
    _check_body_size(req)
    cfg = store.load_config()
    decision = keys.authorize_write(
        _presented_key(req),
        loopback=_is_loopback(_client_address(req, settings)),
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
            log_safe(req.method),
            log_safe(req.url.path),
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


def write_gate(req: Request) -> None:
    """:func:`_require_write` as a route dependency: runs before the body is parsed."""
    _require_write(req)


def admin_gate(req: Request) -> str:
    """:func:`_require_admin` as a route dependency: runs before the body is parsed.

    Resolves to the authenticated key id, for routes that log it.
    """
    return _require_admin(req)


#: Dependency lists for body-taking routes; the gate decides before FastAPI
#: reads the body, so untrusted peers never see a validation error.
WRITE_GATE = [Depends(write_gate)]
ADMIN_GATE = [Depends(admin_gate)]


def _describe_networks(networks: Sequence[IPNetwork]) -> str:
    """Render a network allowlist for the boot log (``['a/32', 'b/8']``)."""
    return "[" + ", ".join(repr(str(network)) for network in networks) + "]"


__all__ = [
    "ADMIN_GATE",
    "TRUSTED_CIDRS_ENV",
    "WRITE_GATE",
    "admin_gate",
    "board_settings",
    "write_gate",
]
