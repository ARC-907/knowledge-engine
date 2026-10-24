"""Agent Board — provider-key vault.

SHA-256 hashed key store backing the Config tab. The raw key is shown to
the operator once on creation and never persisted; only its hash lands
in the database. The `keb_` prefix namespaces these keys so they cannot
be confused with keys minted by an unrelated system that happens to
share the machine.

Schema lives in `foundation/db/schema/001_core_board.sql` (`agent_api_keys`,
`agent_key_permissions`).

Resource types are deliberately narrow:

| Resource type | Examples                                            |
|---------------|------------------------------------------------------|
| provider      | `anthropic`, `openai`, `ollama`, `localrouter`       |
| board         | `*` (full board), `read`, `write`, `admin`           |
| tool          | tool route ID from `tool_host/host.py` registrations |
| model         | a provider model id, e.g. `llama3:70b`               |
| endpoint      | named endpoint group registered elsewhere            |

Permission levels: `read`, `write`, `invoke`, `admin`.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core import paths
from ..core.logsafe import log_safe
from ..core.util.hashing import sha256_hex
from ..core.util.time import utc_now_iso
from ..runtime.foundation import db

_logger = logging.getLogger(__name__)

KEY_PREFIX = "keb_"

VALID_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "provider",
        "board",
        "tool",
        "model",
        "endpoint",
    }
)

VALID_PERMISSIONS: frozenset[str] = frozenset(
    {
        "read",
        "write",
        "invoke",
        "admin",
    }
)

MASTER_KEY_FILENAME = "board-master-key.txt"

# Every SELECT against ``agent_api_keys`` names its columns so ``key_hash``
# never leaves this module — not in API responses, not in logs, not in the
# dicts handed to the HTTP layer.
_KEY_COLUMNS = (
    "key_id, display_name, created_by, enabled, is_master, "
    "created_at, updated_at, last_used_at, expires_at, notes"
)
_PERMISSION_COLUMNS = "perm_id, key_id, resource_type, resource_id, permission, enabled, created_at"


# ── Raw key generation + hashing ───────────────────────────────


def _hash_key(raw_key: str) -> str:
    return sha256_hex(raw_key)


def generate_key() -> tuple[str, str]:
    """Return (raw_key, key_hash). Raw is shown once; only the hash persists."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, _hash_key(raw)


# ── Key CRUD ───────────────────────────────────────────────────


def create_key(
    display_name: str,
    created_by: str = "admin",
    expires_at: str | None = None,
    notes: str | None = None,
    is_master: bool = False,
) -> dict[str, Any]:
    """Create a new key. Returns dict including the raw_key (one-time view)."""
    raw_key, key_hash = generate_key()
    key_id = str(uuid4())
    now = utc_now_iso()

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO agent_api_keys
           (key_id, key_hash, display_name, created_by, enabled, is_master,
            created_at, updated_at, expires_at, notes)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (
            key_id,
            key_hash,
            display_name,
            created_by,
            1 if is_master else 0,
            now,
            now,
            expires_at,
            notes,
        ),
    )
    conn.commit()
    _logger.info(
        "board key created: key_id=%s label=%r master=%s created_by=%s",
        log_safe(key_id),
        log_safe(display_name),
        is_master,
        log_safe(created_by),
    )

    return {
        "key_id": key_id,
        "raw_key": raw_key,
        "display_name": display_name,
        "created_by": created_by,
        "is_master": is_master,
        "enabled": True,
        "created_at": now,
        "expires_at": expires_at,
    }


#: ``last_used_at`` is refreshed at most this often per key. Every
#: authenticated request used to write (and commit) it, turning reads into
#: writes that contend for SQLite's single write lock.
LAST_USED_RESOLUTION_SECONDS = 60


def verify_key(raw_key: str) -> dict[str, Any] | None:
    """Return the key row if valid+enabled+unexpired, else None.

    ``last_used_at`` is updated when it is unset or older than
    :data:`LAST_USED_RESOLUTION_SECONDS`, not on every call.
    """
    key_hash = _hash_key(raw_key)
    conn = db.get_connection()
    now = utc_now_iso()

    row = conn.execute(
        f"""SELECT {_KEY_COLUMNS} FROM agent_api_keys
           WHERE key_hash = ? AND enabled = 1
             AND (expires_at IS NULL OR expires_at > ?)""",
        (key_hash, now),
    ).fetchone()

    if row is None:
        return None

    stale_before = utc_now_iso(-timedelta(seconds=LAST_USED_RESOLUTION_SECONDS))
    last_used = row["last_used_at"]
    if last_used is None or last_used < stale_before:
        conn.execute(
            """UPDATE agent_api_keys SET last_used_at = ?
               WHERE key_id = ? AND (last_used_at IS NULL OR last_used_at < ?)""",
            (now, row["key_id"], stale_before),
        )
        conn.commit()

    return db.dict_from_row(row)


