"""Agent Board — sweeper.

Background loop that keeps the board healthy:

* Prunes expired (TTL-passed) messages
* Prunes overflow once max_messages_before_prune is exceeded
* Surfaces stale unacked blockers as `reminder` posts (with reply_to → original)
* Emits per-channel `digest` posts on the digest interval
* Logs each pass to `board_sweeps` so ops can verify cadence and find errors

Runs as a daemon thread inside the FastAPI process when
`board_config.sweeper_enabled=1`. Standalone mode (`service.py`) runs the
same loop on its own.

Concurrency: multiple sweepers (embedded + standalone, or multi-worker
uvicorn) coordinate via a singleton lease row in `kv_store`
(`board.sweeper_lease`). Only the lease holder runs a pass; everyone
else short-circuits. The lease auto-expires so a crashed holder doesn't
block the next sweep window.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..foundation import db
from ..pipeline import message_board as mb
from . import store

_logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lock = threading.Lock()


def _holder_id() -> str:
    """Stable per-process identifier for the sweeper lease.

    Hostname + PID + a 4-char random suffix so two processes on the same
    machine still distinguish themselves. The suffix is generated once
    per process import; the lease holder string remains stable across
    sweep passes (necessary for renewal).
    """
    host = "unknown"
    try:
        host = socket.gethostname()
    except OSError:
        pass
    return f"{host}:{os.getpid()}:{_HOLDER_SUFFIX}"


_HOLDER_SUFFIX = uuid.uuid4().hex[:4]


# ── Lifecycle ──────────────────────────────────────────────────


def start() -> bool:
    """Start the sweeper thread if not already running. Idempotent."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_run, name="agent-board-sweeper", daemon=True,
        )
        _thread.start()
        _logger.info("agent-board sweeper started")
        return True


def stop(timeout: float = 5.0) -> bool:
    """Signal the sweeper to stop. Returns True if it exited within timeout.

    Best-effort releases the lease so a peer sweeper can pick up promptly
    instead of waiting for the TTL to expire.
    """
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            return True
        _stop_event.set()
        _thread.join(timeout=timeout)
        exited = not _thread.is_alive()
        if exited:
            _thread = None
    try:
        db.release_sweeper_lease(_holder_id())
    except Exception:  # noqa: BLE001
        pass
    return exited


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


# ── One pass (also exposed for the manual `/board/sweep` endpoint) ─


def _sweep_active_db() -> dict[str, Any]:
    """Run the maintenance pass against whatever DB is currently active.

    "Active" = the default board DB, or a scope DB if called inside
    ``db.using_db(...)``. Reads that DB's own ``board_config`` so each scope
    keeps independent retention / reminder / digest cadence, and records the
    pass into that DB's ``board_sweeps`` for per-scope observability.
    """
    cfg = store.load_config()
    started_at = _now_iso()
    pruned_expired = 0
    pruned_overflow = 0
    reminders_emitted = 0
    digests_emitted = 0
    error: str | None = None
    try:
        pruned_expired = store.prune_expired()
        pruned_overflow = store.prune_by_count(int(cfg["max_messages_before_prune"]))
        reminders_emitted = _emit_stale_blocker_reminders(
            int(cfg["stale_blocker_hours"])
        )
        digests_emitted = _maybe_emit_digests(
            int(cfg["digest_interval_minutes"]), cfg.get("channels") or [],
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        _logger.exception("agent-board sweep failed")

    finished_at = _now_iso()
    store.record_sweep(
        started_at=started_at, finished_at=finished_at,
        pruned_expired=pruned_expired, pruned_overflow=pruned_overflow,
        reminders_emitted=reminders_emitted, digests_emitted=digests_emitted,
        error=error,
    )
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "pruned_expired": pruned_expired,
        "pruned_overflow": pruned_overflow,
        "reminders_emitted": reminders_emitted,
        "digests_emitted": digests_emitted,
        "error": error,
    }


