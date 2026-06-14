"""Preflight gate checks for scanner operations.

Each scan mode (``schema.SCAN_MODES``) carries a different safety contract.
:func:`preflight` enforces those contracts before a potentially mutating
operation runs, raising :class:`GateError` with a message that names the gate
that failed. Read-only ``report`` scans are always permitted; ``ingest`` and
``pointer_apply`` require their respective configuration gates to be enabled.

A disabled capability is a configuration decision, not an exceptional bug, so
callers are expected to catch :class:`GateError` and translate it into a
structured (non-raising) tool result.
"""

from __future__ import annotations

from .. import schema


class GateError(Exception):
    """Raised when a preflight gate denies a scan mode."""


def preflight(mode, cfg, conn=None, project_fp=None, branch_fp=None) -> None:
    """Validate that ``mode`` is permitted under the current configuration.

    ``conn``, ``project_fp``, and ``branch_fp`` are accepted for forward
    compatibility with DB-aware gates (e.g. context validation) but are not
    required by the current checks.

    Raises:
        GateError: if ``mode`` is unknown or its required gates are not met.
    """
    _ = (conn, project_fp, branch_fp)

    if mode not in schema.SCAN_MODES:
        raise GateError(
            f"unknown scan mode {mode!r}; expected one of {sorted(schema.SCAN_MODES)}"
        )

    if mode == schema.MODE_REPORT:
        # Read-only: always permitted.
        return

    scanner = cfg.scanner

    if mode == schema.MODE_INGEST:
        if not scanner.enabled:
            raise GateError(
                "scan mode 'ingest' requires cfg.scanner.enabled=True "
                "(scanner is disabled)"
            )
        return

    if mode == schema.MODE_POINTER_APPLY:
        if not scanner.enabled:
            raise GateError(
                "scan mode 'pointer_apply' requires cfg.scanner.enabled=True "
                "(scanner is disabled)"
            )
        pointer = scanner.pointer_replacement
        if not pointer.enabled:
            raise GateError(
                "scan mode 'pointer_apply' requires "
                "cfg.scanner.pointer_replacement.enabled=True"
            )
        if not pointer.allow_source_mutation:
            raise GateError(
                "scan mode 'pointer_apply' requires "
                "cfg.scanner.pointer_replacement.allow_source_mutation=True"
            )
        if scanner.dry_run:
            raise GateError(
                "scan mode 'pointer_apply' requires cfg.scanner.dry_run=False"
            )
        return

    # MODE_POINTER_PLAN / MODE_EMBEDDING: plan-only and enrichment are read-safe
    # at the scanner level; their own modules enforce finer gates (embeddings
    # require cfg.embeddings.enabled). The scanner itself must be enabled.
    if not scanner.enabled:
        raise GateError(
            f"scan mode {mode!r} requires cfg.scanner.enabled=True "
            "(scanner is disabled)"
        )
