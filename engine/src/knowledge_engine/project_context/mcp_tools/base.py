"""Shared scaffolding for project-context MCP tool modules.

Each tool module is a file named ``*_tools.py`` in this package exposing three
module-level attributes:

* ``GROUP: str`` — capability group name (e.g. ``"query"``).
* ``def tools(cfg) -> list[dict]`` — MCP tool definitions for the group.
* ``def dispatch(name, args, ctx) -> dict`` — handle a call, return an MCP
  ``content`` envelope.

``collect_tools`` (in ``__init__``) discovers these modules dynamically, so new
tool groups are added simply by dropping a new ``*_tools.py`` file — no shared
registry edits required.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ...mcp.tool_groups import status_result, text_result
from .. import db as pddb, paths as pdpaths
from ..config import ProjectContextConfig

# ── Per-call execution context ───────────────────────────────────────


@dataclass
class ToolContext:
    """Lazily-opened connections and config for a tool invocation.

    The MCP SDK dispatches tool calls on worker threads, so the lazy opens are
    serialised by a lock: concurrent first calls share one connection (and
    one migration run) instead of racing to open several.
    """

    cfg: ProjectContextConfig
    root: Path
    _registry: sqlite3.Connection | None = field(default=None, repr=False)
    _projects: dict[str, sqlite3.Connection] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def registry_conn(self) -> sqlite3.Connection:
        """Open (once) the shared fingerprint registry DB with migration 001."""
        with self._lock:
            if self._registry is None:
                path = pdpaths.fingerprint_db_path(self.root, self.cfg)
                conn = pddb.connect(path)
                try:
                    pddb.apply_migrations(conn, only_prefixes=("001_",))
                except BaseException:
                    conn.close()
                    raise
                self._registry = conn
            return self._registry

    def project_slug_for(self, project_fp: str | None) -> str | None:
        """Resolve the on-disk slug for a project fingerprint (or current root)."""
        if project_fp is None:
            return pdpaths.slugify(self.root.name)
        row = (
            self.registry_conn()
            .execute("SELECT name FROM projects WHERE project_fp=?", (project_fp,))
            .fetchone()
        )
        if row is None:
            return None
        return pdpaths.slugify(row["name"])

    def project_conn(self, project_fp: str | None = None) -> sqlite3.Connection | None:
        """Open (once) a project content DB with migrations 002-007 applied.

        Returns ``None`` if ``project_fp`` is given but unknown.
        """
        slug = self.project_slug_for(project_fp)
        if slug is None:
            return None
        with self._lock:
            if slug not in self._projects:
                path = pdpaths.project_db_path(self.root, self.cfg, slug)
                conn = pddb.connect(path)
                try:
                    pddb.apply_migrations(
                        conn,
                        only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_"),
                    )
                except BaseException:
                    conn.close()
                    raise
                self._projects[slug] = conn
            return self._projects[slug]

    def close(self) -> None:
        with self._lock:
            for conn in (self._registry, *self._projects.values()):
                if conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        conn.close()
            self._registry = None
            self._projects.clear()


__all__ = ["ToolContext", "status_result", "text_result"]
