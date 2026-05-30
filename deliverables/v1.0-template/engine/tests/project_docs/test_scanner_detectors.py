"""Tests for scanner detectors (markdown, docstrings, comments, logs).

Each test builds a small temporary project tree and asserts the detector emits
the expected :class:`Candidate` records via its ``discover(root, cfg)`` method,
honouring its config gates. Tests are fully offline and touch only the
filesystem.
"""

from __future__ import annotations

from pathlib import Path

from knowledge_engine.project_docs.config import (
    ProjectDocsConfig,
    ScannerCfg,
    ScannerDiscoveryCfg,
)
from knowledge_engine.project_docs.scanner.comments import CommentDetector
from knowledge_engine.project_docs.scanner.docstrings import DocstringDetector
from knowledge_engine.project_docs.scanner.logs import LogDetector
from knowledge_engine.project_docs.scanner.markdown import MarkdownDetector
from knowledge_engine.project_docs.schema import (
    CATEGORY_BUILD_LOG,
    CATEGORY_COMMENT,
    CATEGORY_DEVLOG,
    CATEGORY_DOC,
    CATEGORY_DOCSTRING,
    CATEGORY_TEST_LOG,
)


def _cfg(**discovery_overrides: object) -> ProjectDocsConfig:
    """Build a config with the given discovery overrides applied."""
    discovery = ScannerDiscoveryCfg(**discovery_overrides)
    return ProjectDocsConfig(scanner=ScannerCfg(discovery=discovery))


def _write(root: Path, rel: str, content: str) -> None:
    """Write ``content`` to ``root/rel``, creating parent directories."""
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_markdown_detector_finds_doc_and_devlog(tmp_path: Path) -> None:
    """Markdown detector classifies a docs file as doc and a devlog as devlog."""
    _write(tmp_path, "docs/x.md", "# Title\n\nbody text\n")
    _write(tmp_path, "devlog/2026-05-30.md", "# Dev log\n\nworked on things\n")

    cfg = _cfg(include_markdown=True, include_devlogs=True)
    by_path = {c.source_path: c for c in MarkdownDetector().discover(tmp_path, cfg)}

    assert "docs/x.md" in by_path
    assert by_path["docs/x.md"].category == CATEGORY_DOC
    assert "devlog/2026-05-30.md" in by_path
    assert by_path["devlog/2026-05-30.md"].category == CATEGORY_DEVLOG
    assert by_path["docs/x.md"].detector == "markdown"


def test_markdown_detector_disabled_yields_nothing(tmp_path: Path) -> None:
    """Markdown detector is silent when include_markdown is off."""
    _write(tmp_path, "docs/x.md", "# Title\n")
    cfg = _cfg(include_markdown=False)
    assert list(MarkdownDetector().discover(tmp_path, cfg)) == []


def test_markdown_detector_skips_devlog_when_gate_off(tmp_path: Path) -> None:
    """Dev-log files are skipped (not reclassified) when include_devlogs off."""
    _write(tmp_path, "docs/x.md", "# Title\n")
    _write(tmp_path, "devlog/note.md", "# Dev log\n")
    cfg = _cfg(include_markdown=True, include_devlogs=False)
    paths = {c.source_path for c in MarkdownDetector().discover(tmp_path, cfg)}
    assert "docs/x.md" in paths
    assert "devlog/note.md" not in paths


def test_docstring_detector_finds_function_span(tmp_path: Path) -> None:
    """Docstring detector finds a function docstring with a line span."""
    source = (
        "def foo():\n"
        '    """Function docstring here."""\n'
        "    return 1\n"
    )
    _write(tmp_path, "pkg/mod.py", source)

    cfg = _cfg(include_docstrings=True)
    candidates = list(DocstringDetector().discover(tmp_path, cfg))

    functions = [c for c in candidates if c.subtype == "function"]
    assert len(functions) == 1
    cand = functions[0]
    assert cand.category == CATEGORY_DOCSTRING
    assert cand.span == (2, 2)
    assert "Function docstring here." in cand.preview
    assert cand.detector == "docstring"


def test_docstring_detector_disabled_yields_nothing(tmp_path: Path) -> None:
    """Docstring detector is silent when include_docstrings is off."""
    _write(tmp_path, "pkg/mod.py", 'def f():\n    """d."""\n    return 1\n')
    cfg = _cfg(include_docstrings=False)
    assert list(DocstringDetector().discover(tmp_path, cfg)) == []


def test_comment_detector_disabled_yields_nothing(tmp_path: Path) -> None:
    """Comment detector yields nothing when the gate is off (default)."""
    _write(tmp_path, "pkg/mod.py", "# leading comment\n# second line\n\nx = 1\n")
    cfg = _cfg(include_structured_comments=False)
    assert list(CommentDetector().discover(tmp_path, cfg)) == []


def test_comment_detector_finds_leading_block_when_enabled(tmp_path: Path) -> None:
    """Comment detector finds a leading block comment when enabled."""
    _write(tmp_path, "pkg/mod.py", "# leading comment\n# second line\n\nx = 1\n")
    cfg = _cfg(include_structured_comments=True)
    candidates = list(CommentDetector().discover(tmp_path, cfg))
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.category == CATEGORY_COMMENT
    assert cand.span == (1, 2)
    assert "leading comment" in cand.preview
    assert cand.detector == "comment"


def test_log_detector_finds_test_log_only_when_enabled(tmp_path: Path) -> None:
    """Log detector emits a test .log only when include_test_logs is on."""
    _write(tmp_path, "logs/test-run.log", "PASSED tests\n")

    off = _cfg(include_test_logs=False)
    assert list(LogDetector().discover(tmp_path, off)) == []

    on = _cfg(include_test_logs=True)
    candidates = list(LogDetector().discover(tmp_path, on))
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.category == CATEGORY_TEST_LOG
    assert cand.source_path == "logs/test-run.log"
    assert cand.detector == "log"


def test_log_detector_build_log_gate(tmp_path: Path) -> None:
    """Build logs are gated independently of test logs.

    The file lives under ``logs/`` (a ``build/`` directory is pruned by
    ``discovery.walk``'s ALWAYS_SKIP_DIRS), but its name still classifies it as
    a build log.
    """
    _write(tmp_path, "logs/build.log", "Compiling\n")

    off = _cfg(include_build_logs=False)
    assert list(LogDetector().discover(tmp_path, off)) == []

    on = _cfg(include_build_logs=True)
    candidates = list(LogDetector().discover(tmp_path, on))
    assert len(candidates) == 1
    assert candidates[0].category == CATEGORY_BUILD_LOG
