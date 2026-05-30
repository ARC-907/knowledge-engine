"""Structured leading-comment detector.

Detects a leading block comment at the top of a source file (a contiguous run of
``#`` comment lines, optionally after a shebang) and emits a single
:class:`Candidate` describing it.

This capability is **off by default**: :meth:`discover` yields nothing unless
``cfg.scanner.discovery.include_structured_comments`` is ``True``. Leading
comments frequently embed licence headers, author names, and other content that
should not be ingested implicitly, so the gate stays conservative.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .. import schema
from ..models import Candidate
from .base import Detector, register_detector
from .discovery import walk

#: Source extensions whose leading ``#`` block comments are recognised.
_COMMENT_EXTENSIONS = frozenset(
    {".py", ".sh", ".rb", ".pl", ".toml", ".yaml", ".yml", ".cfg", ".ini"}
)

#: Number of preview characters captured from a comment block.
_PREVIEW_CHARS = 200

#: Maximum number of lines scanned when looking for a leading block comment.
_MAX_LEAD_LINES = 200


def _leading_block(lines: list[str]) -> tuple[int, int, str] | None:
    """Return the leading ``#`` comment block as ``(start, end, text)``.

    Skips a leading shebang line and blank lines preceding the block. ``start``
    and ``end`` are 1-based inclusive line numbers. Returns ``None`` when no
    leading comment block exists.
    """
    idx = 0
    n = min(len(lines), _MAX_LEAD_LINES)
    if idx < n and lines[idx].startswith("#!"):
        idx += 1
    while idx < n and not lines[idx].strip():
        idx += 1
    start = idx
    collected: list[str] = []
    while idx < n and lines[idx].lstrip().startswith("#"):
        collected.append(lines[idx].lstrip().lstrip("#").strip())
        idx += 1
    if not collected:
        return None
    text = " ".join(t for t in collected if t)
    # ``start`` and ``idx`` are 0-based; the block spans lines start..idx-1.
    return start + 1, idx, text


@register_detector
class CommentDetector(Detector):
    """Detect leading block comments as ingestion candidates.

    Disabled by default; controlled solely by the
    ``include_structured_comments`` discovery gate. When enabled, emits at most
    one candidate per file (its leading comment block).
    """

    name = "comment"
    category = schema.CATEGORY_COMMENT

    def discover(self, root: Path, cfg) -> Iterator[Candidate]:
        """Yield leading-comment candidates for source files under ``root``."""
        if not cfg.scanner.discovery.include_structured_comments:
            return
        root = Path(root)
        for path in walk(root, cfg):
            if path.suffix.lower() not in _COMMENT_EXTENSIONS:
                continue
            candidate = self._discover_file(path, root)
            if candidate is not None:
                yield candidate

    def _discover_file(self, path: Path, root: Path) -> Candidate | None:
        """Return a leading-comment candidate for a file, or ``None``."""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        block = _leading_block(lines)
        if block is None:
            return None
        start, end, text = block
        if not text:
            return None
        return Candidate(
            source_path=path.relative_to(root).as_posix(),
            category=schema.CATEGORY_COMMENT,
            subtype="leading_block",
            est_bytes=len(text.encode("utf-8")),
            risk_flags=(),
            span=(start, end),
            preview=text[:_PREVIEW_CHARS],
            detector=self.name,
        )
