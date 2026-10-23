"""Shared scaffolding for agent-board MCP tool modules.

Result envelopes come from :mod:`knowledge_engine.mcp.tool_groups` (shared with
project-context) so dispatching looks the same to the MCP server. Lightweight context object exposes the store + keys so
modules don't need to re-import everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ...mcp.tool_groups import error_result, status_result, text_result
from .. import keys, store, sweeper

_logger = logging.getLogger(__name__)

# ── Per-call context ─────────────────────────────────────────────────


# Schema fragment shared by every write tool. The MCP stdio server is a
# local process, so it is treated as a loopback peer: with
# ``require_key_for_post=0`` (the default) writes are keyless; with the flag
# on, loopback writes stay keyless unless a key is supplied, in which case
# it must be valid and hold ``board:write``.
BOARD_KEY_INPUT: dict[str, Any] = {
    "board_key": {
        "type": "string",
        "description": (
            "Optional X-Board-Key token. Only checked when the board's "
            "require_key_for_post flag is on; the MCP server runs locally and "
            "counts as a loopback peer, so it is never required. If supplied it "
            "must be a valid key with board:write."
        ),
    },
}


@dataclass
class BoardContext:
    """Per-call context handed to every board tool dispatch.

    ``require_key_for_post`` mirrors the persisted flag at the time the
    context was built; :meth:`write_gate` re-reads it so a flag flipped at
    runtime applies to the next tool call without an MCP restart.
    """

    require_key_for_post: bool = False

    @classmethod
    def from_config(cls) -> BoardContext:
        cfg = store.load_config()
        return cls(require_key_for_post=bool(cfg.get("require_key_for_post")))

    def write_gate(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """Apply the WRITE decision shared with ``POST /board/messages``.

        Returns an MCP error envelope when the write is refused, else None.
        ``board_key`` is popped from ``args`` so it never reaches the schema
        validator or the stored message.
        """
        raw_key = args.pop("board_key", None)
        require = bool(store.load_config().get("require_key_for_post"))
        self.require_key_for_post = require
        decision = keys.authorize_write(
            str(raw_key) if raw_key is not None else None,
            loopback=True,
            require_key=require,
        )
        if decision.ok:
            return None
        _logger.warning("board MCP write refused: %s", decision.detail)
        return error_result(decision.detail, status=decision.status)


def write_gate(ctx: BoardContext | None, args: dict[str, Any]) -> dict[str, Any] | None:
    """Module-level convenience: run the write gate with a fresh context when
    the dispatcher did not supply one."""
    return (ctx or BoardContext()).write_gate(args)


__all__ = [
    "BOARD_KEY_INPUT",
    "BoardContext",
    "error_result",
    "keys",
    "status_result",
    "store",
    "sweeper",
    "text_result",
    "write_gate",
]