def get_key(key_id: str) -> dict[str, Any] | None:
    """Key metadata by id (never the hash), or None."""
    conn = db.get_connection()
    row = conn.execute(
        f"SELECT {_KEY_COLUMNS} FROM agent_api_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
    return db.dict_from_row(row)


def list_keys(include_master: bool = True) -> list[dict[str, Any]]:
    """Return all keys without hashes. `include_master=False` hides master keys."""
    conn = db.get_connection()
    sql = f"SELECT {_KEY_COLUMNS} FROM agent_api_keys"
    if not include_master:
        sql += " WHERE is_master = 0"
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql).fetchall()
    return db.rows_to_dicts(rows)


class LastMasterKeyError(ValueError):
    """Raised when an operation would leave zero enabled master keys.

    Carries a recovery hint so callers (and the HTTP layer) can surface
    actionable guidance instead of a generic 500.
    """


def _would_zero_enabled_masters(conn: Any, key_id: str) -> bool:
    """True if disabling / deleting `key_id` removes the last enabled master."""
    target = conn.execute(
        "SELECT is_master, enabled FROM agent_api_keys WHERE key_id = ?",
        (key_id,),
    ).fetchone()
    if target is None or not target["is_master"] or not target["enabled"]:
        return False
    remaining = conn.execute(
        """SELECT COUNT(*) AS n FROM agent_api_keys
           WHERE is_master = 1 AND enabled = 1 AND key_id != ?""",
        (key_id,),
    ).fetchone()
    return (remaining["n"] if remaining else 0) == 0


_LAST_MASTER_HINT = (
    "refusing to {action} the last enabled master key — this would lock "
    "you out of admin endpoints. Create another master key with "
    "`board keys create ... --permission admin` first, OR delete this "
    "master directly in SQLite and re-run `board keys bootstrap-master` "
    "(loopback-only) to self-heal."
)


def toggle_key(key_id: str) -> dict[str, Any] | None:
    """Flip a key's enabled state.

    Refuses (with `LastMasterKeyError`) to disable the last enabled
    master so a routine API call can't lock the operator out of admin
    routes. Disabling a non-master, or disabling a master while another
    enabled master exists, is always allowed.
    """
    conn = db.get_connection()
    row = conn.execute("SELECT enabled FROM agent_api_keys WHERE key_id = ?", (key_id,)).fetchone()
    if row is None:
        return None
    # Refuse if this toggle would zero enabled masters. Re-enabling a
    # disabled master is always fine — the unique partial index only
    # binds when is_master=1 AND enabled=1, so the operator can have
    # multiple disabled masters and pick one to revive.
    if row["enabled"] and _would_zero_enabled_masters(conn, key_id):
        raise LastMasterKeyError(_LAST_MASTER_HINT.format(action="disable"))
    new_state = 0 if row["enabled"] else 1
    conn.execute(
        "UPDATE agent_api_keys SET enabled = ?, updated_at = ? WHERE key_id = ?",
        (new_state, utc_now_iso(), key_id),
    )
    conn.commit()
    _logger.info(
        "board key %s: key_id=%s", "enabled" if new_state else "disabled", log_safe(key_id)
    )
    return get_key(key_id)


