"""Tests for project-docs path resolution."""

from __future__ import annotations

import sys
from pathlib import Path

from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.paths import (
    canonical_root,
    fingerprint_db_path,
    project_db_path,
    project_docs_dir,
    slugify,
)


def test_slugify() -> None:
    assert slugify("My Project") == "my-project"
    assert slugify("a__b--c") == "a-b-c"
    assert slugify("Weird/Name!!") == "weirdname"
    assert slugify("") == "project"


def test_canonical_root_stable(tmp_path: Path) -> None:
    a = canonical_root(tmp_path)
    b = canonical_root(Path(str(tmp_path)))
    assert a == b
    if sys.platform == "win32":
        assert a == a.casefold()


def test_db_paths_under_configured_dirs(tmp_path: Path) -> None:
    cfg = ProjectDocsConfig()
    assert project_docs_dir(tmp_path, cfg) == (tmp_path / ".knowledge-engine" / "project-docs").resolve()
    assert project_db_path(tmp_path, cfg, "My Proj").name == "my-proj.sqlite"
    assert fingerprint_db_path(tmp_path, cfg).name == "project-fingerprints.sqlite"
