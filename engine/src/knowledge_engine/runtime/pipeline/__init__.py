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

Submodules are imported lazily (``knowledge_engine.runtime.pipeline.queue``
works as an attribute or an import).
"""

from __future__ import annotations

import importlib
from types import ModuleType

__all__ = ["message_board", "queue", "task_classifier", "worker_registry"]


def __getattr__(name: str) -> ModuleType:
    # PEP 562: submodules load on first access, so importing the package (or
    # the foundation it builds on) has no side effects.
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
