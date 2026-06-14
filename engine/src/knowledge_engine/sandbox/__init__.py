"""Sandbox runner adapter scaffold.

Optional: integrate an external sandbox executor (subprocess, Docker, Firecracker, etc.).
Default `NoopSandbox` passes through.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int


class NoopSandbox:
    name = "noop"

    def run(self, command: list[str], stdin: str | None = None, timeout: float = 30.0) -> SandboxResult:
        return SandboxResult(stdout="", stderr="noop sandbox \u2014 not configured", returncode=127)


def get_sandbox() -> NoopSandbox:
    """Factory hook for plugging in a real sandbox implementation later."""
    return NoopSandbox()
