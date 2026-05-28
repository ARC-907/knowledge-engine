"""Pipeline subsystem (opt-in).

Multi-worker task queue + message board + worker registry + task classifier
for buyers running coordinated agent pipelines on top of the knowledge-engine.

Requires `knowledge_engine.foundation` (config + db). Not required by the
lean-core happy path (FTS5 search + dashboard + MCP); engine works fine
without ever touching `pipeline/`.

Modules:
  - queue.py             SQLite-backed task queue with lease-based claiming.
  - message_board.py     Append-only coordination channel.
  - worker_registry.py   Worker registration + heartbeat tracking
                         (named `worker_registry` to avoid clashing with the
                         corpus registry at knowledge_engine.registry).
  - task_classifier.py   Tiny classifier for domain/complexity routing.
"""

from . import queue, message_board, worker_registry, task_classifier

__all__ = ["queue", "message_board", "worker_registry", "task_classifier"]
