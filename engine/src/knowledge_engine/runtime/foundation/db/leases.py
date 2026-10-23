"""The board sweeper's singleton lease (a JSON row in ``kv_store``)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from . import connection

_logger = logging.getLogger(__name__)

SWEEPER_LEASE_KEY = "board.sweeper_lease"
MIN_LEASE_TTL_SECONDS = 5


def acquire_sweeper_lease(holder: str, ttl_seconds: int = 120) -> bool:
    """Try to claim the singleton sweeper lease in kv_store.

    Returns True if the caller now owns the lease; False if another holder
    has a valid lease. Lets the embedded-FastAPI sweeper and a standalone
    sweeper coexist on the same DB without doubling reminders / digests.

    The lease is a JSON value ``{holder, expires_at}`` keyed by
    :data:`SWEEPER_LEASE_KEY`. Expired (or corrupt) leases are stolen.
    """
    ttl_seconds = max(ttl_seconds, MIN_LEASE_TTL_SECONDS)
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    payload = json.dumps({"holder": holder, "expires_at": expires_at})

    conn = connection.get_connection()
    # SQLite needs serialization across writers; busy_timeout already set.
    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (SWEEPER_LEASE_KEY,)).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO kv_store(key, value, updated_at) VALUES (?, ?, ?)",
                (SWEEPER_LEASE_KEY, payload, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Lost the insert race -- fall through to the steal/renew decision.
            conn.rollback()
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (SWEEPER_LEASE_KEY,)
            ).fetchone()

    if row is not None and not _may_take(_parse_lease(row["value"]), holder, now):
        return False  # Someone else holds a live lease.

    conn.execute(
        "UPDATE kv_store SET value = ?, updated_at = ? WHERE key = ?",
        (payload, now, SWEEPER_LEASE_KEY),
    )
    conn.commit()
    return True


def _may_take(current: dict[str, Any], holder: str, now: str) -> bool:
    """True if ``holder`` may renew (it owns it) or steal (expired) ``current``."""
    cur_expires = current.get("expires_at")
    if not isinstance(cur_expires, str):
        cur_expires = ""
    if current.get("holder") == holder and cur_expires > now:
        return True  # already ours; renew
    return not (cur_expires and cur_expires > now)


def release_sweeper_lease(holder: str) -> None:
    """Release the lease if this holder owns it. Best-effort."""
    conn = connection.get_connection()
    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (SWEEPER_LEASE_KEY,)).fetchone()
    if row is None:
        return
    if _parse_lease(row["value"]).get("holder") != holder:
        return
    conn.execute("DELETE FROM kv_store WHERE key = ?", (SWEEPER_LEASE_KEY,))
    conn.commit()


def _parse_lease(raw: object) -> dict[str, Any]:
    """Decode a stored lease value; anything that is not a JSON object is no lease.

    A corrupt row (hand-edited, truncated write, foreign writer) must not
    wedge the sweeper: it is logged and treated as absent, so the next
    ``acquire_sweeper_lease`` overwrites it with a valid lease.
    """
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except ValueError:  # JSONDecodeError, or undecodable bytes
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    _logger.warning("ignoring corrupt board.sweeper_lease value; treating it as no lease")
    return {}


__all__ = ["SWEEPER_LEASE_KEY", "acquire_sweeper_lease", "release_sweeper_lease"]
