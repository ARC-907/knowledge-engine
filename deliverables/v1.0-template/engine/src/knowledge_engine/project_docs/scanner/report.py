"""Scanner Mode 1: report-only.

:func:`run` walks a project root with the registered detectors, collects the
:class:`~knowledge_engine.project_docs.models.Candidate` objects each one emits,
probes optional git availability, and assembles a
:class:`~knowledge_engine.project_docs.models.ScanReport` summarising what an
``ingest`` run *would* do. It is strictly read-only: it never writes to any
database (even when a ``conn`` is supplied) and never mutates a source file.

Detector selection follows ``cfg.scanner.discovery``: each detector self-gates
on its own discovery flag (e.g. ``include_markdown``, ``include_docstrings``),
so this module simply runs every registered detector and lets the disabled ones
yield nothing. Importing the detector modules here guarantees they have
registered themselves before :func:`~..scanner.base.iter_detectors` is called.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .. import git_context, schema
from ..models import Candidate, ScanReport

# Importing the detector modules triggers their ``@register_detector`` side
# effects, so ``iter_detectors`` returns a populated registry regardless of
# whether the caller imported them first. (Re-registration is idempotent.)
from . import comments as _comments  # noqa: F401
from . import docstrings as _docstrings  # noqa: F401
from . import logs as _logs  # noqa: F401
from . import markdown as _markdown  # noqa: F401
from .base import iter_detectors
from .discovery import run_detectors

logger = logging.getLogger(__name__)


def _enabled_detectors(cfg) -> list:
    """Return detector instances whose discovery gate is plausibly enabled.

    Detectors already self-gate inside ``discover``; this pre-filter is a cheap
    optimisation that avoids walking the tree for a detector that is wholly
    disabled by config. A detector with no recognised gate is always kept.
    """
    discovery = cfg.scanner.discovery
    gates = {
        "markdown": "include_markdown",
        "docstring": "include_docstrings",
        "comment": "include_structured_comments",
    }
    kept = []
    for detector in iter_detectors():
        attr = gates.get(getattr(detector, "name", ""))
        if attr is not None and not bool(getattr(discovery, attr, False)):
            continue
        kept.append(detector)
    return kept


def _git_available(root: Path, cfg) -> bool:
    """Return whether git context is available for ``root`` (never raises)."""
    try:
        return git_context.collect(root, cfg) is not None
    except Exception:  # noqa: BLE001 - git is optional; absence must not break a report
        logger.debug("git_context.collect failed during report scan", exc_info=True)
        return False


def _recommended_actions(
    candidates: list[Candidate],
    cfg,
    git_available: bool,
) -> tuple[str, ...]:
    """Build human-facing next-step suggestions from the scan results."""
    actions: list[str] = []
    total = len(candidates)
    by_category = Counter(c.category for c in candidates)

    if total == 0:
        actions.append(
            "no ingestable candidates found; check cfg.scanner.discovery flags "
            "and the project layout"
        )
        return tuple(actions)

    if not cfg.scanner.enabled:
        actions.append(
            f"enable scanner.ingest to store {total} candidate(s) "
            "(set cfg.scanner.enabled=True)"
        )
    else:
        actions.append(f"run scanner ingest to store {total} candidate(s)")

    docstrings = by_category.get(schema.CATEGORY_DOCSTRING, 0)
    if docstrings:
        actions.append(
            f"{docstrings} docstring(s) eligible for pointer plan "
            "(scanner pointer_plan, no source writes)"
        )

    log_total = sum(by_category.get(cat, 0) for cat in schema.LOG_CATEGORIES)
    if log_total:
        actions.append(f"{log_total} log file(s) discovered for log ingestion")

    if not git_available:
        actions.append(
            "git context unavailable; commit/branch lineage will be omitted"
        )

    return tuple(actions)


def _notes(candidates: list[Candidate], cfg, git_available: bool) -> tuple[str, ...]:
    """Build informational notes describing the scan and its configuration."""
    by_detector = Counter(c.detector for c in candidates)
    total_bytes = sum(c.est_bytes for c in candidates)
    detector_summary = ", ".join(
        f"{name}={count}" for name, count in sorted(by_detector.items())
    ) or "none"
    return (
        f"scanned candidates by detector: {detector_summary}",
        f"estimated total source bytes: {total_bytes}",
        f"scanner.enabled={cfg.scanner.enabled}, git_available={git_available}",
        "report mode performs no database or source-file writes",
    )


def run(
    root: Path | str,
    cfg: Any,
    conn: sqlite3.Connection | None = None,
) -> ScanReport:
    """Run a report-only scan of ``root`` and return a :class:`ScanReport`.

    Args:
        root: Project root to scan.
        cfg: A loaded :class:`~knowledge_engine.project_docs.config.ProjectDocsConfig`.
        conn: Optional project DB connection. Accepted for symmetry with the
            ingest mode and to let callers pass an open store, but it is treated
            as strictly read-only here — nothing is written to it.

    Returns:
        A :class:`ScanReport` listing the discovered candidates, git
        availability, recommended next actions, and informational notes. No
        database or source file is modified.
    """
    _ = conn  # report mode never writes; accepted only for call-site symmetry
    root_path = Path(root)

    detectors = _enabled_detectors(cfg)
    candidates = run_detectors(root_path, cfg, detectors)
    git_available = _git_available(root_path, cfg)

    return ScanReport(
        root=str(root_path),
        mode=schema.MODE_REPORT,
        candidates=candidates,
        git_available=git_available,
        recommended_actions=_recommended_actions(candidates, cfg, git_available),
        notes=_notes(candidates, cfg, git_available),
    )
