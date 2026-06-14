"""Tests for the scanner report-only mode (``scanner/report.py``).

The report scan must list discovered candidates (including a Python docstring
candidate), recommend next actions, and write nothing — neither to a project DB
nor to any source file.
"""

from __future__ import annotations

from pathlib import Path

from knowledge_engine.project_docs import config as _config
from knowledge_engine.project_docs import db as _db
from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.scanner import report

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


def _make_project(tmp_path: Path) -> Path:
    """Create a tiny project with one markdown file and one Python module."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\nHello world.\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        '"""Module doc."""\n\n\ndef greet():\n'
        '    """Return a friendly greeting."""\n    return "hi"\n',
        encoding="utf-8",
    )
    return tmp_path


def _default_cfg():
    """Load the all-defaults config (no TOML file present)."""
    return _config.load_config(path=Path("nonexistent.toml"))


def _project_conn(tmp_path: Path):
    """Create a fresh project DB with migrations 002-007 applied."""
    conn = _db.connect(tmp_path / "project.sqlite")
    _db.apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    return conn


def test_report_lists_candidates_including_docstring(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    cfg = _default_cfg()

    rep = report.run(root, cfg)

    assert rep.mode == schema.MODE_REPORT
    assert rep.candidates, "expected at least one candidate"

    categories = {c.category for c in rep.candidates}
    assert schema.CATEGORY_DOCSTRING in categories, "expected a docstring candidate"
    assert schema.CATEGORY_DOC in categories, "expected a markdown doc candidate"

    docstring_cands = [
        c for c in rep.candidates if c.category == schema.CATEGORY_DOCSTRING
    ]
    # Each docstring candidate carries a (start, end) source span.
    assert all(c.span is not None for c in docstring_cands)


def test_report_recommended_actions_non_empty(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    cfg = _default_cfg()

    rep = report.run(root, cfg)

    assert rep.recommended_actions, "recommended_actions must be non-empty"
    assert all(isinstance(a, str) for a in rep.recommended_actions)
    assert rep.notes, "notes should describe the scan"


def test_report_writes_nothing_to_project_db(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    cfg = _default_cfg()
    conn = _project_conn(tmp_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM project_docs"
        ).fetchone()["c"]
        assert before == 0

        report.run(root, cfg, conn=conn)

        after = conn.execute(
            "SELECT COUNT(*) AS c FROM project_docs"
        ).fetchone()["c"]
        assert after == 0, "report mode must not write to the project DB"
    finally:
        conn.close()


def test_report_does_not_mutate_source(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    cfg = _default_cfg()

    py_path = root / "src" / "mod.py"
    md_path = root / "docs" / "guide.md"
    py_before = py_path.read_text(encoding="utf-8")
    md_before = md_path.read_text(encoding="utf-8")

    report.run(root, cfg)

    assert py_path.read_text(encoding="utf-8") == py_before
    assert md_path.read_text(encoding="utf-8") == md_before


def test_report_comments_disabled_by_default(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    cfg = _default_cfg()

    rep = report.run(root, cfg)

    # ``include_structured_comments`` defaults False, so no comment candidates.
    assert all(c.category != schema.CATEGORY_COMMENT for c in rep.candidates)
