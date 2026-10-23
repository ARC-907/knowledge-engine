"""Agent Board — sweeper.

Background loop that keeps the board healthy:

* Prunes expired (TTL-passed) messages
* Prunes overflow once max_messages_before_prune is exceeded
* Surfaces stale unacked blockers as `reminder` posts (with reply_to → original)
* Emits per-channel `digest` posts on the digest interval
* Logs each pass to `board_sweeps` so ops can verify cadence and find errors

Runs as a daemon thread inside the FastAPI process when
`board_config.sweeper_enabled=1`. Standalone mode (`service.py`) runs the
same loop on its own. The `KE_BOARD_SWEEPER` env var, when set, overrides
the persisted flag at boot (`0`/`false`/`no`/`off` keep the thread from
starting; `1`/`true`/`yes`/`on` start it; anything else is reported and
ignored, see `core.settings`).

The loop is owned by a :class:`Sweeper` object. Each app lifespan builds
one with its settings (and the store module it sweeps) and installs it as
the process default, so the module-level ``start()``/``stop()``/
``is_running()``/``sweep_once()`` wrappers -- used by the MCP sweep tool,
the CLI and the tests -- always act on the sweeper the running app owns.

Concurrency: multiple sweepers (embedded + standalone, or multi-worker
uvicorn) coordinate via a singleton lease row in `kv_store`
(`board.sweeper_lease`). Only the lease holder runs a pass; everyone
else short-circuits. The lease auto-expires so a crashed holder doesn't
block the next sweep window.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import TYPE_CHECKING, Any

from ..runtime.foundation import db
from ..runtime.pipeline import message_board as mb
from . import scopes, store

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

SWEEPER_ENV = "KE_BOARD_SWEEPER"
THREAD_NAME = "agent-board-sweeper"


def _holder_id() -> str:
    """Stable per-process identifier for the sweeper lease.

    Hostname + PID + a 4-char random suffix so two processes on the same
    machine still distinguish themselves. The suffix is generated once
    per process import; the lease holder string remains stable across
    sweep passes (necessary for renewal).
    """
    host = "unknown"
    with contextlib.suppress(OSError):
        host = socket.gethostname()
    return f"{host}:{os.getpid()}:{_HOLDER_SUFFIX}"


_HOLDER_SUFFIX = uuid.uuid4().hex[:4]


def env_override(settings: Settings | None = None) -> bool | None:
    """The `KE_BOARD_SWEEPER` boot override: None when unset, else on/off."""
    if settings is None:
        from ..core.settings import Settings  # noqa: PLC0415 - keep import light

        settings = Settings()
    return settings.board_sweeper


# ── The sweeper object ─────────────────────────────────────────


class Sweeper:
    """One background sweeper loop (start / stop / is_running / sweep_once).

    ``store_module`` is the board store facade the passes run against
    (default: :mod:`knowledge_engine.agent_board.store`); ``settings``
    supplies the ``KE_BOARD_SWEEPER`` override (default: read the
    environment when asked). Every thread gets its own stop event, so a
    thread that outlives :meth:`stop`'s join timeout can never be revived
    by a later :meth:`start`.
    """

    def __init__(
        self,
        *,
        store_module: ModuleType | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store_module if store_module is not None else store
        self.settings = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── policy ──
    def env_override(self) -> bool | None:
        return env_override(self.settings)

    def should_run(self) -> bool:
        """Boot decision: the env override when set, else the persisted flag."""
        override = self.env_override()
        if override is not None:
            if not override:
                _logger.info("board sweeper disabled by %s", SWEEPER_ENV)
            return override
        try:
            cfg = self.store.load_config()
        except Exception:
            _logger.exception("could not read board config; assuming sweeper_enabled=1")
            return True
        return bool(cfg.get("sweeper_enabled", True))

    # ── lifecycle ──
    def start(self) -> bool:
        """Start the sweeper thread if not already running. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(self._stop_event,),
                name=THREAD_NAME,
                daemon=True,
            )
            self._thread.start()
            _logger.info("agent-board sweeper started")
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the sweeper to stop. Returns True if it exited within timeout.

        Best-effort releases the lease so a peer sweeper can pick up promptly
        instead of waiting for the TTL to expire.
        """
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            self._stop_event.set()
            thread.join(timeout=timeout)
            exited = not thread.is_alive()
            if exited:
                self._thread = None
        if exited:
            _logger.info("agent-board sweeper stopped")
        else:
            _logger.warning("agent-board sweeper did not stop within %.1fs", timeout)
        try:
            db.release_sweeper_lease(_holder_id())
        except Exception:
            _logger.debug("releasing the sweeper lease failed; peers wait for TTL", exc_info=True)
        return exited

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def reset(self, timeout: float = 5.0) -> None:
        """Stop and drop all thread state (test helper; see :func:`reset_for_tests`)."""
        self.stop(timeout=timeout)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                _logger.warning(
                    "agent-board sweeper did not stop within %.1fs; abandoning it", timeout
                )
            self._stop_event.set()
            self._thread = None
            self._stop_event = threading.Event()

    # ── passes ──
    def sweep_once(self, force: bool = False) -> dict[str, Any]:
        """One pass over the default DB and every scope DB (see :func:`sweep_once`)."""
        return _sweep_all(self.store, force=force)

    def _run(self, stop_event: threading.Event) -> None:
        _run_loop(self, stop_event)


# ── Process default (thin module-level API) ────────────────────

_default_lock = threading.Lock()
_default: Sweeper | None = None


def default_sweeper() -> Sweeper:
    """The process-wide sweeper the module-level helpers act on."""
    global _default  # noqa: PLW0603  # process default instance, swapped by app lifespans
    with _default_lock:
        if _default is None:
            _default = Sweeper()
        return _default


def install_default(sweeper: Sweeper | None) -> Sweeper | None:
    """Make ``sweeper`` the process default; return the previous one.

    App lifespans install the sweeper they own and restore the previous one
    on shutdown. ``None`` resets to a lazily created plain instance.
    """
    global _default  # process default instance, swapped by app lifespans
    with _default_lock:
        previous, _default = _default, sweeper
        return previous


def start() -> bool:
    """Start the default sweeper thread if not already running. Idempotent."""
    return default_sweeper().start()


def stop(timeout: float = 5.0) -> bool:
    """Stop the default sweeper. Returns True if it exited within timeout."""
    return default_sweeper().stop(timeout=timeout)


def is_running() -> bool:
    return default_sweeper().is_running()


def reset_for_tests(timeout: float = 5.0) -> None:
    """Stop the default sweeper and drop its state. Test helper.

    Test suites call this between tests so a sweeper started by one app
    lifespan can never post into (or hold a connection to) the next test's
    database. A thread that misses the join timeout keeps its own, already
    set, stop event and exits after its current pass.
    """
    default_sweeper().reset(timeout=timeout)


# ── One pass (also exposed for the manual `/board/sweep` endpoint) ─


def _sweep_active_db(store_mod: ModuleType = store) -> dict[str, Any]:
    """Run the maintenance pass against whatever DB is currently active.

    "Active" = the default board DB, or a scope DB if called inside
    ``db.using_db(...)``. Reads that DB's own ``board_config`` so each scope
    keeps independent retention / reminder / digest cadence, and records the
    pass into that DB's ``board_sweeps`` for per-scope observability.
    """
    cfg = store_mod.load_config()
    started_at = _now_iso()
    pruned_expired = 0
    pruned_overflow = 0
    reminders_emitted = 0
    digests_emitted = 0
    error: str | None = None
    try:
        pruned_expired = store_mod.prune_expired()
        pruned_overflow = store_mod.prune_by_count(int(cfg["max_messages_before_prune"]))
        reminders_emitted = _emit_stale_blocker_reminders(
            int(cfg["stale_blocker_hours"]), store_mod
        )
        digests_emitted = _maybe_emit_digests(
            int(cfg["digest_interval_minutes"]),
            cfg.get("channels") or [],
            store_mod,
        )
    except Exception as exc:
        # The full traceback goes to the log; the stored/returned summary names
        # only the exception type so a /board/sweep response never carries
        # internal details (paths, SQL, stack text).
        error = f"sweep failed ({type(exc).__name__}); see the server log"
        _logger.exception("agent-board sweep failed")

    finished_at = _now_iso()
    store_mod.record_sweep(
        started_at=started_at,
        finished_at=finished_at,
        pruned_expired=pruned_expired,
        pruned_overflow=pruned_overflow,
        reminders_emitted=reminders_emitted,
        digests_emitted=digests_emitted,
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

    Returns an aggregate with a per-target breakdown. Runs against the
    default sweeper's store.
    """
    return default_sweeper().sweep_once(force=force)


