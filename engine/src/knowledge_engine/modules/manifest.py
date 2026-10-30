"""Module manifest version 1: the data a module declares, validated before anything runs.

A module is a directory holding ``module.json`` (or ``module.yaml`` /
``module.yml``) plus whatever its transport needs (a script, nothing for a
service). :func:`load_manifest` reads and validates that file **as data**:
no module code is imported, no process is started and no network is touched
(discovery never executes module code). Validation covers:

* ``manifest_version`` -- only ``1`` is supported; another integer is an
  :class:`~knowledge_engine.knowledge.errors.IncompatibleError`;
* identity -- ``module_id`` (lowercase, dot/dash/underscore namespaced),
  semantic ``version``, ``license`` and ``description``;
* ``engine_compatibility`` -- an engine version range and the contract
  versions the module speaks (checked against the running engine at enable);
* ``transport`` -- ``script`` (argv, no shell), ``service`` (URL from an
  operator setting) or ``mcp`` (stdio argv or a streamable-HTTP URL setting);
* ``tools`` -- stable names, input/output JSON Schemas (checked as schemas),
  an ``effect`` (``read``/``write``/``network``/``process``), ``idempotent``
  and ``long_running`` flags; public MCP names are ``<module_id>.<tool>``;
* ``capabilities`` -- the declared needs; every tool effect and the
  transport's own need must be declared (unknown capabilities are rejected);
* ``configuration`` -- named settings and secret **references** (environment
  variable names). Anything that looks like an embedded credential value is
  rejected;
* ``limits`` and an optional bounded ``health`` probe.

The JSON Schema of the manifest is committed as ``modules/manifest.schema.json``
(:func:`manifest_json_schema`) and pinned by the test suite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from jsonschema import exceptions as jsonschema_exceptions, validators as jsonschema_validators
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..knowledge.contracts import validation_messages
from ..knowledge.errors import IncompatibleError, InvalidInputError

#: The only manifest schema version this engine reads.
MANIFEST_VERSION = 1
#: The script wire protocol version (stdin request / stdout response).
MODULE_PROTOCOL_VERSION = 1
#: Manifest file names, in lookup order.
MANIFEST_FILENAMES = ("module.json", "module.yaml", "module.yml")
#: Largest manifest file read.
MAX_MANIFEST_BYTES = 262_144

MODULE_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SETTING_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,127}$"
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
#: MCP tool names (the SDK's constraint): 1-128 of ``[A-Za-z0-9._-]``.
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Module ids that would shadow built-in tool names or namespaces.
RESERVED_MODULE_IDS = frozenset(
    {
        "search",
        "registry",
        "project_context",
        "board",
        "knowledge",
        "module",
        "modules",
        "jobs",
        "mcp",
        "system",
        "engine",
    }
)

Capability = Literal["read", "write", "network", "process"]
CAPABILITIES: tuple[str, ...] = ("read", "write", "network", "process")

_RANGE_PART_RE = re.compile(r"^\s*(>=|<=|>|<|==)\s*(\d+)\.(\d+)\.(\d+)\s*$")
_SECRET_KEY_RE = re.compile(
    r"(secret|token|passw(or)?d|passwd|api[_-]?key|credential|private[_-]?key|auth)", re.I
)
#: Value shapes of well-known credentials (never valid manifest content).
_SECRET_VALUE_RES = (
    re.compile(r"^(sk|pk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^xox[abprs]-[A-Za-z0-9-]{10,}$"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
)
#: Keys whose values are schemas or prose; secret-looking names there are allowed.
_SCHEMA_KEYS = frozenset({"input_schema", "output_schema", "description"})


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _parse_range(spec: str) -> list[tuple[str, tuple[int, int, int]]]:
    parts = [part for part in spec.split(",") if part.strip()]
    if not parts:
        raise ValueError("an engine range needs at least one comparator, e.g. '>=3.0.0,<4.0.0'")
    out: list[tuple[str, tuple[int, int, int]]] = []
    for part in parts:
        match = _RANGE_PART_RE.match(part)
        if match is None:
            raise ValueError(f"invalid engine range comparator {part.strip()!r}")
        op, major, minor, patch = match.groups()
        out.append((op, (int(major), int(minor), int(patch))))
    return out


def version_tuple(version: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` of a version string, ignoring pre-release/local parts."""
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


