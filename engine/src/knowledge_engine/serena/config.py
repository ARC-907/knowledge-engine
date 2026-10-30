"""Serena adapter configuration: the Serena settings (``KE_SERENA_ENABLED``, ``KE_SERENA_COMMAND`` ...) and the operator project file.

The project file (``KE_SERENA_PROJECTS``, TOML or JSON, default
``<data_dir>/serena-projects.toml``) maps scoped-knowledge projects to
repositories::

    [[projects]]
    project_id = "p-app"  # a P1 project id (knowledge.db)
    root = "/srv/repos/app"  # absolute path of the repository
    repository_id = "example.org/app"  # optional; default: the root's name

Routing is by ``project_id`` only, never by the current working directory.
Malformed entries are reported and skipped; two entries for one project, or
two projects sharing a root, are errors (an entry per project keeps the
processes isolated).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..knowledge.contracts import OPAQUE_ID_PATTERN
from ..knowledge.errors import InvalidInputError

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

PROJECTS_FILENAME = "serena-projects.toml"
#: Data-dir sub-directory holding Serena's isolated HOME and logs.
STATE_DIRNAME = "serena"
_PROJECT_ID_RE = re.compile(OPAQUE_ID_PATTERN)
_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


@dataclass(frozen=True)
class SerenaProject:
    """One project the adapter may serve: its id, repository root and identity."""

    project_id: str
    root: Path
    repository_id: str


@dataclass(frozen=True)
class SerenaConfig:
    """Resolved adapter configuration (resolution does no I/O beyond reading the file)."""

    enabled: bool
    command: tuple[str, ...]
    expected_version: str
    projects_file: Path
    state_dir: Path
    request_timeout_s: float = 60.0
    startup_timeout_s: float = 300.0
    max_sessions: int = 4
    idle_shutdown_s: int = 900
    projects: dict[str, SerenaProject] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> SerenaConfig:
        paths = settings.resolve_paths(warn_missing_corpus=False)
        raw = (settings.serena_projects or "").strip()
        projects_file = Path(raw).expanduser() if raw else paths.data_dir / PROJECTS_FILENAME
        try:
            command = tuple(shlex.split(settings.serena_command))
        except ValueError as exc:
            _logger.warning("ignoring an unparseable KE_SERENA_COMMAND: %s", exc)
            command = ()
        projects: dict[str, SerenaProject] = {}
        warnings: tuple[str, ...] = ()
        if settings.serena_enabled:
            projects, warnings = load_projects(projects_file)
        return cls(
            enabled=settings.serena_enabled,
            command=command,
            expected_version=settings.serena_version.strip(),
            projects_file=projects_file,
            state_dir=paths.data_dir / STATE_DIRNAME,
            request_timeout_s=settings.serena_request_timeout_s,
            startup_timeout_s=settings.serena_startup_timeout_s,
            max_sessions=settings.serena_max_sessions,
            idle_shutdown_s=settings.serena_idle_shutdown_s,
            projects=projects,
            warnings=warnings,
        )

    @classmethod
    def from_env(cls) -> SerenaConfig:
        from ..core.settings import Settings  # noqa: PLC0415 - keep the import light

        return cls.from_settings(Settings())

    def project(self, project_id: str) -> SerenaProject:
        """The configured project ``project_id`` (``invalid_input`` when unknown)."""
        found = self.projects.get(project_id)
        if found is None:
            raise InvalidInputError(
                f"project {project_id!r} has no Serena repository configured",
                details={"project_id": project_id, "projects_file": str(self.projects_file)},
            )
        return found


def _read(path: Path) -> Any:
    data = path.read_bytes()
    if path.suffix.lower() == ".json":
        return json.loads(data.decode("utf-8"))
    return tomllib.loads(data.decode("utf-8"))


def load_projects(path: Path) -> tuple[dict[str, SerenaProject], tuple[str, ...]]:
    """Parse the project file; a missing file is no projects, a bad entry is skipped."""
    if not path.is_file():
        return {}, ()
    try:
        document = _read(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInputError(
            f"cannot read the Serena project file {path}: {exc}",
            details={"projects_file": str(path)},
        ) from exc
    entries = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise InvalidInputError(
            f"the Serena project file {path} has no `projects` list",
            details={"projects_file": str(path)},
        )
    projects: dict[str, SerenaProject] = {}
    roots: dict[Path, str] = {}
    warnings: list[str] = []
    for index, entry in enumerate(entries):
        problem = _entry_problem(entry)
        if problem is not None:
            message = f"projects[{index}] skipped: {problem}"
            _logger.warning("Serena project file %s: %s", path, message)
            warnings.append(message)
            continue
        project_id = entry["project_id"]
        root = Path(entry["root"]).expanduser().resolve()
        if project_id in projects:
            raise InvalidInputError(
                f"project {project_id!r} appears twice in {path}",
                details={"project_id": project_id},
            )
        if root in roots:
            raise InvalidInputError(
                f"projects {roots[root]!r} and {project_id!r} share the root {root}",
                details={"root": str(root)},
            )
        roots[root] = project_id
        repository_id = entry.get("repository_id") or root.name or project_id
        if not _REPOSITORY_ID_RE.fullmatch(repository_id):
            repository_id = project_id
        projects[project_id] = SerenaProject(project_id, root, repository_id)
    return projects, tuple(warnings)


def _entry_problem(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "not a table"
    project_id = entry.get("project_id")
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        return "project_id is missing or not a valid project id"
    root = entry.get("root")
    if not isinstance(root, str) or not root.strip():
        return "root is missing"
    target = Path(root).expanduser()
    if not target.is_absolute():
        return "root must be an absolute path"
    if not target.is_dir():
        return f"root {root!r} is not a directory"
    repository_id = entry.get("repository_id")
    if repository_id is not None and (
        not isinstance(repository_id, str) or not _REPOSITORY_ID_RE.fullmatch(repository_id)
    ):
        return "repository_id is not a valid identifier"
    unknown = sorted(set(entry) - {"project_id", "root", "repository_id"})
    if unknown:
        return f"unknown keys {unknown}"
    return None


__all__ = [
    "PROJECTS_FILENAME",
    "SerenaConfig",
    "SerenaProject",
    "load_projects",
]