def delete_key(key_id: str) -> bool:
    """Delete a key and cascade its permissions.

    Refuses (with `LastMasterKeyError`) to delete the last enabled
    master for the same reason `toggle_key` refuses to disable it.
    """
    conn = db.get_connection()
    if _would_zero_enabled_masters(conn, key_id):
        raise LastMasterKeyError(_LAST_MASTER_HINT.format(action="delete"))
    conn.execute("DELETE FROM agent_key_permissions WHERE key_id = ?", (key_id,))
    cursor = conn.execute("DELETE FROM agent_api_keys WHERE key_id = ?", (key_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    if deleted:
        _logger.info("board key revoked: key_id=%s", log_safe(key_id))
    return deleted


# ── Permission CRUD ────────────────────────────────────────────


def grant_permission(
    key_id: str,
    resource_type: str,
    resource_id: str = "*",
    permission: str = "invoke",
) -> dict[str, Any]:
    if resource_type != "*" and resource_type not in VALID_RESOURCE_TYPES:
        raise ValueError(f"Invalid resource_type: {resource_type}")
    if permission not in VALID_PERMISSIONS:
        raise ValueError(f"Invalid permission: {permission}")

    perm_id = str(uuid4())
    now = utc_now_iso()

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO agent_key_permissions
           (perm_id, key_id, resource_type, resource_id, permission, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (perm_id, key_id, resource_type, resource_id, permission, now),
    )
    conn.commit()
    _logger.info(
        "board permission granted: key_id=%s %s:%s=%s",
        log_safe(key_id),
        log_safe(resource_type),
        log_safe(resource_id),
        log_safe(permission),
    )

    return {
        "perm_id": perm_id,
        "key_id": key_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "permission": permission,
        "enabled": True,
        "created_at": now,
    }


def revoke_permission(perm_id: str) -> bool:
    conn = db.get_connection()
    cursor = conn.execute("DELETE FROM agent_key_permissions WHERE perm_id = ?", (perm_id,))
    conn.commit()
    revoked = cursor.rowcount > 0
    if revoked:
        _logger.info("board permission revoked: perm_id=%s", log_safe(perm_id))
    return revoked


def list_permissions(key_id: str) -> list[dict[str, Any]]:
    conn = db.get_connection()
    rows = conn.execute(
        f"""SELECT {_PERMISSION_COLUMNS} FROM agent_key_permissions
           WHERE key_id = ?
           ORDER BY resource_type, resource_id""",
        (key_id,),
    ).fetchall()
    return db.rows_to_dicts(rows)


def check_permission(
    key_id: str,
    resource_type: str,
    resource_id: str = "*",
    permission: str = "invoke",
) -> bool:
    """Master keys always pass. Otherwise check enabled permissions with
    wildcard matching on resource_type/id and `admin` implying everything.
    """
    conn = db.get_connection()
    master = conn.execute(
        "SELECT is_master FROM agent_api_keys WHERE key_id = ? AND enabled = 1",
        (key_id,),
    ).fetchone()
    if master and master["is_master"]:
        return True

    rows = conn.execute(
        """SELECT permission FROM agent_key_permissions
           WHERE key_id = ? AND enabled = 1
             AND (resource_type = ? OR resource_type = '*')
             AND (resource_id = ? OR resource_id = '*')""",
        (key_id, resource_type, resource_id),
    ).fetchall()
    for row in rows:
        if row["permission"] == "admin":
            return True
        if row["permission"] == permission:
            return True
    return False


# ── Master-key bootstrap ───────────────────────────────────────


def ensure_master_key() -> dict[str, Any] | None:
    """Create a master key on first request to an admin endpoint.

    Returns the new key dict (with raw_key shown once) or None if a master
    key already exists. Writes the raw key to `<data_dir>/board-master-key.txt`
    so the operator can grab it after first boot.

    Concurrency-safe: serialized by a module-level lock (`db.master_key_lock`)
    AND backed by a unique partial index in the schema so two concurrent
    bootstrap requests can never both succeed. On the unique-index race the
    second caller sees the freshly-created master and returns None.
    """
    with db.master_key_lock():
        conn = db.get_connection()
        existing = conn.execute(
            "SELECT key_id FROM agent_api_keys WHERE is_master = 1 AND enabled = 1"
        ).fetchone()
        if existing:
            return None

        try:
            key_data = create_key(
                display_name="Board Master Key",
                created_by="system",
                is_master=True,
                notes="Auto-created board master key with full admin access.",
            )
        except sqlite3.IntegrityError:
            # Lost the unique-index race — another caller created the master
            # between the SELECT and the INSERT. Surface "already exists".
            return None
        grant_permission(key_data["key_id"], "*", "*", "admin")
        _logger.warning("board master key bootstrapped: key_id=%s", log_safe(key_data["key_id"]))

        # Best-effort file write — never raise from bootstrap; the raw key
        # is still in the return value.
        try:
            _write_master_key_file(paths.ensure_data_dir() / MASTER_KEY_FILENAME, key_data)
        except Exception:
            _logger.warning("could not write board master key file", exc_info=True)

        return key_data


def _write_master_key_file(key_file: Path, key_data: dict[str, Any]) -> None:
    """Write the one-time master-key note with mode 0600 from the first byte.

    ``O_CREAT | O_EXCL`` with an explicit mode means the file never exists
    world-readable, however permissive the umask. A leftover file from an
    earlier bootstrap (whose key is dead) is removed first; a path that is
    not a regular file is left alone and logged.
    """
    content = (
        "Knowledge-Engine Agent Board — Master Key\n"
        f"Created: {key_data['created_at']}\n"
        f"Key ID:  {key_data['key_id']}\n"
        f"Raw Key: {key_data['raw_key']}\n"
        "\n"
        "Full admin access to all board resources. Store securely and\n"
        "delete this file after copying the raw key.\n"
    )
    if key_file.is_symlink() or (key_file.exists() and not key_file.is_file()):
        _logger.warning("not writing master key file: %s is not a regular file", key_file)
        return
    if key_file.exists():
        _logger.warning("replacing stale master key file %s", key_file)
        key_file.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(key_file, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    _logger.info("board master key written to %s (mode 0600)", key_file)


def get_key_summary(key_id: str) -> dict[str, Any] | None:
    """Get key info (never the hash) with all its permissions."""
    key = get_key(key_id)
    if key is None:
        return None
    key["permissions"] = list_permissions(key_id)
    return key


def has_master_key() -> bool:
    """True if at least one enabled master key exists."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM agent_api_keys WHERE is_master = 1 AND enabled = 1 LIMIT 1"
    ).fetchone()
    return row is not None


# ── Authorization decisions shared by HTTP and MCP ─────────────


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of a key check. ``status`` mirrors the HTTP code a route
    would answer with (401 missing/invalid, 403 insufficient); MCP callers
    only need ``ok`` / ``detail``."""

    ok: bool
    status: int = 200
    detail: str = ""
    key_id: str | None = None

    @classmethod
    def allow(cls, key_id: str | None = None) -> AuthDecision:
        return cls(True, 200, "", key_id)

    @classmethod
    def deny(cls, status: int, detail: str) -> AuthDecision:
        return cls(False, status, detail)


def authorize_key(raw_key: str | None, permission: str, resource_id: str = "*") -> AuthDecision:
    """Validate a presented key and check it holds ``permission`` on the board.

    Master keys pass every check (see :func:`check_permission`).
    """
    raw = (raw_key or "").strip()
    if not raw:
        return AuthDecision.deny(401, "Missing X-Board-Key header")
    record = verify_key(raw)
    if record is None:
        _logger.warning("board auth: invalid or expired key presented")
        return AuthDecision.deny(401, "Invalid or expired key")
    if not check_permission(record["key_id"], "board", resource_id, permission):
        _logger.warning(
            "board auth: key_id=%s lacks board:%s permission",
            log_safe(record["key_id"]),
            log_safe(permission),
        )
        return AuthDecision.deny(403, f"Key lacks board:{permission} permission")
    return AuthDecision.allow(record["key_id"])


def authorize_write(
    raw_key: str | None, *, loopback: bool, require_key: bool, resource_id: str = "*"
) -> AuthDecision:
    """The WRITE gate, identical for HTTP posts/acks and MCP post tools.

    * ``require_key`` is the persisted ``require_key_for_post`` flag.
    * With the flag off, writes from trusted peers are keyless (the caller
      has already passed the peer-trust gate).
    * With the flag on, loopback peers stay keyless **unless they present a
      key**, in which case it must be valid; everyone else must present a
      key with ``board:write``. The MCP stdio server is a local process and
      counts as loopback.
    """
    if not require_key:
        return AuthDecision.allow()
    if loopback and not (raw_key or "").strip():
        return AuthDecision.allow()
    return authorize_key(raw_key, "write", resource_id)


__all__ = [
    "KEY_PREFIX",
    "LAST_USED_RESOLUTION_SECONDS",
    "MASTER_KEY_FILENAME",
    "VALID_PERMISSIONS",
    "VALID_RESOURCE_TYPES",
    "AuthDecision",
    "LastMasterKeyError",
    "authorize_key",
    "authorize_write",
    "check_permission",
    "create_key",
    "delete_key",
    "ensure_master_key",
    "generate_key",
    "get_key",
    "get_key_summary",
    "grant_permission",
    "has_master_key",
    "list_keys",
    "list_permissions",
    "revoke_permission",
    "toggle_key",
    "verify_key",
]