class EngineCompatibility(_Model):
    """Which engines and contracts the module supports."""

    engine: str = Field(
        description="Comma-separated comparators on the engine version, e.g. '>=3.0.0,<4.0.0'."
    )
    contract_versions: tuple[int, ...] = Field(
        default=(1,), min_length=1, description="Scoped-knowledge contract versions spoken."
    )
    module_protocol: int = Field(default=MODULE_PROTOCOL_VERSION, ge=1)

    @field_validator("engine")
    @classmethod
    def _valid_range(cls, value: str) -> str:
        _parse_range(value)
        return value

    def accepts_engine(self, version: str) -> bool:
        """True when ``version`` satisfies every comparator (unparseable versions never do)."""
        current = version_tuple(version)
        if current is None:
            return False
        checks = {
            ">=": lambda a, b: a >= b,
            ">": lambda a, b: a > b,
            "<=": lambda a, b: a <= b,
            "<": lambda a, b: a < b,
            "==": lambda a, b: a == b,
        }
        return all(checks[op](current, bound) for op, bound in _parse_range(self.engine))


Argv = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=1024)], ...],
    Field(min_length=1, max_length=64),
]


class ScriptTransport(_Model):
    """Run ``command`` (interpreter + script under the module directory) once per call."""

    kind: Literal["script"] = "script"
    command: Argv = Field(
        description="argv, no shell: an allowlisted interpreter, then a script in the module dir."
    )


class ServiceTransport(_Model):
    """POST the call to an operator-approved HTTP service (URL from configuration)."""

    kind: Literal["service"] = "service"
    url_setting: str = Field(
        pattern=SETTING_NAME_PATTERN,
        description="Configuration setting holding the approved base URL (http/https).",
    )


class McpTransport(_Model):
    """Call tools of an external MCP server (stdio argv or a streamable-HTTP URL setting)."""

    kind: Literal["mcp"] = "mcp"
    connection: Literal["stdio", "streamable_http"]
    command: Argv | None = Field(default=None, description="stdio: argv of the server process.")
    url_setting: str | None = Field(
        default=None,
        pattern=SETTING_NAME_PATTERN,
        description="streamable_http: configuration setting holding the approved URL.",
    )

    @model_validator(mode="after")
    def _endpoint(self) -> McpTransport:
        if self.connection == "stdio" and not self.command:
            raise ValueError("an MCP stdio transport needs 'command'")
        if self.connection == "streamable_http" and not self.url_setting:
            raise ValueError("an MCP streamable_http transport needs 'url_setting'")
        return self


Transport = Annotated[
    ScriptTransport | ServiceTransport | McpTransport, Field(discriminator="kind")
]


def _check_json_schema(schema: Mapping[str, Any], label: str) -> None:
    validator_cls = jsonschema_validators.validator_for(schema)
    try:
        validator_cls.check_schema(dict(schema))
    except jsonschema_exceptions.SchemaError as exc:
        raise ValueError(f"{label} is not a valid JSON Schema: {exc.message}") from exc