def sweep_once(force: bool = False) -> dict[str, Any]:
    """Run a single sweep pass across the default DB **and every scope DB**.

    One process-wide lease (on the default DB's `kv_store`) guards the whole
    pass, so multiple sweepers (embedded + standalone, or multi-worker
    uvicorn) never double-sweep. The lease holder then sweeps the default
    board and each known per-scope database in turn, each under its own
    `using_db` context so it reads that scope's config and records into that
    scope's `board_sweeps`.

    `force=True` bypasses the lease — used by the on-demand HTTP / MCP
    triggers where the operator wants an immediate pass regardless of who
    holds the lease.

    Returns an aggregate with a per-target breakdown.
    """
    cfg = store.load_config()  # default DB config governs the lease TTL
    started_at = _now_iso()
    holder = _holder_id()
    have_lease = True
    lease_ttl = max(30, int(cfg.get("sweep_interval_s") or 60) * 2)

    if not force:
        try:
            have_lease = db.acquire_sweeper_lease(holder, ttl_seconds=lease_ttl)
        except Exception:  # noqa: BLE001
            # If the lease layer itself errors, fall through and run —
            # missing a sweep is worse than a possible double-pass.
            _logger.exception("acquire_sweeper_lease failed; running anyway")
            have_lease = True

    if not have_lease:
        return {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "skipped": True,
            "reason": "another sweeper holds the lease",
            "targets_swept": 0,
            "pruned_expired": 0,
            "pruned_overflow": 0,
            "reminders_emitted": 0,
            "digests_emitted": 0,
            "error": None,
        }

    # Sweep the default board first, then every scope DB.
    targets: list[dict[str, Any]] = []
    default_result = _sweep_active_db()
    default_result["target"] = "(default)"
    targets.append(default_result)

    try:
        from . import scopes
        scope_entries = scopes.list_scopes()
    except Exception:  # noqa: BLE001
        _logger.exception("listing scopes failed; swept default only")
        scope_entries = []

    for entry in scope_entries:
        path = str(entry["db_path"])
        try:
            with db.using_db(path):
                r = _sweep_active_db()
            r["target"] = f"scope:{entry['scope']}"
            targets.append(r)
        except Exception:  # noqa: BLE001
            _logger.exception("sweep of scope %s failed; continuing", entry.get("scope"))
            targets.append({"target": f"scope:{entry.get('scope')}", "error": "sweep failed"})

    agg = {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "skipped": False,
        "targets_swept": len(targets),
        "pruned_expired": sum(int(t.get("pruned_expired", 0)) for t in targets),
        "pruned_overflow": sum(int(t.get("pruned_overflow", 0)) for t in targets),
        "reminders_emitted": sum(int(t.get("reminders_emitted", 0)) for t in targets),
        "digests_emitted": sum(int(t.get("digests_emitted", 0)) for t in targets),
        "error": next((t["error"] for t in targets if t.get("error")), None),
        "targets": targets,
    }
    return agg


# ── Loop body ──────────────────────────────────────────────────


def _run() -> None:
    """The thread target."""
    # Default initialization makes the loop robust to a first-iteration
    # exception in load_config — without it, `interval` would only be
    # bound inside the try block.
    interval = 60
    while not _stop_event.is_set():
        try:
            cfg = store.load_config()
            interval = max(5, int(cfg.get("sweep_interval_s") or 60))
            if cfg.get("sweeper_enabled"):
                sweep_once()
        except Exception:  # noqa: BLE001
            _logger.exception("sweeper loop error; continuing")
            interval = max(interval, 60)
        # Sleep in small slices so stop() responds quickly.
        slept = 0.0
        while slept < interval and not _stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5


