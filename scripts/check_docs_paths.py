#!/usr/bin/env python3
"""Check that the Markdown docs point at things that exist.

Standard library only. Two checks over every ``*.md`` file in the repository
(the ``.git``, virtualenv, ``node_modules`` and test-fixture trees are skipped):

1. Every backticked dotted path that starts with ``knowledge_engine.`` must
   resolve: either as a module (``importlib.util.find_spec``) or as a module
   plus one attribute (``knowledge_engine.core.settings.Settings``). Text
   inside fenced code blocks is checked too; the CHANGELOG is exempt because
   it records paths that no longer exist on purpose.
2. Every relative Markdown link (``[text](path)``, ``![alt](path)``,
   ``[label]: path``) must point at an existing file or directory. Fragments
   (``#heading``) are stripped first; ``http(s)://``, ``mailto:`` and pure
   ``#fragment`` links are ignored (the CI link checker covers those).

Run it from the project environment so the package is importable::

    uv run --project engine python scripts/check_docs_paths.py [PATH ...]

Exit status 0 when everything resolves, 1 otherwise; every problem is printed
as ``path:line: message``.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

#: Directories never scanned (relative to the repository root, or by name).
SKIP_DIR_NAMES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".knowledge-engine"}
)
SKIP_TREES = (ROOT / "engine" / "tests" / "fixtures",)

#: Files whose dotted paths are history, not claims about the current tree.
DOTTED_PATH_EXEMPT = frozenset({"CHANGELOG.md"})

DOTTED = re.compile(r"`(knowledge_engine(?:\.[A-Za-z_][A-Za-z0-9_]*)+)[^`]*`")
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
EXTERNAL = ("http://", "https://", "mailto:", "tel:", "data:")


def markdown_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for base in paths:
        if base.is_file():
            out.append(base)
            continue
        for path in sorted(base.rglob("*.md")):
            rel_parts = path.relative_to(ROOT).parts
            if any(part in SKIP_DIR_NAMES for part in rel_parts[:-1]):
                continue
            if any(path.is_relative_to(tree) for tree in SKIP_TREES):
                continue
            out.append(path)
    return out


def dotted_path_resolves(dotted: str, cache: dict[str, bool]) -> bool:
    if dotted in cache:
        return cache[dotted]
    ok = _is_module(dotted)
    if not ok and "." in dotted:
        parent, _, leaf = dotted.rpartition(".")
        if _is_module(parent):
            try:
                ok = hasattr(importlib.import_module(parent), leaf)
            except Exception:  # any import failure means "does not resolve"
                ok = False
    cache[dotted] = ok
    return ok


def _is_module(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ModuleNotFoundError, ValueError):
        return False
    except Exception:  # a parent package that fails to import
        return False


def check_file(path: Path, cache: dict[str, bool]) -> list[str]:
    problems: list[str] = []
    rel = path.relative_to(ROOT).as_posix()
    check_dotted = path.name not in DOTTED_PATH_EXEMPT
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if check_dotted:
            for match in DOTTED.finditer(line):
                dotted = match.group(1)
                if not dotted_path_resolves(dotted, cache):
                    problems.append(
                        f"{rel}:{number}: `{dotted}` does not resolve to a module or attribute"
                    )
        if in_fence:
            continue
        prose = INLINE_CODE.sub("", line)
        targets = [m.group(1) for m in INLINE_LINK.finditer(prose)]
        ref = REFERENCE_LINK.match(prose)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            if target.startswith(EXTERNAL) or target.startswith("#"):
                continue
            local = unquote(target.split("#", 1)[0])
            if not local:
                continue
            resolved = (
                (path.parent / local).resolve()
                if not local.startswith("/")
                else ROOT / local.lstrip("/")
            )
            if not resolved.exists():
                problems.append(f"{rel}:{number}: link target {target!r} does not exist")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = [Path(a).resolve() for a in (argv if argv is not None else sys.argv[1:])] or [ROOT]
    if importlib.util.find_spec("knowledge_engine") is None:
        print(
            "knowledge_engine is not importable; run this through the project environment, e.g. "
            "`uv run --project engine python scripts/check_docs_paths.py`",
            file=sys.stderr,
        )
        return 2
    cache: dict[str, bool] = {}
    problems: list[str] = []
    files = markdown_files(args)
    for path in files:
        problems.extend(check_file(path, cache))
    for problem in problems:
        print(problem)
    print(f"checked {len(files)} markdown file(s): {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