class ToolSpec(_Model):
    """One tool of a module."""

    name: str = Field(pattern=TOOL_NAME_PATTERN, description="Stable tool name within the module.")
    description: str = Field(min_length=1, max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect: Capability = Field(description="What the tool does: read, write, network or process.")
    idempotent: bool = Field(default=False, description="Safe to retry automatically.")
    long_running: bool = Field(
        default=False, description="Runs as a job: the call returns a job id immediately."
    )
    artifact_inputs: tuple[Annotated[str, Field(pattern=SETTING_NAME_PATTERN)], ...] = Field(
        default=(),
        description="Input fields holding host artifact ids the host resolves and approves.",
    )
    remote_name: str | None = Field(
        default=None, max_length=128, description="mcp transport: the upstream tool name."
    )
    path: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^/?[A-Za-z0-9._~/-]*$",
        description="service transport: path below the service URL (default /<tool name>).",
    )

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        _check_json_schema(value, "schema")
        return value

    @model_validator(mode="after")
    def _object_input(self) -> ToolSpec:
        if self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: input_schema must have type 'object'")
        props = self.input_schema.get("properties", {})
        for field in self.artifact_inputs:
            if not isinstance(props, Mapping) or field not in props:
                raise ValueError(f"tool {self.name!r}: artifact input {field!r} is not a property")
        return self


class SettingSpec(_Model):
    """A named, non-secret configuration setting."""

    name: str = Field(pattern=SETTING_NAME_PATTERN)
    type: Literal["string", "integer", "boolean", "string_list"] = "string"
    description: str = Field(default="", max_length=2000)
    required: bool = False
    default: str | int | bool | list[str] | None = None


class SecretRef(_Model):
    """A credential the module needs, named by reference only (never a value)."""

    name: str = Field(pattern=SETTING_NAME_PATTERN)
    env: str = Field(
        pattern=ENV_NAME_PATTERN,
        description="Host environment variable that holds the value (the operator may remap it).",
    )
    description: str = Field(default="", max_length=2000)
    required: bool = True
    header: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9-]{1,64}$",
        description="service transport: send the value in this HTTP header.",
    )
    header_prefix: str = Field(default="", max_length=32)


