"""Agent Board — provider-key vault.

SHA-256 hashed key store backing the Config tab. The raw key is shown to
the operator once on creation and never persisted; only its hash lands
in the database. The `keb_` prefix namespaces these keys so they cannot
be confused with keys minted by an unrelated system that happens to
share the machine.

Schema lives in `foundation/db.py` (`agent_api_keys`, `agent_key_permissions`).

Resource types are deliberately narrow:

| Resource type | Examples                                            |
|---------------|------------------------------------------------------|
| provider      | `anthropic`, `openai`, `ollama`, `localrouter`       |
| board         | `*` (full board), `read`, `write`, `admin`           |
| tool          | tool route ID from `tools/host.py` registrations     |
| model         | `claude-opus-4-7`, `gpt-4o`, `llama3:70b`, etc.      |
| endpoint      | named endpoint group registered elsewhere            |

Permission levels: `read`, `write`, `invoke`, `admin`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..foundation import db

KEY_PREFIX = "keb_"

VALID_RESOURCE_TYPES: frozenset[str] = frozenset({
    "provider", "board", "tool", "model", "endpoint",
})

VALID_PERMISSIONS: frozenset[str] = frozenset({
    "read", "write", "invoke", "admin",
})


# ── Raw key generation + hashing ───────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


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
    now = _now_iso()

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO agent_api_keys
           (key_id, key_hash, display_name, created_by, enabled, is_master,
            created_at, updated_at, expires_at, notes)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (
            key_id, key_hash, display_name, created_by,
            1 if is_master else 0, now, now, expires_at, notes,
        ),
    )
    conn.commit()

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


def verify_key(raw_key: str) -> dict[str, Any] | None:
    """Return the key row if valid+enabled+unexpired, else None."""
    key_hash = _hash_key(raw_key)
    conn = db.get_connection()
    now = _now_iso()

    row = conn.execute(
        """SELECT * FROM agent_api_keys
           WHERE key_hash = ? AND enabled = 1
             AND (expires_at IS NULL OR expires_at > ?)""",
        (key_hash, now),
    ).fetchone()

    if row is None:
        return None

    conn.execute(
        "UPDATE agent_api_keys SET last_used_at = ? WHERE key_id = ?",
        (now, row["key_id"]),
    )
    conn.commit()

    return db.dict_from_row(row)


def get_key(key_id: str) -> dict[str, Any] | None:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM agent_api_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
    return db.dict_from_row(row)


def list_keys(include_master: bool = True) -> list[dict[str, Any]]:
    """Return all keys without hashes. `include_master=False` hides master keys."""
    conn = db.get_connection()
    sql = (
        "SELECT key_id, display_name, created_by, enabled, is_master, "
        "created_at, updated_at, last_used_at, expires_at, notes "
        "FROM agent_api_keys"
    )
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
    row = conn.execute(
        "SELECT * FROM agent_api_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
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
        (new_state, _now_iso(), key_id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM agent_api_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
    return db.dict_from_row(updated)


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
    return cursor.rowcount > 0


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
    now = _now_iso()

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO agent_key_permissions
           (perm_id, key_id, resource_type, resource_id, permission, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (perm_id, key_id, resource_type, resource_id, permission, now),
    )
    conn.commit()

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
    cursor = conn.execute(
        "DELETE FROM agent_key_permissions WHERE perm_id = ?", (perm_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def list_permissions(key_id: str) -> list[dict[str, Any]]:
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT * FROM agent_key_permissions
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
        """SELECT * FROM agent_key_permissions
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

        # Best-effort file write — never raise from bootstrap.
        try:
            from ..config import Config
            cfg = Config.from_env()
            key_file = cfg.data_dir / "board-master-key.txt"
            key_file.write_text(
                "Knowledge-Engine Agent Board — Master Key\n"
                f"Created: {key_data['created_at']}\n"
                f"Key ID:  {key_data['key_id']}\n"
                f"Raw Key: {key_data['raw_key']}\n"
                "\n"
                "Full admin access to all board resources. Store securely and\n"
                "delete this file after copying the raw key.\n",
                encoding="utf-8",
            )
            # Tighten file perms on POSIX so the key isn't world-readable.
            try:
                import os
                os.chmod(key_file, 0o600)
            except OSError:
                pass
        except Exception:
            pass

        return key_data


def get_key_summary(key_id: str) -> dict[str, Any] | None:
    """Get key info with all its permissions."""
    key = get_key(key_id)
    if key is None:
        return None
    key["permissions"] = list_permissions(key_id)
    return key


__all__ = [
    "KEY_PREFIX", "VALID_RESOURCE_TYPES", "VALID_PERMISSIONS",
    "LastMasterKeyError",
    "generate_key", "create_key", "verify_key", "get_key",
    "list_keys", "toggle_key", "delete_key",
    "grant_permission", "revoke_permission", "list_permissions",
    "check_permission", "ensure_master_key", "get_key_summary",
]
