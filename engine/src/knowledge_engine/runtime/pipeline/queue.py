"""Knowledge-Engine — SQLite-backed task queue.

Manages durable task state in SQLite. Lease-based claiming with automatic
expiry so offline workers never block the cluster.

Depends on `knowledge_engine.foundation.{config, db}`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..foundation import config, db

VALID_STATES = [
    "queued", "claimed", "running", "blocked",
    "awaiting_cloud", "complete", "failed", "archived",
]

VALID_TASK_TYPES = [
    "discovery", "retrieval", "normalize", "catalog",
    "cluster", "conflict_scan", "synthesis_request", "audit",
]

# Columns allowed in extra_fields to prevent SQL column-name injection.
_ALLOWED_EXTRA_COLUMNS = frozenset({
    "notes", "meta_json", "input_json", "output_artifacts",
    "depends_on", "assigned_capability", "source_policy", "audit_trail",
    "retry_count", "failure_reason", "target_path", "chapter_key",
})


def _is_valid_task_type(task_type: str) -> bool:
    """Check if task_type is valid. Pipeline types use 'pipeline:' prefix."""
    return task_type in VALID_TASK_TYPES or task_type.startswith("pipeline:")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj) if not isinstance(obj, str) else obj


# ── Task CRUD ──────────────────────────────────────────────


def create_task(
    task_type: str,
    library: str,
    target_path: str = "",
    chapter_key: str = "",
    priority: int = 3,
    depends_on: list[str] | None = None,
    allowed_layer: str = "local",
    assigned_capability: list[str] | None = None,
    source_policy: dict[str, bool] | None = None,
    notes: str | None = None,
    input_json: str | None = None,
    meta: str | None = None,
) -> dict[str, Any]:
    """Create a new task in 'queued' state."""
    if not _is_valid_task_type(task_type):
        raise ValueError(f"Invalid task_type: {task_type}")

    task_id = str(uuid.uuid4())
    now = _now_iso()

    default_policy = {
        "allow_web": True, "allow_tavily": True,
        "allow_local_docs": True, "allow_cloud_synthesis": False,
    }
    audit = [{"timestamp": now, "event": "created", "worker": None, "detail": None}]

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO tasks (
            task_id, task_type, library, target_path, chapter_key, priority,
            depends_on, allowed_layer, assigned_capability, source_policy,
            input_json, meta_json,
            status, created_at, updated_at, notes, audit_trail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
        (
            task_id, task_type, library, target_path, chapter_key, priority,
            _json(depends_on or []), allowed_layer,
            _json(assigned_capability or []),
            _json(source_policy or default_policy),
            input_json or "{}",
            meta,
            now, now, notes, _json(audit),
        ),
    )
    conn.commit()
    db.log_event("task_created", task_id=task_id, detail=f"{task_type} for {chapter_key}", conn=conn)

    return read_task(task_id)  # type: ignore[return-value]


def read_task(task_id: str) -> dict[str, Any] | None:
    """Read a task by ID."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return db.dict_from_row(row)


def list_tasks(state: str | None = None) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by state. Ordered by priority then created_at."""
    conn = db.get_connection()
    if state:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority, created_at",
            (state,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY priority, created_at",
        ).fetchall()
    return db.rows_to_dicts(rows)


def transition_task(
    task_id: str,
    new_state: str,
    worker: str | None = None,
    detail: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move a task to a new state. Updates audit trail and timestamps."""
    if new_state not in VALID_STATES:
        raise ValueError(f"Invalid state: {new_state}")

    task = read_task(task_id)
    if task is None:
        raise FileNotFoundError(f"Task not found: {task_id}")

    old_state = task["status"]
    if new_state == old_state:
        return task

    now = _now_iso()
    conn = db.get_connection()

    updates: dict[str, Any] = {"status": new_state, "updated_at": now}
    if new_state == "claimed":
        policy = config.load_policy()
        lease_sec = policy.get("queue_policy", {}).get("claim_timeout_seconds", 600)
        updates["claimed_at"] = now
        updates["assigned_worker"] = worker
        updates["lease_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_sec)
        ).isoformat()
    elif new_state == "running":
        updates["started_at"] = now
    elif new_state in ("complete", "failed"):
        updates["completed_at"] = now
        updates["lease_expires_at"] = None
    if new_state == "failed" and detail:
        updates["failure_reason"] = detail
    if new_state == "queued":
        updates["assigned_worker"] = None
        updates["lease_expires_at"] = None

    if extra_fields:
        for k, v in extra_fields.items():
            if k not in _ALLOWED_EXTRA_COLUMNS:
                continue  # Skip unknown columns to prevent injection
            if k in ("output_artifacts", "depends_on", "assigned_capability", "source_policy", "audit_trail"):
                updates[k] = _json(v)
            else:
                updates[k] = v

    # Append audit entry
    audit = task.get("audit_trail", [])
    if not isinstance(audit, list):
        audit = []
    audit.append({"timestamp": now, "event": f"{old_state} -> {new_state}", "worker": worker, "detail": detail})
    updates["audit_trail"] = _json(audit)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id, old_state]
    result = conn.execute(f"UPDATE tasks SET {set_clause} WHERE task_id = ? AND status = ?", values)
    conn.commit()
    if result.rowcount == 0:
        raise ValueError(f"Task {task_id} state changed during transition (expected {old_state})")

    db.log_event(
        "task_transition", task_id=task_id,
        node_id=worker, detail=f"{old_state} -> {new_state}", conn=conn,
    )
    return read_task(task_id)  # type: ignore[return-value]


# ── Claim / Lease ──────────────────────────────────────────


def claim_task(
    worker_id: str,
    capabilities: list[str],
    allowed_layers: list[str] | None = None,
) -> dict[str, Any] | None:
    """Pull-based claim: find highest-priority queued task this worker can handle.

    Sets a lease expiry — if the worker dies, the lease expires and another
    worker can pick it up. One worker offline never blocks the cluster.
    """
    if allowed_layers is None:
        allowed_layers = ["local", "any"]

    # Release any expired leases first (cheap, keeps queue healthy)
    release_expired_claims()

    queued = list_tasks("queued")
    for task in queued:
        if task.get("allowed_layer") not in allowed_layers:
            continue

        required = task.get("assigned_capability", [])
        if required and not all(cap in capabilities for cap in required):
            continue

        deps = task.get("depends_on", [])
        if deps:
            all_complete = all(
                (t := read_task(dep_id)) is not None and t["status"] == "complete"
                for dep_id in deps
            )
            if not all_complete:
                continue

        return transition_task(task["task_id"], "claimed", worker=worker_id)

    return None


def release_expired_claims(timeout_seconds: int | None = None) -> list[str]:
    """Release tasks whose lease has expired back to queued."""
    released = []
    now = datetime.now(timezone.utc)

    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'claimed' AND lease_expires_at IS NOT NULL",
    ).fetchall()

    for row in rows:
        task = db.dict_from_row(row)
        if task is None:
            continue
        lease_str = task.get("lease_expires_at")
        if not lease_str:
            continue
        lease_time = datetime.fromisoformat(lease_str)
        if now > lease_time:
            transition_task(
                task["task_id"], "queued",
                detail=f"Lease expired (was held by {task.get('assigned_worker', '?')})",
            )
            released.append(task["task_id"])

    return released


# ── Summary ────────────────────────────────────────────────


def get_queue_summary() -> dict[str, int]:
    """Return count of tasks per state."""
    conn = db.get_connection()
    summary = {}
    for state in VALID_STATES:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = ?", (state,),
        ).fetchone()
        summary[state] = row["cnt"] if row else 0
    return summary
