"""Knowledge-Engine — Pipeline Configuration Loader.

Reads YAML config files from the pipeline config directory and provides typed
access to pipeline configuration.

Env vars:
    KE_PIPELINE_ROOT  (default: ./pipeline)
    KE_CONFIG_DIR     (default: $KE_PIPELINE_ROOT/config)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# ── Resolve pipeline root from env vars ───────────────────────────────
PIPELINE_ROOT = Path(
    os.environ.get("KE_PIPELINE_ROOT", str(Path.cwd() / "pipeline"))
).resolve()
CONFIG_DIR = Path(
    os.environ.get("KE_CONFIG_DIR", str(PIPELINE_ROOT / "config"))
).resolve()

# ── Load .env on import (stdlib, no dotenv dependency) ─────
_env_file = PIPELINE_ROOT / ".env"
if _env_file.exists():
    with open(_env_file, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _key and _key not in os.environ:  # don't override system env
                os.environ[_key] = _val


def _load_yaml(filename: str) -> dict[str, Any]:
    """Load a YAML config file from the config/ directory."""
    if yaml is None:
        raise ImportError("PyYAML is required for the pipeline config loader. `pip install pyyaml`.")
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(filename: str, data: dict[str, Any]) -> Path:
    """Write a YAML config file to the config/ directory."""
    if yaml is None:
        raise ImportError("PyYAML is required for the pipeline config writer. `pip install pyyaml`.")
    path = CONFIG_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
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
            "candidates": [{
                "host": proxy.get("host", "127.0.0.1"),
                "port": proxy.get("port", 8420),
                "node_id": proxy.get("node_id", "primary"),
                "priority": 1,
            }],
        }
    return failover


def get_api_key(env_key: str) -> str | None:
    """Read an API key from environment variables. Never logs the value."""
    return os.environ.get(env_key)


def get_enabled_nodes(nodes_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only nodes marked as enabled."""
    if nodes_config is None:
        nodes_config = load_nodes()
    return {
        k: v for k, v in nodes_config.get("nodes", {}).items()
        if v.get("enabled", False)
    }


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
