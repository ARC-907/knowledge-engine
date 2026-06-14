"""Python docstring detector.

Parses ``*.py`` files with the standard library :mod:`ast` module and emits one
:class:`Candidate` per module/class/function docstring. Each candidate carries a
1-based inclusive ``(start_line, end_line)`` span locating the docstring literal
within its source file.

Discovery is gated by ``cfg.scanner.discovery.include_docstrings``. Files that
fail to parse (syntax errors, encoding problems) are skipped quietly so a single
malformed module never aborts a scan.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from .. import schema
from ..models import Candidate
from .base import Detector, register_detector
from .discovery import walk

#: Number of preview characters captured from a docstring.
_PREVIEW_CHARS = 200

#: AST node types that may own a docstring, mapped to a candidate subtype.
_DOC_OWNERS: tuple[tuple[type[ast.AST], str], ...] = (
    (ast.Module, "module"),
    (ast.ClassDef, "class"),
    (ast.FunctionDef, "function"),
    (ast.AsyncFunctionDef, "function"),
)


def _subtype_for(node: ast.AST) -> str | None:
    """Return the candidate subtype for a docstring-owning node, else ``None``."""
    for node_type, subtype in _DOC_OWNERS:
        if isinstance(node, node_type):
            return subtype
    return None


def _docstring_span(node: ast.AST) -> tuple[int, int] | None:
    """Return the 1-based inclusive line span of ``node``'s docstring literal.

    Returns ``None`` when the node has no string-literal docstring expression or
    the literal lacks line information.
    """
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
        return None
    if not isinstance(first.value.value, str):
        return None
    start = getattr(first, "lineno", None)
    if start is None:
        return None
    end = getattr(first, "end_lineno", None) or start
    return start, end


def _preview(text: str) -> str:
    """Return a short, single-line preview of a docstring."""
    return " ".join(text.split())[:_PREVIEW_CHARS]


@register_detector
class DocstringDetector(Detector):
    """Detect Python docstrings as sub-file ingestion candidates.

    Honours the ``include_docstrings`` discovery gate. Each candidate's ``span``
    locates the docstring within its file; the category is
    :data:`schema.CATEGORY_DOCSTRING`.
    """

    name = "docstring"
    category = schema.CATEGORY_DOCSTRING

    def discover(self, root: Path, cfg) -> Iterator[Candidate]:
        """Yield candidates for docstrings in Python files under ``root``."""
        if not cfg.scanner.discovery.include_docstrings:
            return
        root = Path(root)
        for path in walk(root, cfg):
            if path.suffix.lower() != ".py":
                continue
            yield from self._discover_file(path, root)

    def _discover_file(self, path: Path, root: Path) -> Iterator[Candidate]:
        """Yield docstring candidates for a single Python file."""
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            return
        rel_posix = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            subtype = _subtype_for(node)
            if subtype is None:
                continue
            text = ast.get_docstring(node, clean=False)
            if text is None:
                continue
            span = _docstring_span(node)
            if span is None:
                continue
            yield Candidate(
                source_path=rel_posix,
                category=schema.CATEGORY_DOCSTRING,
                subtype=subtype,
                est_bytes=len(text.encode("utf-8")),
                risk_flags=(),
                span=span,
                preview=_preview(text),
                detector=self.name,
            )
