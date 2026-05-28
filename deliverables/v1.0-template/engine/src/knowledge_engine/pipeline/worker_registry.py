"""Knowledge-Engine — Worker Registry (SQLite-backed).

Manages worker registration, capability tracking, and heartbeat monitoring.
Expired heartbeats cause associated tasks to release back to the queue —
so one worker going offline never blocks anything.

Depends on `knowledge_engine.foundation.{config, db}`.

Named `worker_registry` to disambiguate from the corpus registry at
`knowledge_engine.registry`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..foundation import config, db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Registration ───────────────────────────────────────────


def register_worker(
    node_id: str,
    hostname: str | None = None,
    model: str | None = None,
    capabilities: list[str] | None = None,
    max_concurrent: int = 1,
    tailscale_ip: str | None = None,
    ollama_endpoint: str | None = None,
    ollama_model: str | None = None,
    role: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Register or update a worker. Auto-fills from nodes.yaml if available."""
    nodes_config = config.load_nodes()
    node_cfg = nodes_config.get("nodes", {}).get(node_id, {})

    hostname = hostname or node_cfg.get("hostname", node_id)
    model = model or node_cfg.get("model_class", "unknown")
    capabilities = capabilities or node_cfg.get("capabilities", [])
    max_concurrent = max_concurrent or node_cfg.get("max_concurrent_tasks", 1)
    tailscale_ip = tailscale_ip or node_cfg.get("tailscale_ip")
    ollama_endpoint = ollama_endpoint or node_cfg.get("ollama_endpoint")
    ollama_model = ollama_model or node_cfg.get("ollama_model")
    role = role or node_cfg.get("role", "worker")
    display_name = display_name or node_cfg.get("display_name", node_id)

    now = _now_iso()
    conn = db.get_connection()

    conn.execute(
        """INSERT INTO workers (
            node_id, hostname, tailscale_ip, display_name, model_class, role,
            capabilities, ollama_endpoint, ollama_model, max_concurrent,
            status, registered_at, last_heartbeat, config_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            hostname=excluded.hostname, tailscale_ip=excluded.tailscale_ip,
            display_name=excluded.display_name, model_class=excluded.model_class,
            role=excluded.role, capabilities=excluded.capabilities,
            ollama_endpoint=excluded.ollama_endpoint, ollama_model=excluded.ollama_model,
            max_concurrent=excluded.max_concurrent, status='online',
            last_heartbeat=excluded.last_heartbeat, config_json=excluded.config_json
        """,
        (
            node_id, hostname, tailscale_ip, display_name, model, role,
            json.dumps(capabilities), ollama_endpoint, ollama_model, max_concurrent,
            now, now, json.dumps(node_cfg),
        ),
    )
    conn.commit()
    db.log_event("worker_registered", node_id=node_id, conn=conn)
    return get_worker(node_id)  # type: ignore[return-value]


def unregister_worker(node_id: str) -> bool:
    """Remove a worker from the registry."""
    conn = db.get_connection()
    cursor = conn.execute("DELETE FROM workers WHERE node_id = ?", (node_id,))
    conn.commit()
    return cursor.rowcount > 0


# ── Queries ────────────────────────────────────────────────


def get_worker(node_id: str) -> dict[str, Any] | None:
    """Get a specific worker's registration."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM workers WHERE node_id = ?", (node_id,)).fetchone()
    return db.dict_from_row(row)


def list_workers(online_only: bool = False) -> list[dict[str, Any]]:
    """List all registered workers."""
    conn = db.get_connection()
    if online_only:
        rows = conn.execute("SELECT * FROM workers WHERE status = 'online'").fetchall()
    else:
        rows = conn.execute("SELECT * FROM workers").fetchall()
    return db.rows_to_dicts(rows)


def get_workers_with_capability(capability: str) -> list[dict[str, Any]]:
    """Find online workers that have a specific capability."""
    workers = list_workers(online_only=True)
    return [w for w in workers if capability in w.get("capabilities", [])]


# ── Heartbeat ──────────────────────────────────────────────


def send_heartbeat(
    node_id: str,
    current_tasks: list[str] | None = None,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record a heartbeat with optional resource snapshot."""
    now = _now_iso()
    conn = db.get_connection()
    conn.execute(
        """UPDATE workers SET
            status = 'online', last_heartbeat = ?, current_tasks = ?,
            resources_json = COALESCE(?, resources_json)
           WHERE node_id = ?""",
        (now, json.dumps(current_tasks or []),
         json.dumps(resources) if resources else None, node_id),
    )
    conn.commit()
    return get_worker(node_id)


def check_heartbeat(node_id: str) -> str | None:
    """Return the timestamp of the last heartbeat for a worker, or None."""
    w = get_worker(node_id)
    return w["last_heartbeat"] if w else None


def get_expired_workers(timeout_seconds: int = 180) -> list[str]:
    """Return node IDs of workers whose heartbeat has expired."""
    expired = []
    now = datetime.now(timezone.utc)

    for w in list_workers(online_only=True):
        last = w.get("last_heartbeat")
        if not last:
            expired.append(w["node_id"])
            continue
        hb_time = datetime.fromisoformat(last)
        if (now - hb_time).total_seconds() > timeout_seconds:
            expired.append(w["node_id"])

    return expired


def mark_workers_offline(node_ids: list[str]) -> None:
    """Mark workers as offline."""
    conn = db.get_connection()
    for nid in node_ids:
        conn.execute(
            "UPDATE workers SET status = 'offline', current_tasks = '[]' WHERE node_id = ?",
            (nid,),
        )
        db.log_event("worker_offline", node_id=nid, conn=conn)
    conn.commit()


# ── Safeguard Controls ────────────────────────────────────────


def set_safeguard(node_id: str, locked: bool, reason: str = "") -> dict[str, Any] | None:
    """Lock or unlock a worker's safeguard. Returns updated worker or None."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE workers SET safeguard_locked = ?, safeguard_reason = ? WHERE node_id = ?",
        (1 if locked else 0, reason, node_id),
    )
    conn.commit()
    action = "safeguard_lock" if locked else "safeguard_unlock"
    db.log_event(action, node_id=node_id, detail=reason, conn=conn)
    return get_worker(node_id)


def get_safeguard_status(node_id: str) -> dict[str, Any] | None:
    """Return safeguard status for a worker."""
    w = get_worker(node_id)
    if not w:
        return None
    return {
        "node_id": node_id,
        "safeguard_locked": bool(w.get("safeguard_locked", 0)),
        "safeguard_reason": w.get("safeguard_reason", ""),
        "status": w.get("status"),
    }


def list_safeguard_statuses() -> list[dict[str, Any]]:
    """Return safeguard status for all workers."""
    workers = list_workers()
    return [
        {
            "node_id": w["node_id"],
            "safeguard_locked": bool(w.get("safeguard_locked", 0)),
            "safeguard_reason": w.get("safeguard_reason", ""),
            "status": w.get("status"),
        }
        for w in workers
    ]
