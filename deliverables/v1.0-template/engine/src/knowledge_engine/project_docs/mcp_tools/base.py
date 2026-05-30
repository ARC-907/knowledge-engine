"""Shared scaffolding for project-docs MCP tool modules.

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

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ProjectDocsConfig
from .. import db as pddb
from .. import paths as pdpaths


# ── MCP result envelopes ─────────────────────────────────────────────


def text_result(obj: Any) -> dict[str, Any]:
    """Wrap a JSON-serializable object as an MCP text content envelope."""
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, default=str)}]}


def status_result(status: str, **extra: Any) -> dict[str, Any]:
    """Convenience envelope carrying a machine-readable ``status`` field.

    Used for the disabled / not-configured / not-permitted responses that let an
    agent discover capability instead of hitting an error.
    """
    payload = {"status": status}
    payload.update(extra)
    return text_result(payload)


# ── Per-call execution context ───────────────────────────────────────


@dataclass
class ToolContext:
    """Lazily-opened connections and config for a tool invocation."""

    cfg: ProjectDocsConfig
    root: Path
    _registry: sqlite3.Connection | None = field(default=None, repr=False)
    _projects: dict[str, sqlite3.Connection] = field(default_factory=dict, repr=False)

    def registry_conn(self) -> sqlite3.Connection:
        """Open (once) the shared fingerprint registry DB with migration 001."""
        if self._registry is None:
            path = pdpaths.fingerprint_db_path(self.root, self.cfg)
            conn = pddb.connect(path)
            pddb.apply_migrations(conn, only_prefixes=("001_",))
            self._registry = conn
        return self._registry

    def project_slug_for(self, project_fp: str | None) -> str | None:
        """Resolve the on-disk slug for a project fingerprint (or current root)."""
        if project_fp is None:
            return pdpaths.slugify(self.root.name)
        row = self.registry_conn().execute(
            "SELECT name FROM projects WHERE project_fp=?", (project_fp,)
        ).fetchone()
        if row is None:
            return None
        return pdpaths.slugify(row["name"])

    def project_conn(self, project_fp: str | None = None) -> sqlite3.Connection | None:
        """Open (once) a project content DB with migrations 002–007 applied.

        Returns ``None`` if ``project_fp`` is given but unknown.
        """
        slug = self.project_slug_for(project_fp)
        if slug is None:
            return None
        if slug not in self._projects:
            path = pdpaths.project_db_path(self.root, self.cfg, slug)
            conn = pddb.connect(path)
            pddb.apply_migrations(
                conn,
                only_prefixes=("002_", "003_", "004_", "005_", "006_", "007_"),
            )
            self._projects[slug] = conn
        return self._projects[slug]

    def close(self) -> None:
        for conn in (self._registry, *self._projects.values()):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        self._registry = None
        self._projects.clear()
