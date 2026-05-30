"""Shared scaffolding for agent-board MCP tool modules.

Result envelopes match the project-docs pattern so dispatching looks the same
to the MCP server. Lightweight context object exposes the store + keys so
modules don't need to re-import everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .. import keys, store, sweeper  # noqa: F401 — re-exported for tool modules


# ── MCP result envelopes ─────────────────────────────────────────────


def text_result(obj: Any) -> dict[str, Any]:
    """Wrap a JSON-serializable object as an MCP text content envelope."""
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, default=str)}]}


def status_result(status: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": status}
    payload.update(extra)
    return text_result(payload)


def error_result(message: str, **extra: Any) -> dict[str, Any]:
    payload = {"error": message}
    payload.update(extra)
    return text_result(payload)


# ── Per-call context ─────────────────────────────────────────────────


@dataclass
class BoardContext:
    """Per-call context. Currently a thin wrapper; expand as more state lands."""

    require_key_for_post: bool = False

    @classmethod
    def from_config(cls) -> "BoardContext":
        cfg = store.load_config()
        return cls(require_key_for_post=bool(cfg.get("require_key_for_post")))


__all__ = [
    "text_result", "status_result", "error_result", "BoardContext",
    "store", "keys", "sweeper",
]
