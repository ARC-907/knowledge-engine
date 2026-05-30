"""Knowledge-Engine — Agent Board.

First-class coordination surface for agents collaborating across worktrees,
branches, research, library, planning, execution, and testing. Sits on top
of the `foundation/db.py` SQLite backbone and the existing
`pipeline/message_board.py` post/poll primitives, adding:

* Schema-enforced channels and message types (`schemas.py`)
* FTS5 search over subject/body (`store.search_messages`)
* Context-compressed digests for MCP callers (`store.digest`)
* Provider-key vault for config-tab key management (`keys.py`)
* Background sweeper for TTL prune + stale-blocker reminders (`sweeper.py`)
* HTTP routes (`api/board_routes.py`)
* MCP tool group (`mcp_tools/`)
* CLI subcommand (`cli.py`)
* Optional standalone watchdog mode (`service.py`)

Local trust by default — the engine ships with `require_key_for_post=0`. Set
to 1 in the config tab (or via `/board/config`) to enforce key-gated posting
on non-localhost requests.
"""

__all__ = ["schemas", "store", "keys", "sweeper", "service", "cli"]
__version__ = "1.0.0"
