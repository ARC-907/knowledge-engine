"""Markdown documentation detector.

Discovers ``*.md`` files under the project root and classifies each into a
documentation category (dev log, Q&A, design note, decision record, test plan)
using cheap filename/directory heuristics, falling back to the generic ``doc``
category. Classification never reads the full body into the database; only a
short, safe preview is produced.

Discovery is gated by ``cfg.scanner.discovery``:

* ``include_markdown`` enables the detector at all; when off, :meth:`discover`
  yields nothing.
* ``include_devlogs`` controls whether dev-log files are emitted. When it is
  off, files that *would* be classified as dev logs are skipped quietly rather
  than being reclassified as generic docs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .. import schema
from ..models import Candidate
from .base import Detector, register_detector
from .discovery import walk

#: Number of preview characters captured from the head of a file.
_PREVIEW_CHARS = 200


def _classify(rel_posix: str) -> tuple[str, str]:
    """Classify a markdown file by its relative (posix) path.

    Args:
        rel_posix: File path relative to the project root, using ``/`` separators.

    Returns:
        A ``(category, subtype)`` pair. ``category`` is one of
        :data:`schema.CATEGORIES`; ``subtype`` is a finer free-form label.
    """
    lowered = rel_posix.lower()
    parts = lowered.split("/")
    name = parts[-1]
    dirs = parts[:-1]

    def in_dirs(*needles: str) -> bool:
        return any(any(n in segment for n in needles) for segment in dirs)

    def in_any(*needles: str) -> bool:
        return any(any(n in segment for n in needles) for segment in parts)

    if in_dirs("devlog", "dev-log") or "devlog" in name or "dev-log" in name:
        return schema.CATEGORY_DEVLOG, "devlog"
    if in_any("q-and-a", "q_and_a") or "qa" in name or in_dirs("qa"):
        return schema.CATEGORY_QA, "qa"
    if in_any("design-note", "design_note"):
        return schema.CATEGORY_DESIGN_NOTE, "design_note"
    if in_any("adr", "decision-record", "decision_record") or "decision" in name:
        return schema.CATEGORY_DECISION_RECORD, "decision_record"
    if in_any("test-plan", "test_plan"):
        return schema.CATEGORY_TEST_PLAN, "test_plan"
    return schema.CATEGORY_DOC, "doc"


def _safe_preview(path: Path) -> str:
    """Return a short, single-line preview of a text file's head.

    Reads at most :data:`_PREVIEW_CHARS` characters and collapses whitespace so
    the preview stays single-line. Read errors degrade to an empty preview.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(_PREVIEW_CHARS)
    except OSError:
        return ""
    return " ".join(head.split())[:_PREVIEW_CHARS]


@register_detector
class MarkdownDetector(Detector):
    """Detect markdown documentation artifacts.

    Emits one whole-file :class:`Candidate` per ``*.md`` file, classified by
    path heuristics. Honours the ``include_markdown`` and ``include_devlogs``
    discovery gates.
    """

    name = "markdown"
    category = schema.CATEGORY_DOC

    def discover(self, root: Path, cfg) -> Iterator[Candidate]:
        """Yield candidates for markdown documentation files under ``root``."""
        discovery = cfg.scanner.discovery
        if not discovery.include_markdown:
            return
        include_devlogs = bool(discovery.include_devlogs)
        root = Path(root)
        for path in walk(root, cfg):
            if path.suffix.lower() != ".md":
                continue
            rel_posix = path.relative_to(root).as_posix()
            category, subtype = _classify(rel_posix)
            if category == schema.CATEGORY_DEVLOG and not include_devlogs:
                continue
            try:
                est_bytes = path.stat().st_size
            except OSError:
                est_bytes = 0
            yield Candidate(
                source_path=rel_posix,
                category=category,
                subtype=subtype,
                est_bytes=est_bytes,
                risk_flags=(),
                span=None,
                preview=_safe_preview(path),
                detector=self.name,
            )
