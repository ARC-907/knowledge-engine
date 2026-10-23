"""Knowledge-Engine — Pipeline Configuration Loader.

Reads YAML config files from the pipeline config directory and provides typed
access to pipeline configuration.

Env vars (read when a function needs them, never at import):
    KE_PIPELINE_ROOT  (default: <cwd>/pipeline)   -- :func:`pipeline_root`
    KE_CONFIG_DIR     (default: $KE_PIPELINE_ROOT/config)   -- :func:`config_dir`

``.env`` files are loaded explicitly with :func:`load_dotenv_file`; the CLI
(``knowledge-engine ...``), the MCP server entry point and the HTTP app's
lifespan call it for ``<pipeline root>/.env`` at startup. Importing this
module does not touch ``os.environ``.

``PIPELINE_ROOT`` / ``CONFIG_DIR`` remain readable as module attributes for
older callers; each access resolves the current value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]  # optional dependency; checked before use

_logger = logging.getLogger(__name__)

PIPELINE_ROOT_ENV = "KE_PIPELINE_ROOT"
CONFIG_DIR_ENV = "KE_CONFIG_DIR"
DOTENV_FILENAME = ".env"


def pipeline_root(env: Mapping[str, str] | None = None) -> Path:
    """``KE_PIPELINE_ROOT`` (resolved), else ``<cwd>/pipeline``."""
    source = os.environ if env is None else env
    return Path(source.get(PIPELINE_ROOT_ENV, str(Path.cwd() / "pipeline"))).resolve()


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """``KE_CONFIG_DIR`` (resolved), else ``<pipeline root>/config``."""
    source = os.environ if env is None else env
    return Path(source.get(CONFIG_DIR_ENV, str(pipeline_root(source) / "config"))).resolve()


def default_dotenv_path() -> Path:
    """The ``.env`` file historically loaded for the pipeline: ``<pipeline root>/.env``."""
    return pipeline_root() / DOTENV_FILENAME


def load_dotenv_file(
    path: Path | str | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from ``path`` into the environment.

    ``path`` defaults to :func:`default_dotenv_path`. A missing file is not an
    error. Blank lines, ``#`` comments and lines without ``=`` are skipped;
    keys and values are stripped. Variables already set in the environment
    always win (a ``.env`` never overrides the real environment). Returns the
    variables that were set (values are never logged).
    """
    target = Path(path) if path is not None else default_dotenv_path()
    env = os.environ if environ is None else environ
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _logger.warning("could not read %s: %s", target, exc)
        return {}
    loaded: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in env:  # don't override the real environment
            env[key] = value
            loaded[key] = value
    if loaded:
        _logger.info("loaded %d variable(s) from %s: %s", len(loaded), target, ", ".join(loaded))
    return loaded


def __getattr__(name: str) -> Any:
    # PEP 562: the historical import-time constants, now resolved on access.
    if name == "PIPELINE_ROOT":
        return pipeline_root()
    if name == "CONFIG_DIR":
        return config_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_yaml(filename: str) -> dict[str, Any]:
    """Load a YAML config file from the config/ directory."""
    if yaml is None:
        raise ImportError(
            "PyYAML is required for the pipeline config loader. `pip install pyyaml`."
        )
    path = config_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(filename: str, data: dict[str, Any]) -> Path:
    """Write a YAML config file to the config/ directory."""
    if yaml is None:
        raise ImportError(
            "PyYAML is required for the pipeline config writer. `pip install pyyaml`."
        )
    path = config_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
    return path


def load_nodes() -> dict[str, Any]:
    """Load node registry configuration."""
    try:
        return _load_yaml("nodes.yaml")
    except FileNotFoundError:
        return {"nodes": {}}


def load_apis() -> dict[str, Any]:
    """Load API configuration (research + synthesis providers)."""
    try:
        return _load_yaml("apis.yaml")
    except FileNotFoundError:
        return {}


def load_tools() -> dict[str, Any]:
    """Load tool configuration."""
    try:
        return _load_yaml("tools.yaml")
    except FileNotFoundError:
        return {"tools": []}


def load_policy() -> dict[str, Any]:
    """Load pipeline policy (role separation, queue behavior, guardrails)."""
    try:
        return _load_yaml("pipeline-policy.yaml")
    except FileNotFoundError:
        return {"queue_policy": {"claim_timeout_seconds": 600}}


def load_message_board() -> dict[str, Any]:
    """Load message board configuration."""
    try:
        return _load_yaml("message-board.yaml")
    except FileNotFoundError:
        return {}


def load_routing() -> dict[str, Any]:
    """Load routing profiles configuration."""
    try:
        return _load_yaml("routing-profiles.yaml")
    except FileNotFoundError:
        return {}


def load_tool_host() -> dict[str, Any]:
    """Load tool hosting configuration."""
    try:
        return _load_yaml("tool-host.yaml")
    except FileNotFoundError:
        return {"tool_host": {}}


def load_agent_keys() -> dict[str, Any]:
    """Load agent API key configuration."""
    try:
        return _load_yaml("agent-keys.yaml")
    except FileNotFoundError:
        return {"agent_keys": {}}


def load_proxy_failover() -> dict[str, Any]:
    """Load proxy failover configuration from nodes.yaml."""
    nodes_cfg = load_nodes()
    failover = nodes_cfg.get("proxy_failover", {})
    if not failover or not failover.get("candidates"):
        proxy = nodes_cfg.get("proxy", {})
        return {
            "fail_threshold": 3,
            "failback_interval_seconds": 300,
            "candidates": [
                {
                    "host": proxy.get("host", "127.0.0.1"),
                    "port": proxy.get("port", 8420),
                    "node_id": proxy.get("node_id", "primary"),
                    "priority": 1,
                }
            ],
        }
    return failover


def get_api_key(env_key: str) -> str | None:
    """Read an API key from environment variables. Never logs the value."""
    return os.environ.get(env_key)


def get_enabled_nodes(nodes_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only nodes marked as enabled."""
    if nodes_config is None:
        nodes_config = load_nodes()
    return {k: v for k, v in nodes_config.get("nodes", {}).items() if v.get("enabled", False)}


def get_research_api_priority(apis_config: dict[str, Any] | None = None) -> list[str]:
    """Return the ordered list of research API provider keys."""
    if apis_config is None:
        apis_config = load_apis()
    return apis_config.get("research_apis", {}).get("priority_order", [])


def get_synthesis_api_priority(apis_config: dict[str, Any] | None = None) -> list[str]:
    """Return the ordered list of synthesis API provider keys."""
    if apis_config is None:
        apis_config = load_apis()
    return apis_config.get("synthesis_apis", {}).get("priority_order", [])


def get_cloud_gate_thresholds(policy_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return cloud synthesis gate preconditions."""
    if policy_config is None:
        policy_config = load_policy()
    return policy_config.get("cloud_gate", {})


def get_queue_policy(policy_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return queue behavior configuration."""
    if policy_config is None:
        policy_config = load_policy()
    return policy_config.get("queue_policy", {})


def load_all_config() -> dict[str, Any]:
    """Load all config files into a single dict."""
    return {
        "nodes": load_nodes(),
        "apis": load_apis(),
        "tools": load_tools(),
        "policy": load_policy(),
    }
