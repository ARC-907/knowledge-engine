"""Tests for the project-context scanner discovery and preflight gates."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_context import schema
from knowledge_engine.project_context.config import ProjectContextConfig, ScannerCfg
from knowledge_engine.project_context.models import Candidate
from knowledge_engine.project_context.scanner import discovery
from knowledge_engine.project_context.scanner.comments import CommentDetector
from knowledge_engine.project_context.scanner.docstrings import DocstringDetector
from knowledge_engine.project_context.scanner.logs import LogDetector
from knowledge_engine.project_context.scanner.markdown import MarkdownDetector
from knowledge_engine.project_context.scanner.validators import GateError, preflight

#: Tracked, sanitized sample project (engine/tests/fixtures/sample_project).
SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_project"


def _cfg(**scanner_kwargs) -> ProjectContextConfig:
    """Build a config whose scanner section is overridden for the test."""
    scanner = ScannerCfg(**scanner_kwargs)
    return ProjectContextConfig(scanner=scanner)


def _walk_names(root: Path, cfg: ProjectContextConfig) -> set[str]:
    """Return the set of file basenames yielded by ``walk``."""
    return {p.name for p in discovery.walk(root, cfg)}


def test_walk_yields_ordinary_files(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("hello", encoding="utf-8")
    cfg = _cfg(enabled=True, max_file_bytes=1_000_000)
    assert "keep.md" in _walk_names(tmp_path, cfg)


def test_walk_skips_oversize_file(tmp_path: Path) -> None:
    (tmp_path / "small.md").write_text("ok", encoding="utf-8")
    (tmp_path / "big.md").write_text("x" * 500, encoding="utf-8")
    cfg = _cfg(enabled=True, max_file_bytes=10)
    names = _walk_names(tmp_path, cfg)
    assert "small.md" in names
    assert "big.md" not in names


def test_walk_skips_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks not supported on this platform/permission set")
    if not link.is_symlink():
        pytest.skip("symlink creation did not produce a symlink")

    cfg = _cfg(enabled=True, follow_symlinks=False, max_file_bytes=1_000_000)
    names = _walk_names(tmp_path, cfg)
    assert "real.md" in names
    assert "link.md" not in names


def test_walk_skips_gitignored_path(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.md\nsecrets/\n", encoding="utf-8")
    (tmp_path / "kept.md").write_text("keep", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("nope", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "leak.md").write_text("nope", encoding="utf-8")

    cfg = _cfg(enabled=True, respect_gitignore=True, max_file_bytes=1_000_000)
    names = _walk_names(tmp_path, cfg)
    assert "kept.md" in names
    assert "ignored.md" not in names
    assert "leak.md" not in names


def test_walk_honors_gitignore_disabled(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("nope", encoding="utf-8")
    cfg = _cfg(enabled=True, respect_gitignore=False, max_file_bytes=1_000_000)
    names = _walk_names(tmp_path, cfg)
    assert "ignored.md" in names


def test_walk_always_skips_git_dir(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("x", encoding="utf-8")
    (tmp_path / "doc.md").write_text("y", encoding="utf-8")
    cfg = _cfg(enabled=True, respect_gitignore=False, max_file_bytes=1_000_000)
    names = _walk_names(tmp_path, cfg)
    assert "doc.md" in names
    assert "config" not in names


def test_walk_sample_project_fixture_finds_expected_files() -> None:
    """A real walk over the tracked sample project yields its source, docs, and devlog."""
    assert SAMPLE_PROJECT.is_dir(), f"fixture missing: {SAMPLE_PROJECT}"
    cfg = _cfg(enabled=True)
    walked = {p.relative_to(SAMPLE_PROJECT).as_posix() for p in discovery.walk(SAMPLE_PROJECT, cfg)}
    assert {
        "src/widget.py",
        "docs/overview.md",
        "devlog/2026-01-01-init.md",
        "logs/test-output.txt",
    } <= walked


def test_run_detectors_on_sample_project_fixture() -> None:
    """The real detectors classify the sample project's files into the expected categories."""
    cfg = _cfg(enabled=True)  # default discovery gates
    detectors = [MarkdownDetector(), DocstringDetector(), CommentDetector(), LogDetector()]
    candidates = discovery.run_detectors(SAMPLE_PROJECT, cfg, detectors)

    categories: dict[str, set[str]] = {}
    for cand in candidates:
        categories.setdefault(cand.source_path, set()).add(cand.category)

    assert schema.CATEGORY_DOC in categories["docs/overview.md"]
    assert schema.CATEGORY_DEVLOG in categories["devlog/2026-01-01-init.md"]
    assert schema.CATEGORY_DOCSTRING in categories["src/widget.py"]
    assert schema.CATEGORY_TEST_LOG in categories["logs/test-output.txt"]

    # Module, ``validate`` and ``handle`` docstrings, each with a source span.
    widget = [c for c in candidates if c.source_path == "src/widget.py"]
    assert {c.subtype for c in widget} >= {"module", "function"}
    assert len(widget) == 3
    assert all(c.span is not None for c in widget)


