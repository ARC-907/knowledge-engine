"""scripts/check_docs_paths.py: the docs gate finds what it should and nothing else."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs_paths.py"


@pytest.fixture
def checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "check_docs_paths", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def _check(checker: ModuleType, path: Path) -> list[str]:
    return list(checker.check_file(path, {}))


def test_clean_file_has_no_problems(checker: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# other\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "See `knowledge_engine.core.settings` and `knowledge_engine.core.settings.Settings`.\n"
        "[other](other.md) [anchor](#top) [web](https://example.com)\n"
        "```\n[x](links-in-fences-are-not-checked.md)\n```\n",
        encoding="utf-8",
    )
    assert _check(checker, doc) == []


def test_broken_module_paths_and_links_are_reported(checker: ModuleType, tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "`knowledge_engine.nope` `knowledge_engine.core.settings.NoSuchName`\n"
        "[a](missing.md) ![img](missing.png)\n\n[ref]: missing-ref.md\n"
        "```\n`knowledge_engine.in_a_fence`\n```\n",
        encoding="utf-8",
    )
    problems = "\n".join(_check(checker, doc))
    assert "knowledge_engine.nope" in problems
    assert "knowledge_engine.core.settings.NoSuchName" in problems
    assert "missing.md" in problems
    assert "missing.png" in problems
    assert "missing-ref.md" in problems
    # Backticked module paths are checked inside code fences too.
    assert "knowledge_engine.in_a_fence" in problems


def test_file_outside_the_root_is_reported_by_path(checker: ModuleType, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("[x](gone.md)\n", encoding="utf-8")
    try:
        problems = _check(checker, outside)
    finally:
        outside.unlink()
    assert problems and str(outside) in problems[0]