class Configuration(_Model):
    settings: tuple[SettingSpec, ...] = ()
    secrets: tuple[SecretRef, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Configuration:
        names = [s.name for s in self.settings] + [s.name for s in self.secrets]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate configuration names: {', '.join(dupes)}")
        return self


class Limits(_Model):
    """Per-module resource bounds; the host enforces every one of them."""

    deadline_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    concurrency: int = Field(default=2, ge=1, le=64)
    max_input_bytes: int = Field(default=65_536, ge=2, le=16_777_216)
    max_output_bytes: int = Field(default=1_048_576, ge=64, le=67_108_864)
    max_artifact_bytes: int = Field(default=8_388_608, ge=0, le=268_435_456)
    max_artifacts: int = Field(default=16, ge=0, le=1000)
    max_retries: int = Field(
        default=0, ge=0, le=3, description="Automatic retries; only idempotent tools use them."
    )
    job_ttl_s: int = Field(default=3600, ge=60, le=604_800, description="Job result retention.")


class HealthProbe(_Model):
    """A bounded health check, run on request (never during discovery)."""

    command: Argv | None = Field(default=None, description="script: argv of the probe.")
    path: str | None = Field(
        default=None, max_length=512, pattern=r"^/?[A-Za-z0-9._~/-]*$", description="service path."
    )
    timeout_ms: int = Field(default=5000, ge=100, le=30_000)


class ModuleManifest(_Model):
    """Manifest version 1 of a module (see the module docstring)."""

    manifest_version: Literal[1]
    module_id: str = Field(min_length=1, max_length=64, pattern=MODULE_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN, max_length=64)
    license: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    engine_compatibility: EngineCompatibility
    transport: Transport
    tools: tuple[ToolSpec, ...] = Field(min_length=1, max_length=64)
    capabilities: tuple[Capability, ...] = Field(min_length=1)
    configuration: Configuration = Field(default_factory=Configuration)
    limits: Limits = Field(default_factory=Limits)
    health: HealthProbe | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ModuleManifest:
        if self.module_id in RESERVED_MODULE_IDS or self.module_id.split(".")[0] in {
            "knowledge",
            "board",
            "registry",
        }:
            raise ValueError(f"module_id {self.module_id!r} is reserved for built-in tools")
        names = [tool.name for tool in self.tools]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate tool names: {', '.join(dupes)}")
        declared = set(self.capabilities)
        for tool in self.tools:
            if tool.effect not in declared:
                raise ValueError(
                    f"tool {tool.name!r} has effect {tool.effect!r}, "
                    "which is not in the module's capabilities"
                )
            public = mcp_tool_name(self.module_id, tool.name)
            if not MCP_NAME_RE.fullmatch(public):
                raise ValueError(f"public tool name {public!r} violates the MCP name constraint")
        missing = sorted(self.transport_capabilities() - declared)
        if missing:
            raise ValueError(
                f"transport {self.transport.kind!r} needs capabilities {missing} "
                "that the module does not declare"
            )
        settings = {s.name: s for s in self.configuration.settings}
        url_setting = getattr(self.transport, "url_setting", None)
        if url_setting and url_setting not in settings:
            raise ValueError(f"transport url_setting {url_setting!r} is not a declared setting")
        if self.transport.kind != "script":
            for tool in self.tools:
                if tool.artifact_inputs:
                    raise ValueError("artifact inputs are supported by script transports only")
        if self.health is not None:
            if self.transport.kind == "service" and self.health.path is None:
                raise ValueError("a service health probe needs 'path'")
            if self.transport.kind == "script" and self.health.command is None:
                raise ValueError("a script health probe needs 'command'")
        return self

    def transport_capabilities(self) -> set[str]:
        """Capabilities the transport itself needs (process for local, network for remote)."""
        if self.transport.kind == "script":
            return {"process"}
        if self.transport.kind == "service":
            return {"network"}
        return {"process"} if self.transport.connection == "stdio" else {"network"}

    def tool(self, name: str) -> ToolSpec | None:
        return next((tool for tool in self.tools if tool.name == name), None)


def mcp_tool_name(module_id: str, tool: str) -> str:
    """The deterministic public name ``<module_id>.<tool>``."""
    return f"{module_id}.{tool}"


def _iter_strings(value: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield ``(path, key, string value)`` for every string leaf outside schema/prose keys."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name in _SCHEMA_KEYS:
                continue
            where = f"{path}.{name}" if path else name
            if isinstance(item, str):
                yield where, name, item
            else:
                yield from _iter_strings(item, where)
    elif isinstance(value, list):
        key = path.rsplit(".", 1)[-1]
        for index, item in enumerate(value):
            if isinstance(item, str):
                yield f"{path}[{index}]", key, item
            else:
                yield from _iter_strings(item, f"{path}[{index}]")


def find_embedded_secrets(data: Mapping[str, Any]) -> list[str]:
    """Paths of values that look like embedded credentials (never the values themselves).

    A key named like a credential (``token``, ``api_key``, ``password`` ...)
    must hold an environment variable *name*; any value shaped like a known
    credential format is rejected wherever it appears.
    """
    found: list[str] = []
    for path, key, value in _iter_strings(data):
        if not value:
            continue
        if any(pattern.search(value) for pattern in _SECRET_VALUE_RES):
            found.append(path)
            continue
        in_secret_ref = path.startswith("configuration.secrets")
        if (
            _SECRET_KEY_RE.search(key)
            and not in_secret_ref
            and key not in {"name", "header", "header_prefix"}
            and not re.fullmatch(ENV_NAME_PATTERN, value)
        ):
            found.append(path)
    # A secret reference may not carry a value or default field at all (extra="forbid"
    # rejects it later); report it here as an embedded secret for a clear message.
    config = data.get("configuration")
    if isinstance(config, Mapping):
        for index, secret in enumerate(config.get("secrets") or []):
            if isinstance(secret, Mapping) and ({"value", "default"} & set(secret)):
                found.append(f"configuration.secrets[{index}]")
        for index, setting in enumerate(config.get("settings") or []):
            if (
                isinstance(setting, Mapping)
                and _SECRET_KEY_RE.search(str(setting.get("name", "")))
                and setting.get("default") not in (None, "", [])
            ):
                found.append(f"configuration.settings[{index}].default")
    return sorted(set(found))


def parse_manifest(data: Any, *, origin: str = "<manifest>") -> ModuleManifest:
    """Validate manifest ``data`` (already parsed JSON/YAML).

    Raises:
        IncompatibleError: ``manifest_version`` is an integer other than 1.
        InvalidInputError: anything else is wrong (``details.errors`` lists it),
            including an embedded secret value (``details.secret_paths``).
    """
    if not isinstance(data, Mapping):
        raise InvalidInputError(f"{origin}: a manifest must be an object")
    version = data.get("manifest_version")
    if isinstance(version, int) and not isinstance(version, bool) and version != MANIFEST_VERSION:
        raise IncompatibleError(
            f"{origin}: unsupported manifest_version {version} (this engine reads {MANIFEST_VERSION})",
            details={"manifest_version": version, "supported": [MANIFEST_VERSION]},
        )
    secrets = find_embedded_secrets(data)
    if secrets:
        raise InvalidInputError(
            f"{origin}: the manifest embeds secret values; reference environment variables instead",
            details={"secret_paths": secrets},
        )
    try:
        return ModuleManifest.model_validate(dict(data))
    except ValidationError as exc:
        raise InvalidInputError(
            f"{origin}: invalid module manifest", details={"errors": validation_messages(exc)}
        ) from exc


def find_manifest_file(directory: Path) -> Path | None:
    """The manifest file of a module directory, or ``None``."""
    for name in MANIFEST_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def load_manifest(directory: Path) -> ModuleManifest:
    """Read and validate the manifest of ``directory`` (as data; nothing is executed).

    Raises:
        InvalidInputError: no manifest, too large, unparseable or invalid.
        IncompatibleError: unsupported ``manifest_version``.
    """
    path = find_manifest_file(directory)
    if path is None:
        raise InvalidInputError(
            "no module manifest found", details={"expected": list(MANIFEST_FILENAMES)}
        )
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise InvalidInputError(
                "the module manifest is too large", details={"max_bytes": MAX_MANIFEST_BYTES}
            )
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidInputError(f"cannot read the module manifest: {exc.strerror}") from exc
    try:
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            import yaml  # noqa: PLC0415 - YAML manifests only

            data = yaml.safe_load(text)
    except (ValueError, ImportError) as exc:
        raise InvalidInputError(f"{path.name}: not valid {path.suffix[1:].upper()}") from exc
    except Exception as exc:  # yaml.YAMLError and friends
        raise InvalidInputError(f"{path.name}: not valid YAML: {type(exc).__name__}") from exc
    return parse_manifest(data, origin=path.name)


def manifest_digest(manifest: ModuleManifest) -> str:
    """``sha256:<hex>`` of the canonical manifest JSON (frozen-schema identity)."""
    import hashlib  # noqa: PLC0415

    canonical = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manifest_json_schema() -> dict[str, Any]:
    """JSON Schema of manifest version 1 (deterministic; pinned in the repository)."""
    schema = ModuleManifest.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/ARC-907/knowledge-engine/module-manifest-v1.schema.json"
    return schema


__all__ = [
    "CAPABILITIES",
    "MANIFEST_FILENAMES",
    "MANIFEST_VERSION",
    "MODULE_PROTOCOL_VERSION",
    "RESERVED_MODULE_IDS",
    "Capability",
    "Configuration",
    "EngineCompatibility",
    "HealthProbe",
    "Limits",
    "McpTransport",
    "ModuleManifest",
    "ScriptTransport",
    "SecretRef",
    "ServiceTransport",
    "SettingSpec",
    "ToolSpec",
    "find_embedded_secrets",
    "find_manifest_file",
    "load_manifest",
    "manifest_digest",
    "manifest_json_schema",
    "mcp_tool_name",
    "parse_manifest",
    "version_tuple",
]
