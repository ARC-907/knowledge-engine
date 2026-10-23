"""The append-only ``events`` audit log of the pipeline database."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ....core.util.time import utc_now_iso
from . import connection


def log_event(
    event_type: str,
    task_id: str | None = None,
    node_id: str | None = None,
    agent_id: str | None = None,
    detail: str | None = None,
    data: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Append an event to the audit log."""
    if conn is None:
        conn = connection.get_connection()
    conn.execute(
        """INSERT INTO events (timestamp, event_type, task_id, node_id, agent_id, detail, data_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            utc_now_iso(),
            event_type,
            task_id,
            node_id,
            agent_id,
            detail,
            json.dumps(data) if data else None,
        ),
    )
    conn.commit()


__all__ = ["log_event"]
