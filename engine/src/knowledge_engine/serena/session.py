"""One Serena process per project, driven over stdio MCP from synchronous code.

Launch (verified against Serena 2.1.0; see ``witness.json``)::

    <KE_SERENA_COMMAND> start-mcp-server --transport stdio --context agent
        --agent-interface tools --enable-web-dashboard false
        --open-web-dashboard false --enable-gui-log-window false
        --log-level WARNING --project <root>

* ``--agent-interface tools`` is forced: the default ``agent`` context
  otherwise exposes only a code-execution REPL tool.
* ``--project`` is always the configured root; the current directory is never
  used to pick a project.
* The child runs with ``SERENA_USAGE_REPORTING=false`` (Serena otherwise
  reports usage to its vendor at startup), with ``HOME`` set to an isolated
  directory under the engine data dir (Serena's global config, logs and
  language-server downloads live there, apart from the operator's own), and
  with the directory of ``uv`` on ``PATH`` (Serena starts pyright via ``uvx``).

Each session runs the MCP client on its own event loop in a background
thread (an anyio blocking portal). :meth:`SerenaSession.call` is thread-safe
and **serialized** per session. On start the session checks the server
against the witness; a mismatch makes the session ``incompatible`` for good
(until the configuration changes) and no tool is ever called. A timeout or a
crash stops the process; the next call restarts it after an exponential
backoff (1, 2, 4 ... 60 s), and calls during the backoff fail with a
retryable ``unavailable`` naming the time left.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..knowledge.errors import (
    IncompatibleError,
    InvalidInputError,
    KnowledgeError,
    KnowledgeTimeoutError,
    UnavailableError,
)
from .config import SerenaConfig, SerenaProject
from .witness import compatibility_problems, load_witness

_logger = logging.getLogger(__name__)

#: Flags of an unattended stdio server; ``--project <root>`` is appended per session.
LAUNCH_ARGS: tuple[str, ...] = (
    "start-mcp-server",
    "--transport",
    "stdio",
    "--context",
    "agent",
    "--agent-interface",
    "tools",
    "--enable-web-dashboard",
    "false",
    "--open-web-dashboard",
    "false",
    "--enable-gui-log-window",
    "false",
    "--log-level",
    "WARNING",
)
#: Environment every Serena child gets on top of the inherited one.
CHILD_ENV: Mapping[str, str] = {"SERENA_USAGE_REPORTING": "false"}
#: Restart delays after consecutive failures (seconds); the last one repeats.
BACKOFF_S: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
#: Seconds :meth:`SerenaSession.close` waits for the process to exit before cancelling.
CLOSE_TIMEOUT_S = 10.0


class SessionState(StrEnum):
    """Observed state of one project's Serena process."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BACKOFF = "backoff"
    INCOMPATIBLE = "incompatible"


@dataclass
class SessionStatus:
    """Diagnostics of one session (JSON-safe via :meth:`as_dict`)."""

    project_id: str
    state: SessionState
    server: str | None = None
    restarts: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    retry_in_s: float | None = None
    problems: list[str] = field(default_factory=list)
    extra_upstream_tools: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.state.value,
            "server": self.server,
            "restarts": self.restarts,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "retry_in_s": self.retry_in_s,
            "problems": list(self.problems),
            "extra_upstream_tools": list(self.extra_upstream_tools),
        }