def _sweep_all(store_mod: ModuleType, *, force: bool) -> dict[str, Any]:
    cfg = store_mod.load_config()  # default DB config governs the lease TTL
    started_at = _now_iso()
    holder = _holder_id()
    have_lease = True
    lease_ttl = max(30, int(cfg.get("sweep_interval_s") or 60) * 2)

    if not force:
        try:
            have_lease = db.acquire_sweeper_lease(holder, ttl_seconds=lease_ttl)
        except Exception:
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
    default_result = _sweep_active_db(store_mod)
    default_result["target"] = "(default)"
    targets.append(default_result)

    try:
        scope_entries = scopes.list_scopes()
    except Exception:
        _logger.exception("listing scopes failed; swept default only")
        scope_entries = []

    for entry in scope_entries:
        path = str(entry["db_path"])
        try:
            with db.using_db(path):
                r = _sweep_active_db(store_mod)
            r["target"] = f"scope:{entry['scope']}"
            targets.append(r)
        except Exception:
            _logger.exception("sweep of scope %s failed; continuing", entry.get("scope"))
            targets.append({"target": f"scope:{entry.get('scope')}", "error": "sweep failed"})

    return {
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


# ── Loop body ──────────────────────────────────────────────────


def _run_loop(sweeper: Sweeper, stop_event: threading.Event) -> None:
    """The thread target. Loops until ``stop_event`` is set."""
    # Default initialization makes the loop robust to a first-iteration
    # exception in load_config — without it, `interval` would only be
    # bound inside the try block.
    interval = 60
    while not stop_event.is_set():
        try:
            cfg = sweeper.store.load_config()
            interval = max(5, int(cfg.get("sweep_interval_s") or 60))
            if cfg.get("sweeper_enabled"):
                sweeper.sweep_once()
        except Exception:
            _logger.exception("sweeper loop error; continuing")
            interval = max(interval, 60)
        # Sleep in small slices so stop() responds quickly.
        slept = 0.0
        while slept < interval and not stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5


def _emit_stale_blocker_reminders(threshold_hours: int, store_mod: ModuleType = store) -> int:
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
    blockers = store_mod.get_unacked_blockers(threshold_hours=threshold_hours)
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

    cutoff = (datetime.now(UTC) - timedelta(hours=threshold_hours)).isoformat()
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
                body=json.dumps(
                    {
                        "blocker_id": b["message_id"],
                        "blocker_subject": b.get("subject"),
                        "blocker_age_hours": _age_hours(b.get("created_at")),
                        "original_sender": b.get("sender_node_id"),
                        "task_id": b.get("task_id"),
                    }
                ),
                visibility_scope="all",
                reply_to=b["message_id"],
                correlation_id=b.get("correlation_id") or b["message_id"],
                # threshold floor of 1 guarantees a positive TTL.
                ttl_hours=threshold_hours * 2,
            )
            emitted += 1
        except Exception:
            _logger.exception("failed to emit reminder for %s", b["message_id"])
    return emitted


def _maybe_emit_digests(
    interval_minutes: int, channels: list[str], store_mod: ModuleType = store
) -> int:
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

    cutoff = (datetime.now(UTC) - timedelta(minutes=interval_minutes)).isoformat()
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
            summary = store_mod.digest(channel=channel, since=since, max_messages=200)
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
        except Exception:
            _logger.exception("failed to emit digest for channel %s", channel)
    return emitted


# ── Helpers ────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return round((datetime.now(UTC) - then).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


__all__ = [
    "SWEEPER_ENV",
    "Sweeper",
    "default_sweeper",
    "env_override",
    "install_default",
    "is_running",
    "reset_for_tests",
    "start",
    "stop",
    "sweep_once",
]
