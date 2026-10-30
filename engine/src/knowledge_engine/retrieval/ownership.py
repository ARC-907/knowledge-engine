"""Which registry entry owns a corpus file.

Registry entries may nest: a library at ``libraries/example`` and another
enabled entry at ``libraries/example/research``. Every search mode attributes
a file to exactly one entry, its **owner**:

* the owner is the *deepest* enabled entry whose path is the file itself or a
  directory containing it;
* a disabled entry owns nothing, so its files fall back to the nearest
  enabled ancestor (and are not indexed at all if there is none);
* a ``kind`` filter applies to the owner's kind -- a file owned by a nested
  ``skill`` is not returned for ``kind=library`` through its parent library.

FTS indexing (:mod:`.fts5`), vector search and hybrid fusion
(:mod:`.hybrid`) all use this one rule, so a file appears at most once per
result list and with the same ``entry_id`` in every mode. See
``02-dev-docs/adr/0005-nested-entry-ownership.md``.

Paths are compared as corpus-relative POSIX strings after
:func:`os.path.normcase`, so the rule also holds on case-insensitive file
systems where the registry and the directory differ only in letter case.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

__all__ = ["EntryOwnership", "normalize_entry_path"]


def normalize_entry_path(raw: object) -> str:
    """A registry ``path`` as a corpus-relative POSIX string without edge slashes."""
    return str(raw or "").replace("\\", "/").strip("/")


def _key(path: str) -> str:
    return os.path.normcase(path)


class EntryOwnership:
    """Resolve the owning entry of corpus-relative paths.

    ``entries`` are registry entry dicts; only enabled ones (``enabled`` not
    false) can own files. Lookups are cached per instance; build a new
    instance after the registry changes.
    """

    def __init__(self, entries: Iterable[dict[str, Any]]) -> None:
        prefixes: list[tuple[str, dict[str, Any]]] = []
        for entry in entries:
            if entry.get("enabled") is False:
                continue
            path = normalize_entry_path(entry.get("path"))
            if path:
                prefixes.append((_key(path), entry))
        # Deepest first; ties keep registry order (stable sort).
        prefixes.sort(key=lambda item: len(item[0]), reverse=True)
        self._prefixes = prefixes
        self._cache: dict[str, dict[str, Any] | None] = {}

    def owner(self, rel_path: str) -> dict[str, Any] | None:
        """The enabled entry that owns ``rel_path``, or ``None``."""
        key = _key(normalize_entry_path(rel_path))
        if key in self._cache:
            return self._cache[key]
        found: dict[str, Any] | None = None
        for prefix, entry in self._prefixes:
            if key == prefix or key.startswith(prefix + "/"):
                found = entry
                break
        self._cache[key] = found
        return found

    def owns(self, entry_id: str, rel_path: str) -> bool:
        """Whether the entry with ``entry_id`` owns ``rel_path``."""
        owner = self.owner(rel_path)
        return owner is not None and owner.get("id") == entry_id
