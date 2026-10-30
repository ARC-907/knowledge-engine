"""The Serena sessions of all configured projects: routing, the session cap, idle shutdown.

* **Routing** is by configured ``project_id`` only; an unknown project is
  ``invalid_input`` and never falls back to another project or the cwd.
* **Isolation**: one :class:`~.session.SerenaSession` (one process) per
  project; calls to one session are serialized, calls to different projects
  run in parallel and never share a process.
* **Cap**: at most ``KE_SERENA_MAX_SESSIONS`` processes run at once. Starting
  another stops the least recently used idle one; when every running session
  is busy the call fails ``unavailable`` (retryable) instead of queueing.
* **Idle shutdown**: a background reaper stops sessions idle longer than
  ``KE_SERENA_IDLE_SHUTDOWN_S`` (0 disables it).

:func:`run_tool` is the single entry point of the CLI and MCP tools:
allowlist, argument validation, path checks, the call, and normalization
into code locators pinned to the repository state.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..knowledge.errors import KnowledgeError, UnavailableError
from .config import SerenaConfig, SerenaProject
from .repo import RepoStateCache
from .session import SerenaSession
from .tools import normalize, parse_args, upstream_arguments, upstream_tool

_logger = logging.getLogger(__name__)

SessionFactory = Callable[[SerenaProject, SerenaConfig], SerenaSession]


class SerenaManager:
    """Owns the sessions; thread-safe."""

    def __init__(
        self,
        config: SerenaConfig,
        *,
        session_factory: SessionFactory = SerenaSession,
        clock: Callable[[], float] = time.monotonic,
        start_reaper: bool = True,
    ) -> None:
        self.config = config
        self._factory = session_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, SerenaSession] = {}
        self._closed = threading.Event()
        self.repo_states = RepoStateCache()
        self._reaper: threading.Thread | None = None
        if start_reaper and config.idle_shutdown_s > 0:
            self._reaper = threading.Thread(
                target=self._reap_loop, name="serena-idle-reaper", daemon=True
            )
            self._reaper.start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def project(self, project_id: str) -> SerenaProject:
        return self.config.project(project_id)

    def session(self, project_id: str) -> SerenaSession:
        """The session of ``project_id`` (created on first use; may stop an idle one)."""
        if self.closed:
            raise UnavailableError("the Serena adapter is shut down")
        project = self.config.project(project_id)
        with self._lock:
            existing = self._sessions.get(project_id)
            if existing is not None:
                return existing
            self._make_room()
            session = self._factory(project, self.config)
            self._sessions[project_id] = session
            return session

    def _make_room(self) -> None:
        running = [s for s in self._sessions.values() if s.running]
        if len(running) < self.config.max_sessions:
            return
        for candidate in sorted(running, key=lambda s: s.last_used):
            if candidate.is_idle():
                _logger.info(
                    "stopping Serena for %s to stay within %d session(s)",
                    candidate.project.project_id,
                    self.config.max_sessions,
                )
                candidate.close()
                return
        raise UnavailableError(
            f"all {self.config.max_sessions} Serena session(s) are busy",
            details={"max_sessions": self.config.max_sessions},
        )

    def call(self, project_id: str, tool: str, arguments: dict[str, Any]) -> Any:
        upstream_tool(tool)
        return self.session(project_id).call(tool, arguments)

    def check(self, project_id: str | None = None) -> dict[str, Any]:
        """Start each configured project's session (or one) and report compatibility."""
        ids = [project_id] if project_id else sorted(self.config.projects)
        results = []
        for pid in ids:
            entry: dict[str, Any] = {"project_id": pid, "ok": True}
            try:
                entry["session"] = self.session(pid).start().as_dict()
            except KnowledgeError as exc:
                entry["ok"] = False
                entry["error"] = exc.to_dict()
                if pid in self._sessions:
                    entry["session"] = self._sessions[pid].snapshot().as_dict()
            results.append(entry)
        return {"ok": all(r["ok"] for r in results), "projects": results}

    def reap_idle(self) -> list[str]:
        """Stop every session idle for longer than the configured limit."""
        limit = self.config.idle_shutdown_s
        if limit <= 0:
            return []
        now = self._clock()
        with self._lock:
            sessions = list(self._sessions.values())
        stopped = []
        for session in sessions:
            if session.running and now - session.last_used > limit and session.is_idle():
                session.close()
                stopped.append(session.project.project_id)
                _logger.info("stopped idle Serena for %s", session.project.project_id)
        return stopped

    def _reap_loop(self) -> None:
        interval = max(1.0, min(60.0, self.config.idle_shutdown_s / 4))
        while not self._closed.wait(interval):
            try:
                self.reap_idle()
            except Exception:
                _logger.warning("Serena idle reaper failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        """Configuration and per-project session diagnostics (no process is started)."""
        with self._lock:
            sessions = dict(self._sessions)
        projects = []
        for project_id, project in sorted(self.config.projects.items()):
            session = sessions.get(project_id)
            status = (
                session.snapshot().as_dict()
                if session is not None
                else {"project_id": project_id, "state": "stopped"}
            )
            projects.append(
                {
                    **status,
                    "root": str(project.root),
                    "repository_id": project.repository_id,
                }
            )
        return {
            "enabled": self.config.enabled,
            "command": list(self.config.command),
            "expected_version": self.config.expected_version,
            "projects_file": str(self.config.projects_file),
            "max_sessions": self.config.max_sessions,
            "idle_shutdown_s": self.config.idle_shutdown_s,
            "warnings": list(self.config.warnings),
            "projects": projects,
        }

    def close(self) -> None:
        """Stop every session and the reaper (idempotent)."""
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


def run_tool(manager: SerenaManager, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run engine tool ``tool`` (``serena.find_symbol`` ...) and return its JSON result."""
    upstream = upstream_tool(tool)
    args = parse_args(tool, arguments)
    project = manager.project(args.project_id)
    payload = manager.call(args.project_id, upstream, upstream_arguments(args, project.root))
    state = manager.repo_states.get(project.root)
    result = normalize(
        upstream,
        args,
        payload,
        root=project.root,
        repository_id=project.repository_id,
        state=state,
    )
    return result.model_dump(mode="json")


_DEFAULT: SerenaManager | None = None
_DEFAULT_LOCK = threading.Lock()


def default_manager() -> SerenaManager:
    """The process-wide manager (from the environment; ``unavailable`` when disabled)."""
    global _DEFAULT  # noqa: PLW0603 - process default, replaced by set_default_manager
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT.closed:
            config = SerenaConfig.from_env()
            if not config.enabled:
                raise UnavailableError(
                    "the Serena adapter is disabled", details={"hint": "set KE_SERENA_ENABLED=1"}
                )
            _DEFAULT = SerenaManager(config)
        return _DEFAULT


def set_default_manager(manager: SerenaManager | None) -> SerenaManager | None:
    """Install ``manager`` as the process default; return the previous one."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous, _DEFAULT = _DEFAULT, manager
        return previous


def close_default_manager() -> None:
    """Stop the process default manager's sessions (shutdown, tests)."""
    previous = set_default_manager(None)
    if previous is not None:
        previous.close()


atexit.register(close_default_manager)


__all__ = [
    "SerenaManager",
    "close_default_manager",
    "default_manager",
    "run_tool",
    "set_default_manager",
]
