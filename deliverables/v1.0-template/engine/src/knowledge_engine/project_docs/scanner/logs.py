"""Log-file detector.

Discovers ``*.log`` files and conventional test-output files under the project
root and classifies each into a log category - test, build, or runtime - using
filename heuristics. Each category has its own discovery gate:

* ``include_test_logs``    -> :data:`schema.CATEGORY_TEST_LOG`
* ``include_build_logs``   -> :data:`schema.CATEGORY_BUILD_LOG`
* ``include_runtime_logs`` -> :data:`schema.CATEGORY_RUNTIME_LOG`

A log whose category gate is off is skipped quietly. Log bodies are never read
into the database here; only a short preview is captured.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .. import schema
from ..models import Candidate
from .base import Detector, register_detector
from .discovery import walk

#: File extensions treated as candidate log artifacts.
_LOG_EXTENSIONS = frozenset({".log"})

#: Filenames (lowercased) recognised as log/test-output artifacts regardless of
#: extension.
_LOG_FILENAMES = frozenset(
    {
        "test-output.txt",
        "test_output.txt",
        "pytest-output.txt",
        "junit.xml",
        "test-results.xml",
        "test_results.xml",
    }
)

#: Number of preview characters captured from a log file head.
_PREVIEW_CHARS = 200


def _is_log_file(name: str) -> bool:
    """Return whether ``name`` looks like a log/test-output artifact."""
    lowered = name.lower()
    if lowered in _LOG_FILENAMES:
        return True
    return Path(lowered).suffix in _LOG_EXTENSIONS


def _classify(rel_posix: str) -> str:
    """Classify a log file's relative path into a log category.

    Returns one of :data:`schema.CATEGORY_TEST_LOG`,
    :data:`schema.CATEGORY_BUILD_LOG`, or :data:`schema.CATEGORY_RUNTIME_LOG`.
    """
    lowered = rel_posix.lower()
    if "test" in lowered or "pytest" in lowered or "junit" in lowered:
        return schema.CATEGORY_TEST_LOG
    if "build" in lowered or "compile" in lowered or "make" in lowered:
        return schema.CATEGORY_BUILD_LOG
    return schema.CATEGORY_RUNTIME_LOG


def _safe_preview(path: Path) -> str:
    """Return a short, single-line preview of a log file's head."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(_PREVIEW_CHARS)
    except OSError:
        return ""
    return " ".join(head.split())[:_PREVIEW_CHARS]


@register_detector
class LogDetector(Detector):
    """Detect test/build/runtime log artifacts.

    Always runs, but emits a candidate only when the per-category discovery gate
    for that log's classification is enabled.
    """

    name = "log"
    category = schema.CATEGORY_TEST_LOG

    #: Mapping from log category to its discovery-config attribute name.
    _GATE_ATTRS = {
        schema.CATEGORY_TEST_LOG: "include_test_logs",
        schema.CATEGORY_BUILD_LOG: "include_build_logs",
        schema.CATEGORY_RUNTIME_LOG: "include_runtime_logs",
    }

    def discover(self, root: Path, cfg) -> Iterator[Candidate]:
        """Yield candidates for permitted log artifacts under ``root``."""
        discovery = cfg.scanner.discovery
        root = Path(root)
        for path in walk(root, cfg):
            if not _is_log_file(path.name):
                continue
            rel_posix = path.relative_to(root).as_posix()
            category = _classify(rel_posix)
            if not self._category_enabled(discovery, category):
                continue
            try:
                est_bytes = path.stat().st_size
            except OSError:
                est_bytes = 0
            yield Candidate(
                source_path=rel_posix,
                category=category,
                subtype=category,
                est_bytes=est_bytes,
                risk_flags=(),
                span=None,
                preview=_safe_preview(path),
                detector=self.name,
            )

    def _category_enabled(self, discovery, category: str) -> bool:
        """Return whether the discovery gate for ``category`` is enabled."""
        attr = self._GATE_ATTRS.get(category)
        if attr is None:
            return False
        return bool(getattr(discovery, attr))
