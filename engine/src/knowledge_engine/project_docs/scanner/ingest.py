"""Scanner Mode 2: ingest.

This module wires the read-only discovery layer to the write path. It runs a
pre-flight gate check, opens an ingestion run, discovers candidates, reads each
candidate's text (from the file on disk, or from a docstring span when the
detector already captured it), feeds every candidate through
:func:`knowledge_engine.project_docs.ingest.ingest_record`, and finally closes
the run with aggregate statistics.

Unlike Mode 1 (report), this mode writes to the project content DB. It is only
reachable when ``scanner.enabled`` is true: :func:`run` calls
:func:`scanner.validators.preflight` first, which raises
:class:`~knowledge_engine.project_docs.scanner.validators.GateError` if the
scanner is disabled. All file reads use UTF-8 with errors ignored so binary or
mis-encoded files degrade gracefully rather than crashing the run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import ingest as pipeline
from .. import schema
from ..config import ProjectDocsConfig
from ..models import Candidate
from . import base, discovery

# Importing the detector modules registers them with ``base``'s registry via
# their ``@register_detector`` decorators; ``base.iter_detectors()`` then
# returns one live instance of each.
from . import comments as _comments  # noqa: F401
from . import docstrings as _docstrings  # noqa: F401
from . import logs as _logs  # noqa: F401
from . import markdown as _markdown  # noqa: F401
from .validators import preflight


def _read_text(path: Path) -> str | None:
    """Read a file as UTF-8 with undecodable bytes ignored, or ``None``."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _candidate_text(root: Path, candidate: Candidate) -> str | None:
    """Return the text to ingest for ``candidate``, or ``None`` if unreadable.

    Sub-file candidates (e.g. docstrings) carry a 1-based inclusive
    ``(start_line, end_line)`` span; only those lines are read. Whole-file
    candidates have no span and are read in full. Both read the file referenced
    by ``candidate.source_path`` relative to ``root`` as UTF-8 with undecodable
    bytes ignored, degrading to ``None`` on I/O error.
    """
    # A sub-file candidate references its source file with a "path:symbol"
    # suffix; strip anything after the first colon that is not a drive letter.
    rel = candidate.source_path
    span = candidate.span

    file_path = root / rel
    text = _read_text(file_path)
    if text is None:
        return None

    if span is None:
        return text

    start, end = span
    lines = text.splitlines()
    # Spans are 1-based inclusive; clamp defensively.
    start_idx = max(start - 1, 0)
    end_idx = min(end, len(lines))
    return "\n".join(lines[start_idx:end_idx])


def run(
    root: Path,
    cfg: ProjectDocsConfig,
    project_conn: sqlite3.Connection,
    registry_conn: sqlite3.Connection,
    *,
    project_fp: str,
    branch_fp: str,
) -> dict:
    """Discover and ingest every candidate under ``root``; return run stats.

    Args:
        root: Project root to scan.
        cfg: Active project-docs config (gates the run).
        project_conn: Connection to the per-project content DB (written to).
        registry_conn: Connection to the shared registry DB (context check).
        project_fp: Validated project fingerprint for the active context.
        branch_fp: Validated branch fingerprint for the active context.

    Returns:
        A stats dict with ``candidates``, ``ingested``, ``skipped``,
        ``rejected``, ``unreadable``, and the ``run_id``.

    Raises:
        GateError: If ``scanner.enabled`` is false (or any other gate fails).
    """
    preflight("ingest", cfg, registry_conn, project_fp, branch_fp)

    run_id = pipeline.begin_run(
        project_conn, registry_conn, project_fp, branch_fp, "ingest"
    )

    stats = {
        "candidates": 0,
        "ingested": 0,
        "skipped": 0,
        "rejected": 0,
        "unreadable": 0,
        "run_id": run_id,
    }

    candidates = discovery.run_detectors(root, cfg, base.iter_detectors())
    for candidate in candidates:
        stats["candidates"] += 1
        text = _candidate_text(root, candidate)
        if text is None:
            stats["unreadable"] += 1
            continue

        record = pipeline.ingest_record(
            project_conn,
            registry_conn,
            project_fp=project_fp,
            branch_fp=branch_fp,
            source_path=candidate.source_path,
            category=candidate.category,
            subtype=candidate.subtype,
            text=text,
            cfg=cfg,
            run_id=run_id,
        )
        if record.ingestion_status == schema.INGESTED:
            stats["ingested"] += 1
        elif record.ingestion_status == schema.SKIPPED_DEDUPE:
            stats["skipped"] += 1
        elif record.ingestion_status == schema.REJECTED:
            stats["rejected"] += 1

    pipeline.finish_run(project_conn, run_id, stats)
    return stats
