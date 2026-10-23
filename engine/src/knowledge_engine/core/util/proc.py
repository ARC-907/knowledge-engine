"""Subprocess helpers. :func:`run_git` is the engine's only ``git`` runner."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

#: Conservative timeout (seconds) so a hung or locked repository never stalls a caller.
GIT_TIMEOUT_SECONDS: float = 10.0


def run_git(
    args: Sequence[str],
    cwd: Path | str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and return the completed process.

    The argv is passed as a list with ``shell=False``, so no shell parsing
    happens, and ``check=False`` leaves the return code to the caller.
    Callers that pass untrusted refs must validate them (see
    ``project_context.git_context.validate_ref``) and terminate options with
    ``--end-of-options`` / ``--`` themselves.

    Raises:
        FileNotFoundError / OSError: the ``git`` binary could not be started.
        subprocess.TimeoutExpired: git did not finish within ``timeout`` seconds.
    """
    return subprocess.run(  # noqa: S603  # argv list with shell=False; refs are validated by callers
        ["git", *args],  # noqa: S607  # git is resolved from PATH like any other optional tool
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        check=False,  # the return code is inspected by the caller
    )


__all__ = ["GIT_TIMEOUT_SECONDS", "run_git"]
