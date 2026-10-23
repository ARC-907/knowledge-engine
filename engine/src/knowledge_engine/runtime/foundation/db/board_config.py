"""Defaults and first-boot seeding of the board's ``board_config`` singleton row."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ....core.util.time import utc_now_iso

_logger = logging.getLogger(__name__)

BOARD_CONFIG_DEFAULT_CHANNELS: tuple[str, ...] = (
    "ops",
    "research",
    "project",
    "worktree",
    "branch",
    "library",
    "planning",
    "execution",
    "testing",
    "chatter",
)

# Values seeded into the ``board_config`` singleton row on first boot. Also
# what a read of a not-yet-created scope reports, so the two never drift.
BOARD_CONFIG_DEFAULTS: dict[str, int] = {
    "engine_port": 9210,
    "standalone_port": 11437,
    "sweep_interval_s": 60,
    "stale_blocker_hours": 2,
    "digest_interval_minutes": 60,
    "max_messages_before_prune": 5000,
    "default_ttl_hours": 168,
    "sweeper_enabled": 1,
    "require_key_for_post": 0,
}


def default_board_config() -> dict[str, Any]:
    """The ``board_config`` row as it would be seeded, without touching a DB."""
    cfg: dict[str, Any] = {"id": 1, **BOARD_CONFIG_DEFAULTS}
    cfg["channels_json"] = json.dumps(list(BOARD_CONFIG_DEFAULT_CHANNELS))
    cfg["updated_at"] = None
    return cfg


def seed_board_config(conn: sqlite3.Connection) -> None:
    """Insert the singleton board_config row on first boot. Idempotent.

    ``INSERT OR IGNORE`` keeps two connections seeding the same fresh
    database at once from colliding on the primary key.
    """
    row = conn.execute("SELECT id FROM board_config WHERE id = 1").fetchone()
    if row is not None:
        return
    defaults = BOARD_CONFIG_DEFAULTS
    cursor = conn.execute(
        """INSERT OR IGNORE INTO board_config (
            id, engine_port, standalone_port, sweep_interval_s,
            stale_blocker_hours, digest_interval_minutes,
            max_messages_before_prune, default_ttl_hours,
            sweeper_enabled, require_key_for_post, channels_json, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            defaults["engine_port"],
            defaults["standalone_port"],
            defaults["sweep_interval_s"],
            defaults["stale_blocker_hours"],
            defaults["digest_interval_minutes"],
            defaults["max_messages_before_prune"],
            defaults["default_ttl_hours"],
            defaults["sweeper_enabled"],
            defaults["require_key_for_post"],
            json.dumps(list(BOARD_CONFIG_DEFAULT_CHANNELS)),
            utc_now_iso(),
        ),
    )
    conn.commit()
    if cursor.rowcount:
        _logger.debug("seeded the board_config row with defaults")


__all__ = [
    "BOARD_CONFIG_DEFAULTS",
    "BOARD_CONFIG_DEFAULT_CHANNELS",
    "default_board_config",
    "seed_board_config",
]
