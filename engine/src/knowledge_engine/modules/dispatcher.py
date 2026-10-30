"""The module host: one lifecycle manager and dispatcher shared by CLI, HTTP and MCP.

:class:`ModuleHost` implements the distinct operations of the modular
utility contract (V2-DESIGN "Modular utility contract"):

* **discover** -- read the manifests under ``KE_MODULES_DIR`` as data;
* **register** (install) -- validate a module directory's manifest and record
  it (never from inside the watched corpus; registering never enables);
* **configure** -- operator settings plus secret *references*;
* **enable** / **disable** -- intent; enable checks compatibility,
  configuration, host capability grants and public-name collisions; disable
  rejects new work at once and drains/cancels active calls within a deadline;
* **invoke** -- the invocation sequence below;
* **jobs** -- status / cancel / list of long-running calls, scoped by workspace.

Invocation sequence (each step can only narrow what the caller asked for):

1. resolve the enabled module and tool; capture ``catalog_version`` and a run id;
2. validate input size, input schema and scope; resolve approved input
   artifacts; intersect requested effects with the operator's grants and the
   host policy (``KE_MODULES_HOST_CAPABILITIES``) -- read-only is enforced by
   the allowed effects, not by a flag;
3. take a concurrency slot (none free: ``unavailable``), set the deadline;
4. run the transport (script argv without a shell, approved service/MCP URL);
5. validate the output schema, normalize and store artifacts (budgets enforced);
6. optionally hand artifacts to host ingestion with an explicit destination scope;
7. record a sanitized audit row (no inputs, outputs or credentials) and log it.

Only declared-idempotent tools are retried, at most ``limits.max_retries``
times and only after retryable failures. Long-running tools return a scoped
job id at once; the job keeps its concurrency slot, honours the deadline and
can be cancelled (which stops the module's whole process group).

Credentials: secret values are read from the host environment at call time
and passed to the module only. Every string the host returns or logs is
scrubbed of them, and an artifact containing one is rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import exceptions as jsonschema_exceptions, validators as jsonschema_validators
from pydantic import ValidationError

from .. import __version__
from ..knowledge.contracts import CONTRACT_VERSION, RecordScope, canonical_json, validation_messages
from ..knowledge.errors import (
    CancelledError,
    DeniedError,
    IncompatibleError,
    InternalError,
    InvalidInputError,
    InvalidScopeError,
    KnowledgeError,
    KnowledgeTimeoutError,
    OutputInvalidError,
    UnavailableError,
    UnknownScopeError,
)
from . import transports
from .artifacts import ArtifactPayload, ArtifactStore, NormalizedArtifact, normalize_artifacts
from .catalog import CatalogEntry, ModuleCatalog, now_iso
from .config import ModulesConfig
from .ingestion import DerivationInput, EvidenceIngestion, UnconfiguredIngestion
from .manifest import (
    ENV_NAME_PATTERN,
    MODULE_PROTOCOL_VERSION,
    find_embedded_secrets,
    find_manifest_file,
    load_manifest,
    mcp_tool_name,
)

_ENV_RE = re.compile(ENV_NAME_PATTERN)
_logger = logging.getLogger(__name__)
_audit = logging.getLogger("knowledge_engine.modules.audit")

#: MCP tools the module group itself owns (reserved; never a module tool name).
JOB_TOOLS = ("module_job_status", "module_job_cancel")
#: Built-in MCP names a module can never take.
RESERVED_PUBLIC_NAMES = frozenset(
    {"search", "registry_list", "registry_toggle", "registry_get", *JOB_TOOLS}
)
#: Lifecycle states (observed health).
STATES = ("registered", "disabled", "starting", "ready", "degraded", "failed", "stopped")
#: Job states.
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "expired")
_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})
#: How often a running call re-reads catalog intent / cancel flags (seconds).
INTENT_POLL_S = 0.5
#: Default drain deadline of :meth:`ModuleHost.disable`.
DEFAULT_DRAIN_S = 5.0
#: Most module directories :meth:`ModuleHost.discover` reads.
MAX_DISCOVERED = 500
#: Shortest secret value that is scrubbed (shorter values would mangle output).
_MIN_SCRUB_LEN = 4


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def scrub(value: Any, secret_values: Iterable[str]) -> Any:
    """Replace every secret value inside strings of ``value`` (recursively) with ``***``."""
    needles = sorted({s for s in secret_values if len(s) >= _MIN_SCRUB_LEN}, key=len, reverse=True)
    if not needles:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for needle in needles:
                item = item.replace(needle, "***")
            return item
        if isinstance(item, Mapping):
            return {walk(k): walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(value)


def _scrubbed_error(exc: KnowledgeError, secret_values: Iterable[str]) -> KnowledgeError:
    message = scrub(exc.message, secret_values)
    details = scrub(exc.details, secret_values)
    if message == exc.message and details == exc.details:
        return exc
    clean = type(exc)(message, details=details)
    clean.__cause__ = None
    return clean


@dataclass
class _Call:
    """An active call: its cancel flag (disable/cancel set it)."""

    run_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    job_id: str | None = None


@dataclass
class _Prepared:
    """A validated invocation, ready to run."""

    entry: CatalogEntry
    tool_name: str
    arguments: dict[str, Any]
    scope: RecordScope
    scope_explicit: bool
    timeout_s: float
    secret_values: dict[str, str]
    input_artifacts: dict[str, dict[str, Any]]
    parents: dict[str, NormalizedArtifact]
    policy: dict[str, Any]
    params_digest: str
    catalog_version: int


class ModuleHost:
    """Catalog + lifecycle + dispatcher for one process (thread-safe)."""

    def __init__(
        self,
        catalog: ModuleCatalog,
        config: ModulesConfig,
        *,
        ingestion: EvidenceIngestion | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        engine_version: str = __version__,
    ) -> None:
        self.catalog = catalog
        self.config = config
        self.ingestion: EvidenceIngestion = ingestion or UnconfiguredIngestion()
        self.artifacts = ArtifactStore(config.artifact_dir)
        self._environ = environ
        self._clock = clock
        self.engine_version = engine_version
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], threading.BoundedSemaphore] = {}
        self._active: dict[str, dict[str, _Call]] = {}
        self._jobs: dict[str, _Call] = {}
        self._threads: set[threading.Thread] = set()
        self._closed = False

    @classmethod
    def open(cls, config: ModulesConfig, **kwargs: Any) -> ModuleHost:
        """Open the catalog of ``config`` and build a host around it."""
        return cls(ModuleCatalog.open(config.db_path), config, **kwargs)

    # ── lifecycle of the host ────────────────────────────────────

    def start(self) -> dict[str, str]:
        """Rebuild observed health: enabled modules go starting -> ready or failed.

        A module that fails its checks only loses its own availability.
        Returns ``{module_id: state}``.
        """
        states: dict[str, str] = {}
        for entry in self.catalog.entries():
            if not entry.enabled:
                state = "disabled" if entry.configured else "registered"
                self.catalog.set_health(entry.module_id, state)
                states[entry.module_id] = state
                continue
            self.catalog.set_health(entry.module_id, "starting")
            try:
                self._check_enable(entry, entry.grants, others=self._enabled_names(entry.module_id))
            except KnowledgeError as exc:
                _logger.warning(
                    "module %s failed to start: %s (%s)", entry.module_id, exc.message, exc.code
                )
                self.catalog.set_health(entry.module_id, "failed", f"{exc.code}: {exc.message}")
                states[entry.module_id] = "failed"
                continue
            self.catalog.set_health(entry.module_id, "ready")
            states[entry.module_id] = "ready"
        return states

    def close(self, drain_s: float = DEFAULT_DRAIN_S) -> None:
        """Cancel active calls and jobs, wait up to ``drain_s``, mark enabled modules stopped."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            calls = [c for group in self._active.values() for c in group.values()]
            threads = list(self._threads)
        for call in calls:
            call.cancel.set()
        deadline = time.monotonic() + drain_s
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            for entry in self.catalog.entries():
                if entry.enabled:
                    self.catalog.set_health(entry.module_id, "stopped")
        except KnowledgeError:
            _logger.debug("could not record stopped modules", exc_info=True)
        self.catalog.close()

    # ── discovery and registration ───────────────────────────────

    def _require_modules_dir(self) -> Path:
        if self.config.modules_dir is None:
            raise InvalidInputError(
                "no module directory is configured", details={"setting": "KE_MODULES_DIR"}
            )
        return self.config.modules_dir

    def discover(self) -> list[dict[str, Any]]:
        """Manifests under ``KE_MODULES_DIR``, validated as data (nothing is executed)."""
        root = self._require_modules_dir()
        if not root.is_dir():
            raise UnavailableError("the module directory does not exist")
        registered = {e.module_id: e for e in self.catalog.entries()}
        out: list[dict[str, Any]] = []
        for child in sorted(root.iterdir())[:MAX_DISCOVERED]:
            if (
                not child.is_dir()
                or child.name.startswith(".")
                or find_manifest_file(child) is None
            ):
                continue
            item: dict[str, Any] = {"directory": child.name}
            try:
                manifest = load_manifest(child)
            except KnowledgeError as exc:
                item.update(valid=False, error=exc.to_dict())
            else:
                entry = registered.get(manifest.module_id)
                item.update(
                    valid=True,
                    module_id=manifest.module_id,
                    version=manifest.version,
                    transport=manifest.transport.kind,
                    registered=entry is not None,
                    registered_version=entry.manifest.version if entry else None,
                )
            out.append(item)
        return out

    def _check_location(self, directory: Path) -> Path:
        try:
            resolved = directory.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidInputError("the module directory does not exist") from exc
        if not resolved.is_dir():
            raise InvalidInputError("a module path must be a directory")
        corpus = self.config.corpus_root.resolve()
        if resolved == corpus or resolved.is_relative_to(corpus):
            raise DeniedError(
                "module directories inside the watched corpus are never executable; "
                "use KE_MODULES_DIR or another operator directory"
            )
        return resolved

    def register(self, path: str | Path) -> dict[str, Any]:
        """Install the module at ``path`` (validated as data). It stays disabled."""
        directory = self._check_location(Path(path))
        manifest = load_manifest(directory)
        entry, created = self.catalog.upsert(manifest, directory)
        _audit.info(
            "module %s %s %s from %s",
            entry.module_id,
            manifest.version,
            "registered" if created else "re-registered (disabled)",
            directory,
        )
        return {"created": created, **self._describe(entry)}

    def register_named(self, directory: str) -> dict[str, Any]:
        """Register a directory of ``KE_MODULES_DIR`` by name (the HTTP form; no free paths)."""
        root = self._require_modules_dir()
        if not directory or "/" in directory or "\\" in directory or directory in {".", ".."}:
            raise InvalidInputError("directory must be a plain directory name")
        return self.register(root / directory)

    # ── configuration and intent ─────────────────────────────────

    def configure(
        self,
        module_id: str,
        settings: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Store operator settings and secret references (env var names only)."""
        entry = self.catalog.get(module_id)
        specs = {s.name: s for s in entry.manifest.configuration.settings}
        refs = {s.name: s for s in entry.manifest.configuration.secrets}
        clean: dict[str, Any] = {}
        for name, value in (settings or {}).items():
            spec = specs.get(name)
            if spec is None:
                if name in refs:
                    raise InvalidInputError(
                        f"{name!r} is a secret; configure it as a reference (--secret {name}=ENV_VAR)"
                    )
                raise InvalidInputError(
                    f"unknown setting {name!r}", details={"known": sorted(specs)}
                )
            clean[name] = _coerce_setting(name, spec.type, value)
        leaked = find_embedded_secrets({"settings": clean})
        if leaked:
            raise InvalidInputError(
                "settings must not contain credentials; use secret references",
                details={"secret_paths": leaked},
            )
        secret_env: dict[str, str] = {}
        for name, env in (secrets or {}).items():
            if name not in refs:
                raise InvalidInputError(
                    f"unknown secret reference {name!r}", details={"known": sorted(refs)}
                )
            if not isinstance(env, str) or not _ENV_RE.fullmatch(env):
                raise InvalidInputError(
                    f"secret {name!r} must name an environment variable (A-Z, 0-9, _), not a value"
                )
            secret_env[name] = env
        updated = self.catalog.set_configuration(module_id, clean, secret_env)
        if not updated.enabled:
            self.catalog.set_health(module_id, "disabled")
        _audit.info(
            "module %s configured (settings: %s)", module_id, ", ".join(sorted(clean)) or "-"
        )
        return self._describe(updated)

    def _enabled_names(self, exclude: str) -> set[str]:
        names: set[str] = set()
        for other in self.catalog.entries():
            if other.enabled and other.module_id != exclude:
                names.update(mcp_tool_name(other.module_id, t.name) for t in other.manifest.tools)
        return names

    def _effective_settings(self, entry: CatalogEntry) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for spec in entry.manifest.configuration.settings:
            if spec.name in entry.settings:
                values[spec.name] = entry.settings[spec.name]
            elif spec.default is not None:
                values[spec.name] = spec.default
        return values

    def _env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def _secret_values(self, entry: CatalogEntry, *, required_only: bool = False) -> dict[str, str]:
        env = self._env()
        values: dict[str, str] = {}
        missing: list[str] = []
        for ref in entry.manifest.configuration.secrets:
            var = entry.secret_env.get(ref.name, ref.env)
            value = env.get(var, "")
            if value:
                values[ref.name] = value
            elif ref.required:
                missing.append(var)
        if missing and not required_only:
            raise UnavailableError(
                "required secret references are not set in the host environment",
                details={"env": sorted(missing)},
            )
        return values

    def _check_enable(
        self, entry: CatalogEntry, grants: Iterable[str], *, others: set[str]
    ) -> tuple[str, ...]:
        """Compatibility, configuration, grants and collisions. Returns the effective grants."""
        manifest = entry.manifest
        compat = manifest.engine_compatibility
        if not compat.accepts_engine(self.engine_version):
            raise IncompatibleError(
                f"module {entry.module_id} needs engine {compat.engine}; this is {self.engine_version}",
                details={"engine": self.engine_version, "requires": compat.engine},
            )
        if CONTRACT_VERSION not in compat.contract_versions:
            raise IncompatibleError(
                "module does not speak this engine's contract version",
                details={"contract_version": CONTRACT_VERSION},
            )
        if compat.module_protocol != MODULE_PROTOCOL_VERSION:
            raise IncompatibleError(
                "unsupported module protocol", details={"supported": MODULE_PROTOCOL_VERSION}
            )
        settings = self._effective_settings(entry)
        missing = [
            s.name for s in manifest.configuration.settings if s.required and s.name not in settings
        ]
        if missing:
            raise InvalidInputError(
                "required settings are not configured", details={"settings": sorted(missing)}
            )
        self._secret_values(entry)
        granted = frozenset(grants)
        unknown = granted - set(manifest.capabilities)
        if unknown:
            raise DeniedError(
                "grants must be capabilities the module declares",
                details={"unknown": sorted(unknown)},
            )
        over = granted - self.config.host_capabilities
        if over:
            raise DeniedError(
                "the host policy does not allow these capabilities",
                details={"denied": sorted(over), "setting": "KE_MODULES_HOST_CAPABILITIES"},
            )
        need = manifest.transport_capabilities() - granted
        if need:
            raise DeniedError(
                f"the {manifest.transport.kind} transport needs capabilities that are not granted",
                details={"missing": sorted(need)},
            )
        public = {mcp_tool_name(entry.module_id, t.name) for t in manifest.tools}
        collisions = sorted((public & RESERVED_PUBLIC_NAMES) | (public & others))
        if collisions:
            raise InvalidInputError("public tool names collide", details={"collisions": collisions})
        kind = manifest.transport.kind
        if kind == "script":
            transports.resolve_argv(
                tuple(getattr(manifest.transport, "command", ())), entry.source_dir
            )
        elif (
            kind == "service" or getattr(manifest.transport, "connection", "") == "streamable_http"
        ):
            transports.service_url(manifest, settings, "")
        else:
            transports.resolve_argv(
                tuple(getattr(manifest.transport, "command", ()) or ()), entry.source_dir
            )
        return tuple(sorted(granted))

    def enable(self, module_id: str, grants: Iterable[str] | None = None) -> dict[str, Any]:
        """Check and enable a module. Advertised MCP tools change on the next restart (ADR 0009)."""
        entry = self.catalog.get(module_id)
        wanted = (
            frozenset(grants)
            if grants is not None
            else frozenset(entry.manifest.capabilities) & self.config.host_capabilities
        )
        self.catalog.set_health(module_id, "starting")
        try:
            effective = self._check_enable(entry, wanted, others=self._enabled_names(module_id))
        except KnowledgeError as exc:
            self.catalog.set_health(module_id, "failed", f"{exc.code}: {exc.message}")
            _audit.info("module %s enable refused: %s", module_id, exc.code)
            raise
        updated = self.catalog.set_enabled(module_id, True, effective)
        self.catalog.set_health(module_id, "ready")
        _audit.info("module %s enabled (grants: %s)", module_id, ", ".join(effective))
        return {**self._describe(updated), "restart_required_for_mcp": True}

    def disable(self, module_id: str, drain_s: float = DEFAULT_DRAIN_S) -> dict[str, Any]:
        """Reject new work now, cancel active calls/jobs, wait up to ``drain_s`` for them."""
        updated = self.catalog.set_enabled(module_id, False)
        with self._lock:
            calls = list(self._active.get(module_id, {}).values())
        for call in calls:
            call.cancel.set()
            if call.job_id:
                self.catalog.request_cancel(call.job_id)
        deadline = time.monotonic() + max(drain_s, 0.0)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active.get(module_id):
                    break
            time.sleep(0.02)
        with self._lock:
            remaining = len(self._active.get(module_id, {}))
        self.catalog.set_health(module_id, "disabled")
        _audit.info(
            "module %s disabled (cancelled %d active, %d still draining)",
            module_id,
            len(calls),
            remaining,
        )
        return {**self._describe(updated), "cancelled": len(calls), "draining": remaining}

    # ── inspection ───────────────────────────────────────────────

    def _describe(self, entry: CatalogEntry) -> dict[str, Any]:
        health = self.catalog.health().get(entry.module_id, {})
        with self._lock:
            active = len(self._active.get(entry.module_id, {}))
        return {
            **entry.public(),
            "state": health.get("state", "registered"),
            "state_detail": health.get("detail", ""),
            "observed_at": health.get("observed_at"),
            "active_calls": active,
        }

    def list_modules(self) -> list[dict[str, Any]]:
        return [self._describe(entry) for entry in self.catalog.entries()]

    def status(self, module_id: str, *, probe: bool = False) -> dict[str, Any]:
        """Describe a module; ``probe`` runs its bounded health check (enabled modules only)."""
        entry = self.catalog.get(module_id)
        result: dict[str, Any] = {}
        if probe:
            if not entry.enabled:
                raise UnavailableError(f"module {module_id} is not enabled")
            secret_values: dict[str, str] = {}
            try:
                secret_values = self._secret_values(entry)
                transports.probe(
                    entry.manifest, entry.source_dir, self._effective_settings(entry), secret_values
                )
            except KnowledgeError as exc:
                clean = _scrubbed_error(exc, secret_values.values())
                self.catalog.set_health(module_id, "degraded", f"{clean.code}: {clean.message}")
                result["probe"] = {"ok": False, "error": clean.to_dict()}
            else:
                self.catalog.set_health(module_id, "ready")
                result["probe"] = {"ok": True}
        return {
            **self._describe(entry),
            **result,
            "catalog_version": self.catalog.catalog_version(),
            "recent_runs": self.catalog.list_runs(module_id, limit=10),
        }

    def advertised_tools(self) -> list[tuple[str, CatalogEntry, str]]:
        """``(public name, entry, tool)`` of every ready module's granted tools (MCP)."""
        health = self.catalog.health()
        out: list[tuple[str, CatalogEntry, str]] = []
        for entry in self.catalog.entries():
            if not entry.enabled or health.get(entry.module_id, {}).get("state") not in {
                "ready",
                "degraded",
            }:
                continue
            allowed = set(entry.grants) & self.config.host_capabilities
            out.extend(
                (mcp_tool_name(entry.module_id, tool.name), entry, tool.name)
                for tool in entry.manifest.tools
                if tool.effect in allowed
            )
        return sorted(out, key=lambda item: item[0])

    # ── invocation ───────────────────────────────────────────────

    def _scope(self, scope: Any) -> tuple[RecordScope, bool]:
        if scope is None:
            return RecordScope(workspace_id=self.config.default_workspace), False
        if isinstance(scope, RecordScope):
            return scope, True
        try:
            return RecordScope.model_validate(scope), True
        except ValidationError as exc:
            raise InvalidScopeError(
                "invalid scope", details={"errors": validation_messages(exc)}
            ) from exc

    def _prepare(
        self,
        module_id: str,
        tool_name: str,
        arguments: Any,
        *,
        scope: Any,
        timeout_ms: int | None,
        effects: Iterable[str] | None,
        expected_digest: str | None,
    ) -> _Prepared:
        catalog_version = self.catalog.catalog_version()
        entry = self.catalog.get(module_id)
        if not entry.enabled:
            raise UnavailableError(
                f"module {module_id} is disabled", details={"module_id": module_id}
            )
        if expected_digest is not None and expected_digest != entry.manifest_digest:
            raise UnavailableError(
                f"module {module_id} changed since this session started; restart the server",
                details={"restart_required": True},
            )
        state = self.catalog.health().get(module_id, {}).get("state")
        if state == "failed":
            raise UnavailableError(f"module {module_id} failed to start", details={"state": state})
        tool = entry.manifest.tool(tool_name)
        if tool is None:
            raise InvalidInputError(f"module {module_id} has no tool {tool_name!r}")
        record_scope, explicit = self._scope(scope)
        if not isinstance(arguments, dict):
            raise InvalidInputError("tool input must be a JSON object")
        limits = entry.manifest.limits
        size = len(canonical_json(arguments).encode("utf-8"))
        if size > limits.max_input_bytes:
            raise InvalidInputError(
                f"tool input is {size} bytes; the limit is {limits.max_input_bytes}"
            )
        validator_cls = jsonschema_validators.validator_for(tool.input_schema)
        errors = sorted(
            validator_cls(tool.input_schema).iter_errors(arguments), key=lambda e: list(e.path)
        )
        if errors:
            raise InvalidInputError(
                "tool input does not match its schema",
                details={"errors": [_schema_error(e) for e in errors[:10]]},
            )
        # Policy: effects the caller allows ∩ operator grants ∩ host policy.
        granted = set(entry.grants) & self.config.host_capabilities
        requested = set(effects) if effects is not None else set(granted)
        allowed = requested & granted
        need = entry.manifest.transport_capabilities() - granted
        policy = {
            "granted": sorted(granted),
            "requested": sorted(requested),
            "allowed_effects": sorted(allowed),
            "effect": tool.effect,
        }
        if need:
            raise DeniedError(
                "the host policy no longer grants what the transport needs",
                details={"missing": sorted(need), "policy": policy},
            )
        if tool.effect not in allowed:
            raise DeniedError(
                f"tool effect {tool.effect!r} is not allowed for this call",
                details={"policy": policy},
            )
        input_artifacts: dict[str, dict[str, Any]] = {}
        parents: dict[str, NormalizedArtifact] = {}
        for field_name in tool.artifact_inputs:
            artifact_id = arguments.get(field_name)
            if artifact_id is None:
                continue
            if not isinstance(artifact_id, str):
                raise InvalidInputError(f"{field_name} must be an artifact id")
            artifact = self.catalog.get_artifact(artifact_id)
            if artifact is None or not _scope_allows(record_scope, artifact.scope):
                raise UnknownScopeError(
                    f"artifact {artifact_id!r} is not available in this scope",
                    details={"field": field_name},
                )
            if artifact.byte_length > limits.max_artifact_bytes:
                raise InvalidInputError("the input artifact exceeds the module's artifact budget")
            input_artifacts[artifact_id] = {
                "path": str(self.artifacts.path_for(artifact.digest)),
                "media_type": artifact.media_type,
                "digest": artifact.digest,
                "byte_length": artifact.byte_length,
                "module_id": artifact.module_id,
                "canonical_uri": artifact.canonical_uri,
            }
            parents[artifact_id] = artifact
        deadline_ms = (
            limits.deadline_ms if timeout_ms is None else min(int(timeout_ms), limits.deadline_ms)
        )
        if deadline_ms < 1:
            raise InvalidInputError("timeout_ms must be positive")
        settings = self._effective_settings(entry)
        secret_values = self._secret_values(entry)
        return _Prepared(
            entry=entry,
            tool_name=tool_name,
            arguments=arguments,
            scope=record_scope,
            scope_explicit=explicit,
            timeout_s=deadline_ms / 1000,
            secret_values=secret_values,
            input_artifacts=input_artifacts,
            parents=parents,
            policy=policy,
            params_digest=_digest({"tool": tool_name, "input": arguments, "settings": settings}),
            catalog_version=catalog_version,
        )

    def _slot(self, entry: CatalogEntry) -> threading.BoundedSemaphore:
        key = (entry.module_id, entry.manifest_digest)
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = threading.BoundedSemaphore(entry.manifest.limits.concurrency)
                self._slots[key] = slot
            return slot

    def invoke(
        self,
        module_id: str,
        tool_name: str,
        arguments: Any,
        *,
        scope: Any = None,
        surface: str = "library",
        principal: str = "",
        timeout_ms: int | None = None,
        effects: Iterable[str] | None = None,
        ingest: bool = False,
        expected_digest: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Run one tool call (see the module docstring). Raises typed ``KnowledgeError``s.

        A long-running tool returns ``{"status": "accepted", "job": {...}}``
        immediately unless ``wait`` is true (the CLI waits: its process owns the job).
        """
        if self._closed:
            raise UnavailableError("the module host is shut down")
        started = time.monotonic()
        started_at = now_iso()
        run_id = "run_" + _secrets.token_hex(16)
        try:
            prepared = self._prepare(
                module_id,
                tool_name,
                arguments,
                scope=scope,
                timeout_ms=timeout_ms,
                effects=effects,
                expected_digest=expected_digest,
            )
            if ingest and not prepared.scope_explicit:
                raise InvalidInputError("ingestion needs an explicit destination scope")
        except KnowledgeError as exc:
            self._audit_refusal(module_id, tool_name, surface, principal, run_id, exc, started_at)
            raise
        slot = self._slot(prepared.entry)
        if not slot.acquire(blocking=False):
            busy = UnavailableError(
                f"module {module_id} is at its concurrency limit "
                f"({prepared.entry.manifest.limits.concurrency}); retry later",
                details={"concurrency": prepared.entry.manifest.limits.concurrency},
            )
            self._audit_refusal(module_id, tool_name, surface, principal, run_id, busy, started_at)
            raise busy
        call = _Call(run_id)
        with self._lock:
            self._active.setdefault(module_id, {})[run_id] = call
        tool = prepared.entry.manifest.tool(tool_name)
        assert tool is not None  # noqa: S101 - checked in _prepare
        if tool.long_running:
            try:
                return self._start_job(
                    prepared, call, slot, surface, principal, started, started_at, ingest, wait
                )
            except Exception:
                self._release(module_id, call, slot)
                raise
        try:
            return self._execute(prepared, call, surface, principal, started, started_at, ingest)
        finally:
            self._release(module_id, call, slot)

    def _release(self, module_id: str, call: _Call, slot: threading.BoundedSemaphore) -> None:
        with self._lock:
            group = self._active.get(module_id, {})
            group.pop(call.run_id, None)
            if not group:
                self._active.pop(module_id, None)
        slot.release()

    def _should_cancel(self, prepared: _Prepared, call: _Call) -> Callable[[], bool]:
        last = [0.0]

        def check() -> bool:
            if call.cancel.is_set():
                return True
            now = time.monotonic()
            if now - last[0] < INTENT_POLL_S:
                return False
            last[0] = now
            try:
                if not self.catalog.is_enabled(prepared.entry.module_id) or (
                    call.job_id and self.catalog.cancel_requested(call.job_id)
                ):
                    call.cancel.set()
            except KnowledgeError:
                return False
            return call.cancel.is_set()

        return check

    def _attempt(self, prepared: _Prepared, call: _Call, remaining_s: float) -> dict[str, Any]:
        tool = prepared.entry.manifest.tool(prepared.tool_name)
        assert tool is not None  # noqa: S101
        request = transports.CallRequest(
            manifest=prepared.entry.manifest,
            tool=tool,
            source_dir=prepared.entry.source_dir,
            arguments=prepared.arguments,
            settings=self._effective_settings(prepared.entry),
            secrets=prepared.secret_values,
            run_id=call.run_id,
            scope=prepared.scope,
            timeout_s=remaining_s,
            now=self._clock().isoformat(timespec="seconds"),
            input_artifacts=prepared.input_artifacts,
            should_cancel=self._should_cancel(prepared, call),
        )
        return transports.run_transport(request)

    def _execute(
        self,
        prepared: _Prepared,
        call: _Call,
        surface: str,
        principal: str,
        started: float,
        started_at: str,
        ingest: bool,
    ) -> dict[str, Any]:
        entry = prepared.entry
        tool = entry.manifest.tool(prepared.tool_name)
        assert tool is not None  # noqa: S101
        max_attempts = 1 + (entry.manifest.limits.max_retries if tool.idempotent else 0)
        secret_values = list(prepared.secret_values.values())
        attempts = 0
        artifacts: list[ArtifactPayload] = []
        ingestion: dict[str, Any] | None = None
        try:
            while True:
                attempts += 1
                remaining = prepared.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise _timeout(prepared)
                try:
                    raw = self._attempt(prepared, call, remaining)
                    break
                except KnowledgeError as exc:
                    if call.cancel.is_set() and not isinstance(exc, CancelledError):
                        raise CancelledError("the call was cancelled") from exc
                    if not exc.retryable or attempts >= max_attempts or call.cancel.is_set():
                        raise
                    _logger.info(
                        "module %s.%s attempt %d failed (%s); retrying (idempotent)",
                        entry.module_id,
                        prepared.tool_name,
                        attempts,
                        exc.code,
                    )
            output = scrub(raw.get("output"), secret_values)
            validator_cls = jsonschema_validators.validator_for(tool.output_schema)
            error = jsonschema_exceptions.best_match(
                validator_cls(tool.output_schema).iter_errors(output)
            )
            if error is not None:
                raise OutputInvalidError(
                    "the module output does not match its schema",
                    details={"error": scrub(_schema_error(error), secret_values)},
                )
            artifacts = normalize_artifacts(
                raw.get("artifacts"),
                module_id=entry.module_id,
                module_version=entry.manifest.version,
                run_id=call.run_id,
                scope=prepared.scope,
                max_artifacts=entry.manifest.limits.max_artifacts,
                max_artifact_bytes=entry.manifest.limits.max_artifact_bytes,
                known_parents=prepared.parents,
                now=self._clock(),
            )
            for payload in artifacts:
                if any(
                    s.encode("utf-8") in payload.content
                    for s in secret_values
                    if len(s) >= _MIN_SCRUB_LEN
                ):
                    raise OutputInvalidError("an artifact contains a configured credential")
            for payload in artifacts:
                self.artifacts.put(payload.artifact.digest, payload.content)
                self.catalog.put_artifact(payload.artifact)
            if ingest:
                ingestion = self._ingest(prepared, call, artifacts)
        except KnowledgeError as exc:
            clean = _scrubbed_error(exc, secret_values)
            self._audit_run(
                prepared, call, surface, principal, started, started_at, attempts, clean, []
            )
            if clean is exc:
                raise
            raise clean from None
        except Exception as exc:
            _logger.exception(
                "module %s.%s crashed in the host", entry.module_id, prepared.tool_name
            )
            internal = InternalError("internal error while running the module")
            self._audit_run(
                prepared, call, surface, principal, started, started_at, attempts, internal, []
            )
            raise internal from exc
        result = {
            "status": "ok",
            "run_id": call.run_id,
            "module_id": entry.module_id,
            "module_version": entry.manifest.version,
            "tool": prepared.tool_name,
            "scope": prepared.scope.model_dump(mode="json"),
            "output": output,
            "artifacts": [p.artifact.model_dump(mode="json") for p in artifacts],
            "ingestion": ingestion,
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "provenance": {
                "params_digest": prepared.params_digest,
                "parent_artifact_ids": sorted(prepared.parents),
                "catalog_version": prepared.catalog_version,
            },
        }
        self._audit_run(
            prepared,
            call,
            surface,
            principal,
            started,
            started_at,
            attempts,
            None,
            [p.artifact.artifact_id for p in artifacts],
        )
        return result

    def _ingest(
        self, prepared: _Prepared, call: _Call, artifacts: list[ArtifactPayload]
    ) -> dict[str, Any]:
        if not artifacts:
            raise OutputInvalidError("ingestion was requested but the module returned no artifacts")
        parent_ids = sorted(prepared.parents)
        derivation = DerivationInput(
            run_id=call.run_id,
            module_id=prepared.entry.module_id,
            module_version=prepared.entry.manifest.version,
            parameters_digest=prepared.params_digest,
            parent_artifact_ids=tuple(parent_ids),
            parent_revision_ids=tuple(
                sorted(set(self.ingestion.revisions_for(parent_ids).values()))
            ),
        )
        receipt = self.ingestion.ingest(
            artifacts, destination=prepared.scope, derivation=derivation
        )
        return receipt.model_dump(mode="json")

    # ── jobs ─────────────────────────────────────────────────────

    def _start_job(
        self,
        prepared: _Prepared,
        call: _Call,
        slot: threading.BoundedSemaphore,
        surface: str,
        principal: str,
        started: float,
        started_at: str,
        ingest: bool,
        wait: bool,
    ) -> dict[str, Any]:
        entry = prepared.entry
        job_id = "job_" + _secrets.token_hex(16)
        call.job_id = job_id
        now = self._clock()
        ttl = timedelta(seconds=entry.manifest.limits.job_ttl_s)
        job = {
            "job_id": job_id,
            "run_id": call.run_id,
            "module_id": entry.module_id,
            "tool": prepared.tool_name,
            "workspace_id": prepared.scope.workspace_id,
            "project_id": prepared.scope.project_id,
            "owner": surface,
            "state": "running",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=prepared.timeout_s) + ttl).isoformat(),
        }
        self.catalog.create_job(job)

        def work() -> None:
            try:
                result = self._execute(
                    prepared, call, surface, principal, started, started_at, ingest
                )
            except KnowledgeError as exc:
                state = "cancelled" if isinstance(exc, CancelledError) else "failed"
                self.catalog.finish_job(
                    job_id, state, error=exc.to_dict(), expires_at=(self._clock() + ttl).isoformat()
                )
            except Exception:
                _logger.exception("module job %s crashed", job_id)
                self.catalog.finish_job(
                    job_id, "failed", error=InternalError("internal error").to_dict()
                )
            else:
                self.catalog.finish_job(
                    job_id, "succeeded", result=result, expires_at=(self._clock() + ttl).isoformat()
                )
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)
                    self._threads.discard(threading.current_thread())
                self._release(entry.module_id, call, slot)

        with self._lock:
            self._jobs[job_id] = call
        if wait:
            with self._lock:
                self._threads.add(threading.current_thread())
            work()
            return self.job_status(job_id, prepared.scope.workspace_id)
        thread = threading.Thread(target=work, name=f"ke-module-job-{job_id[-8:]}", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return {
            "status": "accepted",
            "run_id": call.run_id,
            "module_id": entry.module_id,
            "module_version": entry.manifest.version,
            "tool": prepared.tool_name,
            "job": self._public_job(self.catalog.get_job(job_id) or job),
        }

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "cancel_requested"} | {
            "cancel_requested": bool(job.get("cancel_requested"))
        }

    def _scoped_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        self.catalog.expire_jobs(self._clock().isoformat())
        job = self.catalog.get_job(job_id)
        if job is None or job["workspace_id"] != workspace_id:
            raise UnknownScopeError(f"job {job_id!r} is not known in this workspace")
        return job

    def job_status(self, job_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """A job's state (and result when finished). Unknown in this workspace: ``unknown_scope``."""
        return self._public_job(
            self._scoped_job(job_id, workspace_id or self.config.default_workspace)
        )

    def job_cancel(
        self, job_id: str, workspace_id: str | None = None, *, wait_s: float = 2.0
    ) -> dict[str, Any]:
        """Request cancellation (reaches the owning process); wait briefly when owned here."""
        workspace = workspace_id or self.config.default_workspace
        job = self._scoped_job(job_id, workspace)
        if job["state"] in _TERMINAL_JOB_STATES:
            return self._public_job(job)
        self.catalog.request_cancel(job_id)
        with self._lock:
            call = self._jobs.get(job_id)
        if call is not None:
            call.cancel.set()
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                current = self.catalog.get_job(job_id)
                if current is None or current["state"] in _TERMINAL_JOB_STATES:
                    break
                time.sleep(0.02)
        _audit.info("module job %s cancel requested", job_id)
        return self.job_status(job_id, workspace)

    def job_list(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        self.catalog.expire_jobs(self._clock().isoformat())
        jobs = self.catalog.list_jobs(workspace_id or self.config.default_workspace)
        return [self._public_job({**j, "result": None}) for j in jobs]

    # ── audit ────────────────────────────────────────────────────

    def _audit_run(
        self,
        prepared: _Prepared,
        call: _Call,
        surface: str,
        principal: str,
        started: float,
        started_at: str,
        attempts: int,
        error: KnowledgeError | None,
        artifact_ids: list[str],
    ) -> None:
        elapsed = int((time.monotonic() - started) * 1000)
        outcome = "ok" if error is None else error.code
        row = {
            "run_id": call.run_id,
            "module_id": prepared.entry.module_id,
            "module_version": prepared.entry.manifest.version,
            "tool": prepared.tool_name,
            "surface": surface,
            "principal": principal,
            "workspace_id": prepared.scope.workspace_id,
            "project_id": prepared.scope.project_id,
            "outcome": outcome,
            "error_code": None if error is None else error.code,
            "elapsed_ms": elapsed,
            "attempts": max(attempts, 1),
            "catalog_version": prepared.catalog_version,
            "policy_json": json.dumps(prepared.policy, sort_keys=True),
            "params_digest": prepared.params_digest,
            "artifact_ids_json": json.dumps(artifact_ids),
            "job_id": call.job_id,
            "started_at": started_at,
            "finished_at": now_iso(),
        }
        try:
            self.catalog.record_run(row)
        except KnowledgeError:
            _logger.warning("could not record module run %s", call.run_id)
        _audit.info(
            "module run %s %s.%s v%s surface=%s outcome=%s attempts=%d elapsed_ms=%d artifacts=%d "
            "workspace=%s catalog_version=%d",
            call.run_id,
            prepared.entry.module_id,
            prepared.tool_name,
            prepared.entry.manifest.version,
            surface,
            outcome,
            max(attempts, 1),
            elapsed,
            len(artifact_ids),
            prepared.scope.workspace_id,
            prepared.catalog_version,
        )

    def _audit_refusal(
        self,
        module_id: str,
        tool_name: str,
        surface: str,
        principal: str,
        run_id: str,
        exc: KnowledgeError,
        started_at: str,
    ) -> None:
        _audit.info(
            "module run %s %s.%s surface=%s principal=%s refused=%s at=%s",
            run_id,
            module_id,
            tool_name,
            surface,
            principal or "-",
            exc.code,
            started_at,
        )


def _timeout(prepared: _Prepared) -> KnowledgeError:
    return KnowledgeTimeoutError(f"the call exceeded its deadline of {prepared.timeout_s:g}s")


def _scope_allows(requested: RecordScope, owned: RecordScope) -> bool:
    """An artifact is approved for a call in its workspace, and in its project (or workspace-wide)."""
    if requested.workspace_id != owned.workspace_id:
        return False
    return owned.project_id is None or owned.project_id == requested.project_id


def _schema_error(error: jsonschema_exceptions.ValidationError) -> str:
    where = "/".join(str(p) for p in error.absolute_path)
    return f"{where}: {error.message}" if where else error.message


def _coerce_setting(name: str, kind: str, value: Any) -> Any:
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "integer":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.lower() in {"true", "1", "yes"}
    if kind == "string_list":
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    raise InvalidInputError(f"setting {name!r} must be a {kind}")


# ── process default host ─────────────────────────────────────────────

_DEFAULT: ModuleHost | None = None
_DEFAULT_LOCK = threading.Lock()


def default_host() -> ModuleHost:
    """The process-wide host (opened from the environment on first use and started)."""
    global _DEFAULT  # noqa: PLW0603 - process default, replaced by set_default_host
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT.catalog.closed:
            host = ModuleHost.open(ModulesConfig.from_env())
            host.start()
            _DEFAULT = host
        return _DEFAULT


def set_default_host(host: ModuleHost | None) -> ModuleHost | None:
    """Install ``host`` as the process default (the app lifespan does); return the previous one."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous, _DEFAULT = _DEFAULT, host
        return previous


def close_default_host() -> None:
    """Close the process default host, if one was opened."""
    previous = set_default_host(None)
    if previous is not None:
        previous.close()


__all__ = [
    "JOB_STATES",
    "JOB_TOOLS",
    "RESERVED_PUBLIC_NAMES",
    "STATES",
    "ModuleHost",
    "close_default_host",
    "default_host",
    "scrub",
    "set_default_host",
]
