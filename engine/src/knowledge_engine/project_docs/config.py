"""TOML configuration layer for the project-docs subsystem.

This is a *separate* config layer from the base engine's env-driven
``knowledge_engine.config.Config``. It reads an optional ``knowledge-engine.toml``
from the user's own project (not the engine repo). Discovery order:

1. ``KE_CONFIG_PATH`` environment variable (explicit path), else
2. walk up from ``start`` (default: CWD) looking for ``knowledge-engine.toml``.

If no file is found, **all defaults apply** and ``project_docs.enabled`` is
``False`` — the feature is dark until a user opts in. Every field has a safe,
conservative default baked into the dataclass, so a partial file is always valid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    import tomli as _toml  # type: ignore[no-redefined]

CONFIG_FILENAME = "knowledge-engine.toml"


# ── Nested config sections (mirror the design spec / brief TOML) ──────


@dataclass(frozen=True)
class ProjectsCfg:
    auto_register: bool = False
    require_explicit_project: bool = True


@dataclass(frozen=True)
class IngestionCfg:
    include_docs: bool = True
    include_devlogs: bool = True
    include_qa: bool = True
    include_tests: bool = True
    include_logs: bool = True
    include_git_metadata: bool = True
    include_diffs: bool = False
    max_document_bytes: int = 250_000
    max_log_bytes: int = 1_000_000
    sanitize_before_write: bool = True
    retain_raw_content: bool = False


@dataclass(frozen=True)
class ScannerDiscoveryCfg:
    include_markdown: bool = True
    include_docstrings: bool = True
    include_structured_comments: bool = False
    include_devlogs: bool = True
    include_test_logs: bool = True
    include_build_logs: bool = True
    include_runtime_logs: bool = False
    include_git_metadata: bool = True
    include_diffs: bool = False


@dataclass(frozen=True)
class PointerReplacementCfg:
    enabled: bool = False
    plan_only_by_default: bool = True
    apply_requires_explicit_confirm: bool = True
    pointer_scheme: str = "KE-DOCSTRING"
    write_backups: bool = True
    allow_source_mutation: bool = False


@dataclass(frozen=True)
class ScannerCfg:
    installed: bool = True
    enabled: bool = False
    mode: str = "report"
    dry_run: bool = True
    provider: str = "none"
    max_file_bytes: int = 500_000
    follow_symlinks: bool = False
    respect_gitignore: bool = True
    sanitize_before_write: bool = True
    discovery: ScannerDiscoveryCfg = field(default_factory=ScannerDiscoveryCfg)
    pointer_replacement: PointerReplacementCfg = field(default_factory=PointerReplacementCfg)


@dataclass(frozen=True)
class GitCfg:
    enabled: bool = True
    require_git: bool = False
    include_dirty_status: bool = True
    include_commit_metadata: bool = True
    include_history_summaries: bool = False
    include_diff_summaries: bool = False
    include_full_diffs: bool = False


@dataclass(frozen=True)
class EmbeddingsCfg:
    enabled: bool = False
    provider: str = "none"
    model: str = ""
    backend: str = "none"
    store_vectors: bool = False
    generate_on_ingest: bool = False
    allow_remote_provider: bool = False


@dataclass(frozen=True)
class McpCfg:
    enabled: bool = True
    default_result_mode: str = "summary"
    allow_full_content: bool = True
    allow_raw_logs: bool = False
    allow_embedding_search: bool = False
    allow_mutating_tools: bool = False


@dataclass(frozen=True)
class ProjectDocsConfig:
    enabled: bool = False
    database_dir: str = ".knowledge-engine/project-docs"
    fingerprint_database: str = ".knowledge-engine/project-fingerprints.sqlite"
    default_visibility: str = "local"
    projects: ProjectsCfg = field(default_factory=ProjectsCfg)
    ingestion: IngestionCfg = field(default_factory=IngestionCfg)
    scanner: ScannerCfg = field(default_factory=ScannerCfg)
    git: GitCfg = field(default_factory=GitCfg)
    embeddings: EmbeddingsCfg = field(default_factory=EmbeddingsCfg)
    mcp: McpCfg = field(default_factory=McpCfg)


# ── Loading ──────────────────────────────────────────────────────────


def find_config_file(start: Path | None = None) -> Path | None:
    """Locate ``knowledge-engine.toml``.

    ``KE_CONFIG_PATH`` wins if set and exists. Otherwise walk up from ``start``
    (default CWD) to the filesystem root. Returns ``None`` when not found.
    """
    env_path = os.environ.get("KE_CONFIG_PATH")
    if env_path:
        p = Path(env_path).expanduser()
        return p if p.exists() else None

    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _build(dc_type: type, data: dict[str, Any]) -> Any:
    """Construct a (possibly nested) frozen dataclass from a dict, ignoring
    unknown keys and using defaults for anything absent.

    ``from __future__ import annotations`` turns field annotations into strings,
    so we resolve them with ``get_type_hints`` to detect nested dataclasses.
    """
    hints = get_type_hints(dc_type)
    kwargs: dict[str, Any] = {}
    for f in fields(dc_type):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name)
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _build(ftype, value)  # type: ignore[arg-type]
        else:
            kwargs[f.name] = value
    return dc_type(**kwargs)


def load_config(start: Path | None = None, path: Path | None = None) -> ProjectDocsConfig:
    """Load the ``[project_docs]`` table into a :class:`ProjectDocsConfig`.

    ``path`` forces a specific file; otherwise discovery applies. A missing
    file yields all-default config (feature disabled).
    """
    cfg_path = path or find_config_file(start)
    if cfg_path is None or not Path(cfg_path).is_file():
        return ProjectDocsConfig()
    with open(cfg_path, "rb") as fh:
        doc = _toml.load(fh)
    table = doc.get("project_docs", {})
    if not isinstance(table, dict):
        return ProjectDocsConfig()
    return _build(ProjectDocsConfig, table)


def _as_dict(obj: Any) -> Any:
    """Recursively convert a frozen dataclass to a plain dict (for docs/tests)."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    return obj


#: Canonical default config rendered as a nested dict — used by docs and tests
#: so the documented defaults can never drift from the dataclass defaults.
PROJECT_DOCS_DEFAULTS: dict[str, Any] = _as_dict(ProjectDocsConfig())
