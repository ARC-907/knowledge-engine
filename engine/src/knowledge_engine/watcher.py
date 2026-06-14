"""Optional watchdog: auto-register new folders when lifecycle says so.

Soft-coupled: if `watchdog` package not installed, raises at startup.
"""

from __future__ import annotations

import logging
from typing import Callable

from .config import Config
from .registry import Registry, RegistryEntry

log = logging.getLogger(__name__)


def slugify(name: str) -> str:
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "&"):
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


_FOLDER_TO_KIND = {"libraries": "library", "skills": "skill", "kits": "kit", "capabilities": "tool"}


def auto_register(config: Config, registry: Registry, on_change: Callable[[], None] | None = None) -> int:
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
            registry.upsert(RegistryEntry(
                id=entry_id,
                kind=kind,  # type: ignore[arg-type]
                name=child.name,
                path=str(child.relative_to(config.corpus_root)).replace("\\", "/"),
                auto_registered=True,
            ))
            added += 1
            log.info("auto-registered %s", entry_id)
    if added and on_change:
        on_change()
    return added


def start_watcher(config: Config, registry: Registry, on_change: Callable[[], None] | None = None):
    """Start a background watchdog observer. Returns the observer (call .stop() to halt)."""
    try:
        from watchdog.observers import Observer  # type: ignore
        from watchdog.events import FileSystemEventHandler  # type: ignore
    except ImportError as e:
        raise RuntimeError("watchdog not installed; pip install watchdog") from e

    class Handler(FileSystemEventHandler):  # type: ignore[misc]
        def on_created(self, event):  # type: ignore[no-untyped-def]
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