def _emit_stale_blocker_reminders(threshold_hours: int) -> int:
    """For each stale unacked blocker, post one `reminder` message.

    Idempotency: a reminder's `reply_to` references the original blocker's
    message_id. We use a single `MAX(created_at) GROUP BY reply_to`
    aggregation to dedup against the latest reminder per blocker, so the
    sweeper doesn't re-emit until another `threshold_hours` window has
    passed.

    `threshold_hours` is clamped to a minimum of 1 — without the floor, a
    misconfigured `stale_blocker_hours=0` would treat every blocker as
    "stale," producing reminder spam that never expires (the reminder's
    own TTL would also collapse to zero, meaning "no expiry").
    """
    threshold_hours = max(1, threshold_hours)
    blockers = store.get_unacked_blockers(threshold_hours=threshold_hours)
    if not blockers:
        return 0

    conn = db.get_connection()
    # One indexed scan instead of a Python loop over 500 rows.
    last_per_blocker: dict[str, str] = {}
    rows = conn.execute(
        """SELECT reply_to, MAX(created_at) AS last_at
             FROM messages
            WHERE message_type = 'reminder'
              AND reply_to IS NOT NULL
            GROUP BY reply_to"""
    ).fetchall()
    for r in rows:
        if r["reply_to"]:
            last_per_blocker[r["reply_to"]] = r["last_at"]

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=threshold_hours)
    ).isoformat()
    emitted = 0
    for b in blockers:
        last_at = last_per_blocker.get(b["message_id"])
        if last_at and last_at > cutoff:
            continue
        try:
            mb.post_message(
                message_type="reminder",
                sender_node_id="board-sweeper",
                channel=b.get("channel") or "ops",
                task_id=b.get("task_id"),
                product_id=b.get("product_id"),
                subject=f"Stale blocker: {b.get('subject') or b['message_id'][:8]}",
                body=json.dumps({
                    "blocker_id": b["message_id"],
                    "blocker_subject": b.get("subject"),
                    "blocker_age_hours": _age_hours(b.get("created_at")),
                    "original_sender": b.get("sender_node_id"),
                    "task_id": b.get("task_id"),
                }),
                visibility_scope="all",
                reply_to=b["message_id"],
                correlation_id=b.get("correlation_id") or b["message_id"],
                # threshold floor of 1 guarantees a positive TTL.
                ttl_hours=threshold_hours * 2,
            )
            emitted += 1
        except Exception:  # noqa: BLE001
            _logger.exception("failed to emit reminder for %s", b["message_id"])
    return emitted


def _maybe_emit_digests(interval_minutes: int, channels: list[str]) -> int:
    """If at least `interval_minutes` have passed since the last digest per
    channel, emit a new one. Returns the count emitted in this pass.
    """
    if interval_minutes <= 0 or not channels:
        return 0

    conn = db.get_connection()
    last_digest_per_channel: dict[str, str] = {}
    rows = conn.execute(
        """SELECT channel, MAX(created_at) AS last_at
             FROM messages
            WHERE message_type = 'digest'
              AND sender_node_id = 'board-sweeper'
            GROUP BY channel"""
    ).fetchall()
    for r in rows:
        if r["channel"]:
            last_digest_per_channel[r["channel"]] = r["last_at"]

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=interval_minutes)
    ).isoformat()
    emitted = 0
    for channel in channels:
        last = last_digest_per_channel.get(channel)
        if last and last > cutoff:
            continue
        try:
            # `since=last or cutoff` widens the first-after-boot digest
            # so a freshly-restarted process still summarises pre-restart
            # activity in that channel.
            since = last or cutoff
            summary = store.digest(channel=channel, since=since, max_messages=200)
            # Skip empty windows — no point spamming a quiet channel.
            if summary.get("scanned", 0) == 0:
                continue
            mb.post_message(
                message_type="digest",
                sender_node_id="board-sweeper",
                channel=channel,
                subject=f"Digest — {channel} ({interval_minutes}m window)",
                body=json.dumps(summary, indent=2),
                visibility_scope="all",
                ttl_hours=max(interval_minutes // 60 + 1, 24),
            )
            emitted += 1
        except Exception:  # noqa: BLE001
            _logger.exception("failed to emit digest for channel %s", channel)
    return emitted


# ── Helpers ────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - then).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


__all__ = ["start", "stop", "is_running", "sweep_once"]