def test_run_detectors_isolates_failure(tmp_path: Path) -> None:
    class GoodDetector:
        name = "good"

        def discover(self, root, cfg):
            return [
                Candidate(
                    source_path=str(root / "a.md"),
                    category=schema.CATEGORY_DOC,
                    detector="good",
                )
            ]

    class BadDetector:
        name = "bad"

        def discover(self, root, cfg):
            raise RuntimeError("boom")

    cfg = _cfg(enabled=True)
    result = discovery.run_detectors(tmp_path, cfg, [BadDetector(), GoodDetector()])
    assert [c.detector for c in result] == ["good"]


def test_preflight_report_always_allowed() -> None:
    cfg = _cfg(enabled=False)
    # Should not raise even with the scanner disabled.
    preflight(schema.MODE_REPORT, cfg)


def test_preflight_ingest_blocked_when_disabled() -> None:
    cfg = _cfg(enabled=False)
    with pytest.raises(GateError):
        preflight(schema.MODE_INGEST, cfg)


def test_preflight_ingest_allowed_when_enabled() -> None:
    cfg = _cfg(enabled=True)
    preflight(schema.MODE_INGEST, cfg)


def test_preflight_unknown_mode_raises() -> None:
    cfg = _cfg(enabled=True)
    with pytest.raises(GateError):
        preflight("nonsense", cfg)


def test_preflight_pointer_apply_requires_full_chain() -> None:
    base = _cfg(enabled=True, dry_run=True)
    # pointer_replacement disabled by default and dry_run True -> blocked.
    with pytest.raises(GateError):
        preflight(schema.MODE_POINTER_APPLY, base)

    pr = base.scanner.pointer_replacement
    enabled_pr = replace(pr, enabled=True, allow_source_mutation=True)
    scanner = replace(base.scanner, pointer_replacement=enabled_pr, dry_run=False)
    cfg = replace(base, scanner=scanner)
    # Full chain satisfied: scanner enabled + pointer enabled + mutation + not dry-run.
    preflight(schema.MODE_POINTER_APPLY, cfg)

    # dry_run still True -> blocked even with pointer gates on.
    dry_scanner = replace(base.scanner, pointer_replacement=enabled_pr, dry_run=True)
    dry_cfg = replace(base, scanner=dry_scanner)
    with pytest.raises(GateError):
        preflight(schema.MODE_POINTER_APPLY, dry_cfg)

    # allow_source_mutation off -> blocked.
    no_mut = replace(enabled_pr, allow_source_mutation=False)
    no_mut_scanner = replace(base.scanner, pointer_replacement=no_mut, dry_run=False)
    no_mut_cfg = replace(base, scanner=no_mut_scanner)
    with pytest.raises(GateError):
        preflight(schema.MODE_POINTER_APPLY, no_mut_cfg)
