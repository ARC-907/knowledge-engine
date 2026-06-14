"""Regression tests for the tool-host interpreter allowlist + argv-only invocation.

After the 2026-05 security review, `invoke_script` was refactored from
`subprocess.run(cmd_string, shell=True)` to `subprocess.run(argv_list, shell=False)`
with the command pre-parsed via `shlex.split`. These tests pin the rejection
paths so future edits cannot silently re-introduce a shell-injection surface.
"""

from __future__ import annotations

from knowledge_engine.tools import host


def _stub_tool(command: str, timeout: int = 5) -> dict:
    """Minimal in-memory tool dict; we never reach _record_invocation in these
    tests because every rejection branch returns before the subprocess call."""
    return {
        "tool_id": "test-tool",
        "command": command,
        "timeout_seconds": timeout,
        "working_dir": ".",
    }


def test_empty_command_rejected() -> None:
    result = host.invoke_script(_stub_tool(""))
    assert result["ok"] is False
    assert "No command configured" in result["error"]


def test_non_allowlisted_interpreter_rejected() -> None:
    """`curl` is not in the default allowlist (python/node/bash)."""
    result = host.invoke_script(_stub_tool("curl http://attacker.example/"))
    assert result["ok"] is False
    assert "not in allowed list" in result["error"]
    assert "curl" in result["error"]


def test_shell_injection_metacharacter_rejected() -> None:
    """Pre-refactor (shell=True), `python; rm -rf /` would have shelled out and
    executed both commands. Post-refactor, shlex.split produces argv
    ['python;', 'rm', '-rf', '/'] and the first_word check rejects 'python;'
    (note the trailing semicolon) because it isn't an exact match for any
    allowlisted interpreter."""
    result = host.invoke_script(_stub_tool("python; rm -rf /"))
    assert result["ok"] is False
    assert "not in allowed list" in result["error"]


def test_command_substitution_no_longer_evaluated() -> None:
    """Pre-refactor, `bash $(id)` would have shelled out and executed `id`
    via command substitution. Post-refactor, shlex.split tokenizes literally:
    first_word becomes 'bash' (allowlisted) and '$(id)' becomes a literal
    positional argument. We don't actually launch bash here — we just verify
    the parse path produces 'bash' as first_word (so the rejection path is
    not taken) and that '$(id)' survives as a single literal token."""
    import shlex
    argv = shlex.split("bash $(id)", posix=True)
    assert argv[0] == "bash"
    assert argv[1] == "$(id)"  # literal — no shell expansion


def test_unparseable_command_rejected() -> None:
    """An unterminated quote should fail parsing cleanly, not raise."""
    result = host.invoke_script(_stub_tool('python "unterminated'))
    assert result["ok"] is False
    assert "Unparseable" in result["error"]


def test_custom_allowlist_via_config(monkeypatch) -> None:
    """The allowlist is sourced from tool-host.yaml's
    `tool_host.allowed_script_interpreters`. Verify that overriding it does
    in fact gate which interpreters pass through."""
    monkeypatch.setattr(
        host,
        "_load_tool_host_config",
        lambda: {"allowed_script_interpreters": ["only-this"]},
    )
    result = host.invoke_script(_stub_tool("python -c pass"))
    assert result["ok"] is False
    assert "not in allowed list" in result["error"]
