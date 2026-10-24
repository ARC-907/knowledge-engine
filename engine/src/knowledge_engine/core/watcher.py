"""Optional watchdog: auto-register new corpus folders while `knowledge-engine watch` runs.

The registry's lifecycle flags (`watch_enabled`, `auto_register`, both on by
default; `PATCH /registry/lifecycle/state` changes them) are read from
`registry.json` on every folder event, so a running watcher follows changes made
by the server: with either flag off, new folders are logged and left alone.

Soft-coupled: if `watchdog` package not installed, raises at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .config import Config
from .registry import CORPUS_FOLDER_TO_KIND, Registry, RegistryEntry
from .util.slug import slugify

if TYPE_CHECKING:
    from watchdog.events import DirCreatedEvent, FileCreatedEvent
    from watchdog.observers.api import BaseObserver

log = logging.getLogger(__name__)


def register_corpus_folders(config: Config, registry: Registry) -> dict[str, int]:
    """Register every folder under ``corpus/{libraries,skills,kits,capabilities}``.

    Children are visited in sorted order; hidden folders and files are
    ignored. Each new folder becomes a ``<kind>-<slug>`` entry with
    ``auto_registered=True``; a folder whose id is already registered (or
    already claimed by an earlier folder in this pass) is counted as
    ``skipped``. All new entries are written in one atomic registry update.

    Returns ``{"library": n, "skill": n, "kit": n, "tool": n, "skipped": n}``.
    """
    root = config.corpus_root
    counts: dict[str, int] = {"library": 0, "skill": 0, "kit": 0, "tool": 0, "skipped": 0}
    pending: list[RegistryEntry] = []
    claimed: set[str] = set()
    for sub, kind in CORPUS_FOLDER_TO_KIND.items():
        folder = root / sub
        if not folder.is_dir():
            continue
        try:
            # Sort by name (not Path): Path ordering is case-insensitive on
            # Windows, which would make the registration order platform-dependent.
            children = sorted(folder.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            log.warning("cannot list corpus folder %s: %s", folder, exc)
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            entry_id = f"{kind}-{slugify(child.name)}"
            if registry.get(entry_id):
                counts["skipped"] += 1
                continue
            entry = RegistryEntry(
                id=entry_id,
                kind=kind,
                name=child.name,
                path=child.relative_to(root).as_posix(),
                auto_registered=True,
            )
            if entry_id in claimed:
                # Two folders slugify to the same id. Historically the later
                # folder won (and both counted as added); keep that, loudly,
                # and keep the entry's position in the sorted walk.
                log.warning(
                    "duplicate corpus slug %r: %s replaces an earlier folder", entry_id, child
                )
                index = next(i for i, e in enumerate(pending) if e.id == entry_id)
                pending[index] = entry
            else:
                claimed.add(entry_id)
                pending.append(entry)
            counts[kind] += 1
    registry.upsert_many(pending)
    for entry in pending:
        log.info("auto-registered %s", entry.id)
    return counts


def auto_register(
    config: Config, registry: Registry, on_change: Callable[[], None] | None = None
) -> int:
    """One-shot scan: register any corpus folder not yet in the registry.

    Returns the number of entries added; calls ``on_change`` when any were.
    """
    counts = register_corpus_folders(config, registry)
    added = sum(n for key, n in counts.items() if key != "skipped")
    if added and on_change:
        on_change()
    return added


def lifecycle_allows_auto_register(registry: Registry) -> bool:
    """True when the registry's ``watch_enabled`` and ``auto_register`` flags are on.

    Re-reads ``registry.json`` first so flags (and entries) changed by another
    process are seen; an unreadable or invalid file keeps the last loaded state
    (the registry logs why).
    """
    try:
        registry.reload()
    except (OSError, ValueError):
        log.warning("registry reload failed; using the last loaded lifecycle flags")
    flags = registry.lifecycle()
    return bool(flags.get("watch_enabled", True)) and bool(flags.get("auto_register", True))


def handle_new_folder(
    config: Config,
    registry: Registry,
    path: str,
    on_change: Callable[[], None] | None = None,
) -> int:
    """React to a folder created under the corpus; returns the entries added.

    Honours the lifecycle flags and never raises: the watcher thread must
    survive one bad event (the failure is logged).
    """
    if not lifecycle_allows_auto_register(registry):
        log.info("new folder %s not registered: lifecycle flags are off", path)
        return 0
    try:
        return auto_register(config, registry, on_change)
    except Exception:
        log.exception("auto-registering after %s failed", path)
        return 0


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
                handle_new_folder(config, registry, str(event.src_path), on_change)

    observer = Observer()
    for sub in CORPUS_FOLDER_TO_KIND:
        folder = config.corpus_root / sub
        if folder.exists():
            observer.schedule(Handler(), str(folder), recursive=False)
    observer.start()
    log.info("watcher started on %s", config.corpus_root)
    return observer