def child_environment(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a Serena child: inherited, isolated ``HOME``, reporting off, uv on PATH."""
    env = dict(os.environ if base is None else base)
    env.update(CHILD_ENV)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv is None:
        uv = shutil.which("uv")
        if uv is not None:
            env["PATH"] = os.pathsep.join(filter(None, [str(Path(uv).parent), env.get("PATH")]))
        else:
            _logger.warning("uv is not on PATH; Serena starts its Python language server via uvx")
    return env


def _text(result: Any) -> str:
    return "".join(str(c.text) for c in getattr(result, "content", ()) if getattr(c, "text", None))


class _Connection:
    """The live MCP client of one process, owned by a task on the portal's loop."""

    def __init__(self, params: Any, startup_timeout_s: float) -> None:
        self.params = params
        self.startup_timeout_s = startup_timeout_s
        self.client: Any = None
        self.ready: concurrent.futures.Future[tuple[str | None, str | None, dict[str, Any]]] = (
            concurrent.futures.Future()
        )
        self.stop: Any = None

    async def run(self) -> None:
        import anyio  # noqa: PLC0415 - the MCP SDK's event loop library
        from mcp import Client  # noqa: PLC0415 - optional at import time

        self.stop = anyio.Event()
        try:
            async with Client(self.params, read_timeout_seconds=self.startup_timeout_s) as client:
                info = client.server_info
                listed = await client.list_tools()
                schemas = {tool.name: dict(tool.input_schema or {}) for tool in listed.tools}
                self.client = client
                if not self.ready.done():
                    self.ready.set_result(
                        (
                            getattr(info, "name", None),
                            getattr(info, "version", None),
                            schemas,
                        )
                    )
                await self.stop.wait()
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            raise
        finally:
            self.client = None

    async def call(self, tool: str, arguments: dict[str, Any], timeout_s: float) -> Any:
        import anyio  # noqa: PLC0415

        if self.client is None:
            raise ConnectionError("the Serena session is not connected")
        with anyio.fail_after(timeout_s):
            return await self.client.call_tool(tool, arguments)

    async def request_stop(self) -> None:
        if self.stop is not None:
            self.stop.set()


class SerenaSession:
    """One project's Serena process: start, verify, call (serialized), restart, close."""

    def __init__(
        self,
        project: SerenaProject,
        config: SerenaConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        command: tuple[str, ...] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project = project
        self.config = config
        self._clock = clock
        self._command = command if command is not None else config.command
        self._base_env = environment
        self._lock = threading.RLock()
        self._portal_cm: AbstractContextManager[Any] | None = None
        self._portal: Any = None
        self._task: concurrent.futures.Future[Any] | None = None
        self._conn: _Connection | None = None
        self._retry_at = 0.0
        self.status = SessionStatus(project.project_id, SessionState.STOPPED)
        self.last_used = clock()

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done() and self._conn is not None

    def _home(self) -> Path:
        home = self.config.state_dir / "home" / self.project.project_id
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _start(self) -> None:
        """Launch and verify; raises a typed error and leaves the session stopped on failure."""
        from anyio.from_thread import start_blocking_portal  # noqa: PLC0415
        from mcp.client.stdio import StdioServerParameters  # noqa: PLC0415

        if not self._command:
            raise UnavailableError("KE_SERENA_COMMAND is empty")
        executable = shutil.which(self._command[0])
        if executable is None:
            raise UnavailableError(
                f"the Serena executable {self._command[0]!r} was not found",
                details={"hint": "install serena-agent and set KE_SERENA_COMMAND"},
            )
        params = StdioServerParameters(
            command=executable,
            args=[*self._command[1:], *LAUNCH_ARGS, "--project", str(self.project.root)],
            env=child_environment(self._home(), self._base_env),
            cwd=str(self.project.root),
        )
        self.status.state = SessionState.STARTING
        _logger.info(
            "starting Serena for project %s at %s", self.project.project_id, self.project.root
        )
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        conn = _Connection(params, self.config.startup_timeout_s)
        self._task = self._portal.start_task_soon(conn.run)
        try:
            name, version, schemas = conn.ready.result(timeout=self.config.startup_timeout_s)
        except concurrent.futures.TimeoutError as exc:
            self._teardown()
            raise KnowledgeTimeoutError(
                f"Serena did not start within {self.config.startup_timeout_s:g} s",
                details={"project_id": self.project.project_id},
            ) from exc
        except Exception as exc:
            self._teardown()
            raise UnavailableError(
                f"Serena could not be started: {type(exc).__name__}: {exc}",
                details={"project_id": self.project.project_id},
            ) from exc
        self._conn = conn
        try:
            problems = compatibility_problems(
                name, version, schemas, expected_version=self.config.expected_version
            )
            witness = load_witness()
            self.status.server = f"{name} {version}"
            self.status.extra_upstream_tools = sorted(
                set(schemas) - set(witness.tool_schema_sha256) - set(witness.upstream_tools)
            )
        except Exception:
            self._teardown()
            raise
        if problems:
            self._teardown()
            self.status.state = SessionState.INCOMPATIBLE
            self.status.problems = problems
            _logger.error(
                "Serena for project %s is incompatible: %s",
                self.project.project_id,
                "; ".join(problems),
            )
            raise IncompatibleError(
                "the Serena server does not match the recorded compatibility witness",
                details={"project_id": self.project.project_id, "problems": problems},
            )
        self.status.state = SessionState.READY
        self.status.problems = []
        _logger.info("Serena %s ready for project %s", version, self.project.project_id)

    def _teardown(self) -> None:
        """Stop the process and the portal (bounded); never raises."""
        conn, task, portal, portal_cm = self._conn, self._task, self._portal, self._portal_cm
        self._conn = self._task = self._portal = self._portal_cm = None
        try:
            if portal is not None and conn is not None and task is not None and not task.done():
                try:
                    portal.call(conn.request_stop)
                    task.result(timeout=CLOSE_TIMEOUT_S)
                except Exception:  # the process may already be gone
                    _logger.debug("Serena stop was not clean", exc_info=True)
            if task is not None and not task.done():
                task.cancel()
            if portal_cm is not None:
                portal_cm.__exit__(None, None, None)
        except Exception:
            _logger.warning("stopping Serena for %s failed", self.project.project_id, exc_info=True)
        if self.status.state not in (SessionState.INCOMPATIBLE, SessionState.BACKOFF):
            self.status.state = SessionState.STOPPED

    def _fail(self, message: str) -> None:
        self._teardown()
        failures = self.status.consecutive_failures
        delay = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]
        self.status.consecutive_failures = failures + 1
        self.status.last_error = message
        self._retry_at = self._clock() + delay
        self.status.state = SessionState.BACKOFF
        _logger.warning(
            "Serena for project %s failed (%s); restart allowed in %.0f s",
            self.project.project_id,
            message,
            delay,
        )

    def _ensure_running(self) -> None:
        if self.status.state == SessionState.INCOMPATIBLE:
            raise IncompatibleError(
                "the Serena server does not match the recorded compatibility witness",
                details={"project_id": self.project.project_id, "problems": self.status.problems},
            )
        if self.running:
            return
        if self._task is not None:  # the process ended on its own
            self._fail("the Serena process exited")
        wait = self._retry_at - self._clock()
        if wait > 0:
            raise UnavailableError(
                f"Serena for project {self.project.project_id!r} is restarting",
                details={
                    "project_id": self.project.project_id,
                    "retry_in_s": round(wait, 1),
                    "last_error": self.status.last_error,
                },
            )
        if self.status.consecutive_failures:
            self.status.restarts += 1
        try:
            self._start()
        except IncompatibleError:
            raise
        except KnowledgeError as exc:
            self._fail(exc.message)
            raise

    def start(self) -> SessionStatus:
        """Start (or keep) the process and verify it; typed errors as :meth:`call`."""
        with self._lock:
            self.last_used = self._clock()
            self._ensure_running()
        return self.snapshot()

    # ── calls ────────────────────────────────────────────────────

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call ``tool`` and return its parsed JSON result (serialized per session)."""
        with self._lock:
            self.last_used = self._clock()
            self._ensure_running()
            conn, portal = self._conn, self._portal
            if conn is None or portal is None:
                raise UnavailableError("the Serena session is not connected")
            try:
                result = portal.call(conn.call, tool, arguments, self.config.request_timeout_s)
            except TimeoutError as exc:
                self._fail(f"{tool} timed out")
                raise KnowledgeTimeoutError(
                    f"Serena did not answer {tool} within {self.config.request_timeout_s:g} s",
                    details={"project_id": self.project.project_id, "tool": tool},
                ) from exc
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}")
                raise UnavailableError(
                    f"the Serena session for {self.project.project_id!r} failed during {tool}",
                    details={"project_id": self.project.project_id, "tool": tool},
                ) from exc
            self.status.consecutive_failures = 0
            self.last_used = self._clock()
        text = _text(result)
        if getattr(result, "is_error", False):
            raise InvalidInputError(
                f"Serena rejected {tool}: {text[:500]}",
                details={"project_id": self.project.project_id, "tool": tool},
            )
        try:
            return json.loads(text) if text.strip() else None
        except ValueError as exc:
            # Serena answers in prose when a result would be too large ("refine the
            # search"); that is the caller's to act on, not a session failure.
            raise InvalidInputError(
                f"Serena answered {tool} without a result: {text[:500]}",
                details={"project_id": self.project.project_id, "tool": tool, "reason": "text"},
            ) from exc

    def is_idle(self) -> bool:
        """Whether no call is running on this session right now."""
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return True
        return False

    def close(self) -> None:
        """Stop the process (bounded); the session can start again on the next call."""
        with self._lock:
            self._teardown()

    def snapshot(self) -> SessionStatus:
        with self._lock:
            status = replace(
                self.status,
                problems=list(self.status.problems),
                extra_upstream_tools=list(self.status.extra_upstream_tools),
            )
            if status.state == SessionState.BACKOFF:
                status.retry_in_s = round(max(0.0, self._retry_at - self._clock()), 1)
            if status.state == SessionState.READY and not self.running:
                status.state = SessionState.STOPPED
            return status


__all__ = [
    "BACKOFF_S",
    "CHILD_ENV",
    "LAUNCH_ARGS",
    "SerenaSession",
    "SessionState",
    "SessionStatus",
    "child_environment",
]
