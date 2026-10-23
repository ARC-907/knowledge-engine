"""Typed process settings: the single environment layer for the engine.

Every ``KE_*`` variable the package reads is declared on :class:`Settings`
with its exact name, type and default (plus the three ``OLLAMA_*`` variables
the Ollama provider has always honoured and the ``BOARD_HOST``/``BOARD_PORT``
defaults of ``board-serve``). The HTTP server, the CLI's ``serve`` commands,
the generation providers and the agent board's HTTP surface, standalone
service and sweeper and the ``knowledge-engine board`` client read their
configuration from a ``Settings`` instance instead of ``os.environ``.

A few substrate modules still read their own variables directly, deep inside
code that predates this layer (``runtime.foundation.config``/``db``,
``runtime.pipeline.task_classifier``, ``project_context.config``,
``agent_board.scopes`` and ``mcp.http``;
flags and lists among them use the shared parsers in ``core.util.env``).
Those variables are declared here too, so
``knowledge-engine config show`` and ``01-user-docs/CONFIGURATION.md`` list
them, and the ``Read by`` column names the module that consumes each one.

Parsing is deliberately forgiving, as the ad-hoc readers it replaces were:
an unparseable value is logged at WARNING (secrets are never echoed) and the
field falls back to its default instead of aborting startup. Booleans share
one parser (:func:`knowledge_engine.core.util.env.parse_bool`): ``1/true/yes/on`` are on, ``0/false/no/off`` and the empty string
are off, anything else is reported and ignored.

Regenerate the operator reference with::

    python -m knowledge_engine.core.settings --markdown > 01-user-docs/CONFIGURATION.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import (
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import paths as _paths
from .util.env import (
    FALSE_VALUES,
    TRUE_VALUES,
    parse_bool as parse_env_bool,
    parse_bool_or as env_flag,
    parse_list,
)

_logger = logging.getLogger(__name__)

ENV_PREFIX = "KE_"
MASK = "********"

#: Values accepted by ``KE_EMBED_PROVIDER`` (and ``--provider`` of the embeddings CLI).
EMBED_PROVIDERS = ("ollama", "remote", "fake")

# Section keys, in the order the generated reference lists them.
SECTIONS: dict[str, str] = {
    "paths": "Paths",
    "server": "HTTP server",
    "board": "Agent board",
    "generation": "Generation providers",
    "embeddings": "Embeddings (semantic and hybrid search)",
    "substrate": "Pipeline and substrates",
}


EnvBool = Annotated[bool, BeforeValidator(parse_env_bool)]
OptionalEnvBool = Annotated[bool | None, BeforeValidator(parse_env_bool)]


def _env(
    name: str,
    default: Any,
    description: str,
    *,
    section: str,
    read_by: str,
    secret: bool = False,
    **constraints: Any,
) -> Any:
    """Declare one environment-backed field (exact env name, docs metadata)."""
    if section not in SECTIONS:  # pragma: no cover - programming error
        raise ValueError(f"unknown settings section {section!r}")
    return Field(
        default,
        validation_alias=name,
        description=description,
        json_schema_extra={"section": section, "read_by": read_by, "secret": secret},
        **constraints,
    )


class Settings(BaseSettings):
    """Effective engine configuration resolved from the environment.

    Every field reads the environment variable named by its alias (``KE_*``,
    plus the historical ``OLLAMA_*`` and ``BOARD_HOST``/``BOARD_PORT`` names).

    Instances are immutable. Construct one with ``Settings()`` (reads the
    current environment) or pass field values as keyword arguments, e.g.
    ``Settings(board_enabled=False)``, in tests and embedding code.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        frozen=True,
        case_sensitive=True,
        validate_by_name=True,
        validate_default=False,
    )

    # ── Paths (resolved by core.paths) ───────────────────────────
    home: str | None = _env(
        "KE_HOME",
        None,
        "Engine home. Unset: the nearest directory at or above the working "
        "directory that contains corpus/registry.json, else the working directory.",
        section="paths",
        read_by="core.paths",
    )
    corpus_root: str | None = _env(
        "KE_CORPUS_ROOT",
        None,
        "Corpus root. Unset: <home>/corpus.",
        section="paths",
        read_by="core.paths",
    )
    registry_path: str | None = _env(
        "KE_REGISTRY_PATH",
        None,
        "Registry JSON (the source of truth). Unset: <corpus_root>/registry.json.",
        section="paths",
        read_by="core.paths",
    )
    data_dir: str | None = _env(
        "KE_DATA_DIR",
        None,
        "Runtime data directory (FTS index, board DB, embeddings). "
        "Unset: <home>/.knowledge-engine/data.",
        section="paths",
        read_by="core.paths",
    )

    # ── HTTP server ──────────────────────────────────────────────
    port: int | None = _env(
        "KE_PORT",
        None,
        "Default port of `knowledge-engine serve` when --port is not given (unset: 9210); "
        "also the board client's port fallback.",
        section="server",
        read_by="cli, agent_board.cli",
        ge=1,
        le=65535,
    )
    bind_host: str = _env(
        "KE_BIND_HOST",
        "127.0.0.1",
        "Address the HTTP app is bound to; selects the /mcp Host-header policy. "
        "`knowledge-engine serve --host` sets it.",
        section="server",
        read_by="server.app",
    )
    cors_origins: str = _env(
        "KE_CORS_ORIGINS",
        "",
        "Comma-separated browser origins allowed to call the engine API. "
        "Empty: loopback origins only. `*` allows any origin.",
        section="server",
        read_by="server.app",
    )
    log_level: str = _env(
        "KE_LOG_LEVEL",
        "INFO",
        "Log level of the long-running commands (`serve`, `board-serve`, `mcp`, `watch`); "
        "`-v`/`-q` override it. One-shot commands log at WARNING.",
        section="server",
        read_by="core.logging_config, mcp.server",
    )
    mcp_http: EnvBool = _env(
        "KE_MCP_HTTP",
        False,
        "Mount the MCP streamable-HTTP endpoint at /mcp on the engine app.",
        section="server",
        read_by="server.app",
    )
    mcp_allowed_hosts: str = _env(
        "KE_MCP_ALLOWED_HOSTS",
        "",
        "Extra Host-header names accepted by /mcp (comma-separated; `*` disables the check).",
        section="server",
        read_by="mcp.http",
    )
    trust_proxy: EnvBool = _env(
        "KE_TRUST_PROXY",
        False,
        "Honour X-Forwarded-For from KE_TRUSTED_PROXIES (board trust gate and uvicorn).",
        section="server",
        read_by="core.http, agent_board.http",
    )
    proxy_allowlist: str | None = _env(
        "KE_TRUSTED_PROXIES",
        None,
        "Comma-separated CIDRs allowed to speak for clients via X-Forwarded-For. "
        "Unset: loopback. Empty: none.",
        section="server",
        read_by="core.http, agent_board.http",
    )

    # ── Agent board ──────────────────────────────────────────────
    board_enabled: EnvBool = _env(
        "KE_BOARD_ENABLED",
        True,
        "Mount the agent board at /board on the engine app.",
        section="board",
        read_by="server.app",
    )
    board_sweeper: OptionalEnvBool = _env(
        "KE_BOARD_SWEEPER",
        None,
        "Boot override for the board sweeper thread. Unset: the persisted "
        "board_config.sweeper_enabled flag decides (default on).",
        section="board",
        read_by="agent_board.sweeper, server.lifespan, agent_board.service",
    )
    board_peer_allowlist: str | None = _env(
        "KE_BOARD_TRUSTED_CIDRS",
        None,
        "Comma-separated CIDRs of trusted board peers. Unset: loopback + "
        "Tailscale 100.64.0.0/10. Set (even to an empty string) it REPLACES that "
        "default: an empty value trusts no peer, so set `127.0.0.1/32,::1/128` to "
        "keep loopback.",
        section="board",
        read_by="agent_board.http",
    )
    board_cors_origins: str = _env(
        "KE_BOARD_CORS_ORIGINS",
        "",
        "CORS allow-list of the standalone board service (same syntax as KE_CORS_ORIGINS).",
        section="board",
        read_by="agent_board.service",
    )
    board_max_scopes: int = _env(
        "KE_BOARD_MAX_SCOPES",
        64,
        "Cap on per-scope board databases (0 = unlimited; negative values count as 0).",
        section="board",
        read_by="agent_board.scopes",
    )
    board_serve_host: str | None = _env(
        "BOARD_HOST",
        None,
        "Default bind address of `knowledge-engine board-serve` when --host is not given "
        "(unset: 127.0.0.1).",
        section="board",
        read_by="cli",
    )
    board_serve_port: int | None = _env(
        "BOARD_PORT",
        None,
        "Default port of `knowledge-engine board-serve` when --port is not given; "
        "wins over KE_BOARD_STANDALONE_PORT.",
        section="board",
        read_by="cli",
        ge=1,
        le=65535,
    )
    board_standalone_port: int = _env(
        "KE_BOARD_STANDALONE_PORT",
        11437,
        "Port of the standalone board service when neither --port nor BOARD_PORT is given.",
        section="board",
        read_by="agent_board.service, cli",
        ge=1,
        le=65535,
    )
    board_url: str | None = _env(
        "KE_BOARD_URL",
        None,
        "Base URL the `knowledge-engine board` client talks to.",
        section="board",
        read_by="agent_board.cli",
    )
    board_port: str | None = _env(
        "KE_BOARD_PORT",
        None,
        "Port the board client uses when KE_BOARD_URL is unset (falls back to KE_PORT, then 9210).",
        section="board",
        read_by="agent_board.cli",
    )
    board_key: SecretStr | None = _env(
        "KE_BOARD_KEY",
        None,
        "X-Board-Key the board client presents when --key is not given.",
        section="board",
        read_by="agent_board.cli",
        secret=True,
    )

    # ── Generation providers ─────────────────────────────────────
    provider_order: str = _env(
        "KE_PROVIDER_ORDER",
        "",
        "Comma-separated provider names tried first, in this order "
        "(e.g. `ollama,cloud-http`); unlisted providers follow in the default order "
        "(cloud-http, ollama, echo).",
        section="generation",
        read_by="generation.factory",
    )
    provider_probe_ttl: float = _env(
        "KE_PROVIDER_PROBE_TTL",
        30.0,
        "Seconds a provider availability probe is cached (0 = probe on every request).",
        section="generation",
        read_by="generation.base",
        ge=0,
    )
    provider_startup_probe: EnvBool = _env(
        "KE_PROVIDER_STARTUP_PROBE",
        True,
        "Probe every provider in the background at startup and warn about unreachable ones.",
        section="generation",
        read_by="server.lifespan",
    )
    cloud_endpoint: str = _env(
        "KE_CLOUD_ENDPOINT",
        "",
        "Base URL of an OpenAI-compatible API (e.g. https://api.example/v1); "
        "the provider POSTs to <endpoint>/chat/completions.",
        section="generation",
        read_by="generation.cloud_http",
    )
    cloud_api_key: SecretStr | None = _env(
        "KE_CLOUD_API_KEY",
        None,
        "Bearer token for KE_CLOUD_ENDPOINT. The cloud-http provider is "
        "registered only when both are set.",
        section="generation",
        read_by="generation.cloud_http, agent_board.provider_registry",
        secret=True,
    )
    cloud_model: str = _env(
        "KE_CLOUD_MODEL",
        "",
        "Default model for cloud-http when a request names none.",
        section="generation",
        read_by="generation.cloud_http",
    )
    cloud_timeout: float = _env(
        "KE_CLOUD_TIMEOUT",
        60.0,
        "Timeout in seconds for one cloud-http completion request.",
        section="generation",
        read_by="generation.cloud_http",
        gt=0,
    )
    ollama_base_url: str = _env(
        "OLLAMA_BASE_URL",
        "http://127.0.0.1:11434",
        "Ollama server for the ollama generation provider.",
        section="generation",
        read_by="generation.ollama_provider",
    )
    ollama_model: str = _env(
        "OLLAMA_MODEL",
        "llama3.2",
        "Default Ollama model when a request names none.",
        section="generation",
        read_by="generation.ollama_provider",
    )
    ollama_timeout: float = _env(
        "OLLAMA_TIMEOUT",
        30.0,
        "Timeout in seconds for one Ollama generate request.",
        section="generation",
        read_by="generation.ollama_provider",
        gt=0,
    )
    anthropic_api_key: SecretStr | None = _env(
        "KE_ANTHROPIC_API_KEY",
        None,
        "Default env binding for the `anthropic` board provider credential.",
        section="generation",
        read_by="agent_board.provider_registry",
        secret=True,
    )
    openai_api_key: SecretStr | None = _env(
        "KE_OPENAI_API_KEY",
        None,
        "Default env binding for the `openai` board provider credential.",
        section="generation",
        read_by="agent_board.provider_registry",
        secret=True,
    )

    # ── Embeddings (corpus embedding index, hybrid search) ───────
    embed_provider: str = _env(
        "KE_EMBED_PROVIDER",
        "ollama",
        "Embedding provider of the corpus embedding index: `ollama` (local Ollama at "
        "KE_OLLAMA_URL), `remote` (KE_EMBED_REMOTE_URL; needs KE_EMBED_ALLOW_REMOTE=1) or "
        "`fake` (offline, deterministic; tests and dry runs).",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )
    embed_model: str = _env(
        "KE_EMBED_MODEL",
        "bge-m3:latest",
        "Embedding model (bge-m3 by default). The index records it and refuses vectors "
        "of another model; rebuild with `knowledge-engine embeddings build --rebuild` "
        "after changing it.",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )
    embed_batch: int = _env(
        "KE_EMBED_BATCH",
        32,
        "Texts sent per embedding request while building the index.",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
        ge=1,
        le=1024,
    )
    ollama_url: str = _env(
        "KE_OLLAMA_URL",
        "http://127.0.0.1:11434",
        "Ollama server of the `ollama` embedding provider.",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )
    embed_remote_url: str = _env(
        "KE_EMBED_REMOTE_URL",
        "",
        "Endpoint of the `remote` embedding provider (POST {model, input} per text; "
        "answers {embedding} or {embeddings}).",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )
    embed_allow_remote: EnvBool = _env(
        "KE_EMBED_ALLOW_REMOTE",
        False,
        "Opt-in gate of the `remote` embedding provider: corpus text is sent off-machine "
        "only when this is on.",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )
    embeddings_db: str | None = _env(
        "KE_EMBEDDINGS_DB",
        None,
        "Corpus embedding index (SQLite). Unset: <data_dir>/embeddings.db.",
        section="embeddings",
        read_by="retrieval.embeddings.factory",
    )

    # ── Pipeline and substrates (read directly by those modules) ──
    pipeline_db: str | None = _env(
        "KE_PIPELINE_DB",
        None,
        "Board/pipeline SQLite database. Unset: <data_dir>/pipeline.db.",
        section="substrate",
        read_by="runtime.foundation.db",
    )
    pipeline_root: str | None = _env(
        "KE_PIPELINE_ROOT",
        None,
        "Pipeline root (YAML config, and the .env the CLI/MCP server/app load at startup). "
        "Unset: ./pipeline.",
        section="substrate",
        read_by="runtime.foundation.config",
    )
    config_dir: str | None = _env(
        "KE_CONFIG_DIR",
        None,
        "Pipeline YAML config directory. Unset: <KE_PIPELINE_ROOT>/config.",
        section="substrate",
        read_by="runtime.foundation.config",
    )
    config_path: str | None = _env(
        "KE_CONFIG_PATH",
        None,
        "project-context TOML config file.",
        section="substrate",
        read_by="project_context.config",
    )
    classifier_model: str = _env(
        "KE_CLASSIFIER_MODEL",
        "qwen3:4b",
        "Model of the pipeline task classifier.",
        section="substrate",
        read_by="runtime.pipeline.task_classifier",
    )
    classifier_endpoint: str = _env(
        "KE_CLASSIFIER_ENDPOINT",
        "http://127.0.0.1:11434/api/generate",
        "Endpoint of the pipeline task classifier.",
        section="substrate",
        read_by="runtime.pipeline.task_classifier",
    )
    # ── Validation ───────────────────────────────────────────────

    @field_validator("bind_host", mode="before")
    @classmethod
    def _blank_bind_host(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or "127.0.0.1"
        return value

    @field_validator("embed_provider", mode="before")
    @classmethod
    def _normalise_embed_provider(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip().lower()
        if not text:
            return "ollama"
        if text not in EMBED_PROVIDERS:
            raise ValueError(f"unknown embedding provider {value!r}")
        return text

    @field_validator("embed_model", mode="before")
    @classmethod
    def _blank_embed_model(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or "bge-m3:latest"
        return value

    @field_validator("board_max_scopes", mode="after")
    @classmethod
    def _clamp_max_scopes(cls, value: int) -> int:
        # Same rule as agent_board.scopes.max_scopes(): negative means unlimited.
        return max(value, 0)

    @field_validator("board_serve_host", "port", "board_serve_port", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip().upper()
        if not text:
            return "INFO"
        if text not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level {value!r}")
        return text

    @field_validator("*", mode="wrap")
    @classmethod
    def _lenient(
        cls, value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> Any:
        """Fall back to the default (with a WARNING) instead of failing startup."""
        try:
            return handler(value)
        except ValidationError as exc:
            name = info.field_name or "?"
            field = cls.model_fields[name]
            default = field.get_default(call_default_factory=True)
            env_name = field.validation_alias if isinstance(field.validation_alias, str) else name
            shown = MASK if _is_secret(field.json_schema_extra) else repr(_short(value))
            reason = exc.errors()[0].get("msg", "invalid value") if exc.errors() else "invalid"
            _logger.warning(
                "ignoring invalid %s=%s (%s); using default %r", env_name, shown, reason, default
            )
            return default

    # ── Derived views ────────────────────────────────────────────

    def _path_env(self) -> dict[str, str]:
        pairs = (
            (_paths.ENV_HOME, self.home),
            (_paths.ENV_CORPUS_ROOT, self.corpus_root),
            (_paths.ENV_REGISTRY_PATH, self.registry_path),
            (_paths.ENV_DATA_DIR, self.data_dir),
        )
        return {name: value for name, value in pairs if value is not None}

    @property
    def paths(self) -> _paths.EnginePaths:
        """The on-disk layout, resolved by :func:`core.paths.resolve` (read-only)."""
        return _paths.resolve(self._path_env())

    def resolve_paths(self, *, warn_missing_corpus: bool = True) -> _paths.EnginePaths:
        """Like :attr:`paths`, optionally without the missing-corpus warning."""
        return _paths.resolve(self._path_env(), warn_missing_corpus=warn_missing_corpus)

    def provider_order_list(self) -> list[str]:
        """``KE_PROVIDER_ORDER`` split into names (lower-cased, blanks dropped)."""
        return parse_list(self.provider_order, lower=True)

    def cloud_configured(self) -> bool:
        """Both ``KE_CLOUD_ENDPOINT`` and ``KE_CLOUD_API_KEY`` are set."""
        key = self.cloud_api_key.get_secret_value() if self.cloud_api_key else ""
        return bool(self.cloud_endpoint.strip() and key.strip())

    # ── Introspection (config show / reference generator) ────────

    @classmethod
    def env_names(cls) -> dict[str, str]:
        """``{field name: environment variable}`` for every declared setting."""
        out: dict[str, str] = {}
        for name, field in cls.model_fields.items():
            alias = field.validation_alias
            out[name] = alias if isinstance(alias, str) else ENV_PREFIX + name.upper()
        return out

    @classmethod
    def secret_fields(cls) -> frozenset[str]:
        return frozenset(
            name for name, field in cls.model_fields.items() if _is_secret(field.json_schema_extra)
        )

    def masked(self) -> dict[str, Any]:
        """``{ENV_NAME: effective value}`` with every secret replaced by a mask.

        A secret that is unset shows as ``None``; a set one as ``********``.
        """
        secrets = self.secret_fields()
        out: dict[str, Any] = {}
        for name, env_name in self.env_names().items():
            value = getattr(self, name)
            if name in secrets:
                out[env_name] = MASK if value is not None and _secret_text(value) else None
            else:
                out[env_name] = value
        return out

    def explicitly_set(self) -> list[str]:
        """Environment names whose value came from the environment or a kwarg."""
        names = self.env_names()
        return sorted(names[name] for name in self.model_fields_set)


def _is_secret(extra: Any) -> bool:
    return isinstance(extra, Mapping) and bool(extra.get("secret"))


def _secret_text(value: Any) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


def _short(value: Any, limit: int = 80) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def get_settings() -> Settings:
    """A fresh :class:`Settings` from the current environment.

    Not cached: long-lived components (the app, the sweeper, providers) take
    a ``Settings`` once at construction; one-shot callers read the
    environment as it is now.
    """
    return Settings()


# ── Reference generator ──────────────────────────────────────────


def _default_text(name: str, value: Any) -> str:
    if value is None:
        return "*(unset)*"
    if isinstance(value, bool):
        return "`1`" if value else "`0`"
    if value == "":
        return "*(empty)*"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"`{value}`"


def _type_text(annotation: Any) -> str:
    text = str(annotation).replace("typing.", "").replace("pydantic.types.", "")
    optional = " (optional)" if "None" in text else ""
    for needle, label in (("bool", "bool"), ("SecretStr", "secret"), ("float", "float")):
        if needle in text:
            return label + (optional if label == "bool" else "")
    if "int" in text:
        return "int"
    return "string" + optional


def markdown_reference() -> str:
    """Render the operator reference table for every setting (Markdown)."""
    names = Settings.env_names()
    lines = [
        "# Configuration reference",
        "",
        "<!-- Generated by `python -m knowledge_engine.core.settings --markdown`; do not edit by hand. -->",
        "",
        "Every environment variable the engine reads, with its type and default.",
        "Print the effective values (secrets masked) with `knowledge-engine config show`.",
        "",
        "Booleans accept `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off` (case-insensitive;",
        "an empty value is off). An unparseable value is logged as a WARNING and the default",
        "is used instead.",
        "",
    ]
    by_section: dict[str, list[str]] = {key: [] for key in SECTIONS}
    for name, field in Settings.model_fields.items():
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, Mapping) else {}
        section = str(extra.get("section", "substrate"))
        description = (field.description or "").replace("|", "\\|")
        if extra.get("secret"):
            description += " Secret: masked by `config show`."
        row = (
            f"| `{names[name]}` | {_type_text(field.annotation)} | "
            f"{_default_text(name, field.default)} | {description} | `{extra.get('read_by', '')}` |"
        )
        by_section[section].append(row)
    for key, title in SECTIONS.items():
        rows = by_section[key]
        if not rows:
            continue
        lines += [
            f"## {title}",
            "",
            "| Variable | Type | Default | Description | Read by |",
            "|---|---|---|---|---|",
            *rows,
            "",
        ]
    lines += [
        "Variables in the last section are read directly by the substrate modules named in",
        "*Read by*; they are listed here so the whole surface is documented in one place.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m knowledge_engine.core.settings [--markdown | --show]``."""
    parser = argparse.ArgumentParser(prog="python -m knowledge_engine.core.settings")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--markdown", action="store_true", help="Print the Markdown reference.")
    mode.add_argument(
        "--show", action="store_true", help="Print the effective settings (secrets masked)."
    )
    args = parser.parse_args(argv)
    if args.markdown:
        sys.stdout.write(markdown_reference())
        return 0
    import json  # noqa: PLC0415 - only needed for --show

    sys.stdout.write(json.dumps(Settings().masked(), indent=2, default=str) + "\n")
    return 0


__all__ = [
    "EMBED_PROVIDERS",
    "ENV_PREFIX",
    "FALSE_VALUES",
    "MASK",
    "TRUE_VALUES",
    "EnvBool",
    "OptionalEnvBool",
    "Settings",
    "env_flag",
    "get_settings",
    "markdown_reference",
    "parse_env_bool",
]


if __name__ == "__main__":
    raise SystemExit(main())
