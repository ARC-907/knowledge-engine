"""Project-Specific Documentation Library subsystem.

A local-first, provider-abstracted, SQLite-backed layer that ingests sanitized,
fingerprinted, branch-aware project documentation / logs / git-context and
exposes them through the engine's MCP surface. Everything beyond read-only FTS
search is opt-in and off by default (scanner, docstring-pointer rewriting,
embeddings, git diffs, raw-log retention).

See ``docs/PROJECT_DOCS.md`` and the design spec at
``docs/superpowers/specs/2026-05-30-project-docs-design.md``.
"""

from __future__ import annotations

from .config import ProjectDocsConfig, load_config
from .paths import resolve_project_root


def is_enabled(cfg: ProjectDocsConfig | None = None) -> bool:
    """True when the project-docs feature is turned on in config."""
    cfg = cfg or load_config()
    return cfg.enabled


__all__ = ["ProjectDocsConfig", "load_config", "resolve_project_root", "is_enabled"]
