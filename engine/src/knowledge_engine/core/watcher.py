"""Optional watchdog: auto-register new folders when lifecycle says so.

Soft-coupled: if `watchdog` package not installed, raises at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .config import Config
from .registry import EntryKind, Registry, RegistryEntry
from .util.slug import slugify

if TYPE_CHECKING:
    from watchdog.events import DirCreatedEvent, FileCreatedEvent
    from watchdog.observers.api import BaseObserver

log = logging.getLogger(__name__)


_FOLDER_TO_KIND: dict[str, EntryKind] = {
    "libraries": "library",
    "skills": "skill",
    "kits": "kit",
    "capabilities": "tool",
}


def auto_register(
    config: Config, registry: Registry, on_change: Callable[[], None] | None = None
) -> int:
    """One-shot scan: register any folder under corpus/{libraries,skills,kits,capabilities} not yet in registry."""
    added = 0
    for sub, kind in _FOLDER_TO_KIND.items():
        folder = config.corpus_root / sub
        if not folder.exists():
            continue
        for child in folder.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            entry_id = f"{kind}-{slugify(child.name)}"
            if registry.get(entry_id):
                continue
            registry.upsert(
                RegistryEntry(
                    id=entry_id,
                    kind=kind,
                    name=child.name,
                    path=str(child.relative_to(config.corpus_root)).replace("\\", "/"),
                    auto_registered=True,
                )
            )
            added += 1
            log.info("auto-registered %s", entry_id)
    if added and on_change:
        on_change()
    return added


def start_watcher(
    config: Config, registry: Registry, on_change: Callable[[], None] | None = None
) -> BaseObserver:
    """Start a background watchdog observer. Returns the observer (call .stop() to halt)."""
    # watchdog is soft-coupled: import it only when a watcher is actually started.
    try:
        from watchdog.events import FileSystemEventHandler  # noqa: PLC0415
        from watchdog.observers import Observer  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("watchdog not installed; pip install watchdog") from e

    class Handler(FileSystemEventHandler):
        def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
            if event.is_directory:
                auto_register(config, registry, on_change)

    observer = Observer()
    for sub in _FOLDER_TO_KIND:
        folder = config.corpus_root / sub
        if folder.exists():
            observer.schedule(Handler(), str(folder), recursive=False)
    observer.start()
    log.info("watcher started on %s", config.corpus_root)
    return observer
