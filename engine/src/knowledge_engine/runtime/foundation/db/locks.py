"""Process-local locks that serialise check-then-write sequences on the board.

They close the in-process races; the database constraints stay the
authoritative defence (e.g. the unique partial index on enabled master keys).
Separate processes sharing a data directory are not serialised by these
locks.
"""

from __future__ import annotations

import threading

# Serializes ensure_master_key against concurrent bootstrap calls. The
# unique partial index in the schema is the authoritative defense; this
# lock just avoids the duplicate-key error path under normal racing.
_MASTER_KEY_LOCK = threading.Lock()

# Serializes the KE_BOARD_MAX_SCOPES count-then-create sequence so
# concurrent first posts to new scopes cannot overshoot the cap.
_SCOPE_CREATION_LOCK = threading.RLock()


def master_key_lock() -> threading.Lock:
    """Lock used by the board's master-key bootstrap to serialize concurrent calls."""
    return _MASTER_KEY_LOCK


def scope_creation_lock() -> threading.RLock:
    """Lock held while a new board scope is counted against the cap and created."""
    return _SCOPE_CREATION_LOCK


__all__ = ["master_key_lock", "scope_creation_lock"]
