"""Hardening tests for the tool host: script argv validation, proxy allowlists,
upstream URL checks, and persistence of the new per-tool fields.

Everything is offline: ``subprocess.run`` and the HTTP opener are replaced with
recorders, and DB-backed tests use a throwaway SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
from http.client import HTTPMessage
from pathlib import Path
from typing import Any
from urllib import error as urllib_error, request as urllib_request

import pytest

from knowledge_engine.runtime.foundation import db as db_mod
from knowledge_engine.tool_host import host

# ── fixtures ─────────────────────────────────────────────────────────


class _RunRecorder:
    def __init__(self, stdout: str = '{"ok": true}', returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


@pytest.fixture
def script_env(tmp_path: Path, monkeypatch):
    """Isolated tool root, pipeline root, config, invocation counter and runner."""
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    pipeline_root = tmp_path / "pipeline"
    (pipeline_root / "scripts").mkdir(parents=True)
    runner = _RunRecorder()
    invoked: list[str] = []
    monkeypatch.setattr(host, "PIPELINE_ROOT", pipeline_root)
    monkeypatch.setattr(host, "_load_tool_host_config", dict)
    monkeypatch.setattr(host, "_record_invocation", invoked.append)
    monkeypatch.setattr(host.subprocess, "run", runner)
    return {
        "tmp": tmp_path,
        "root": tool_root,
        "pipeline": pipeline_root,
        "run": runner,
        "invoked": invoked,
    }


def _script_tool(command: str, working_dir: Path, **extra: Any) -> dict[str, Any]:
    return {
        "tool_id": "tool-123",
        "command": command,
        "timeout_seconds": 5,
        "working_dir": str(working_dir),
        **extra,
    }


# ── invoke_script: inline code and stray options are denied ─────────


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'echo pwned'",
        "python -c 'import os'",
        "python -m http.server",
        "node -e 'process.exit(1)'",
        "node --eval 1",
        "python -",
        "python -I run.py",
        "bash --norc run.sh",
    ],
)
def test_inline_code_and_options_denied(script_env, command: str) -> None:
    (script_env["root"] / "run.py").write_text("print(1)\n", encoding="utf-8")
    (script_env["root"] / "run.sh").write_text("echo 1\n", encoding="utf-8")
    result = host.invoke_script(_script_tool(command, script_env["root"]))
    assert result["ok"] is False
    assert "allow_inline" in result["error"]
    assert script_env["run"].calls == []
    assert script_env["invoked"] == []


def test_interpreter_without_script_denied(script_env) -> None:
    result = host.invoke_script(_script_tool("python", script_env["root"]))
    assert result["ok"] is False
    assert "must name a script" in result["error"]
    assert script_env["run"].calls == []


@pytest.mark.parametrize("kind", ["relative", "absolute", "symlink"])
def test_script_outside_root_denied(script_env, kind: str) -> None:
    outside = script_env["tmp"] / "outside.py"
    outside.write_text("print('escaped')\n", encoding="utf-8")
    if kind == "relative":
        command = "python ../outside.py"
    elif kind == "absolute":
        command = f"python {outside}"
    else:
        link = script_env["root"] / "link.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
            pytest.skip(f"symlinks unavailable: {exc}")
        command = "python link.py"
    result = host.invoke_script(_script_tool(command, script_env["root"]))
    assert result["ok"] is False
    assert "outside the tool's root" in result["error"]
    assert script_env["run"].calls == []


def test_script_with_disallowed_extension_denied(script_env) -> None:
    (script_env["root"] / "notes.txt").write_text("print(1)\n", encoding="utf-8")
    result = host.invoke_script(_script_tool("python notes.txt", script_env["root"]))
    assert result["ok"] is False
    assert "extensions" in result["error"]
    assert script_env["run"].calls == []


def test_missing_script_denied(script_env) -> None:
    result = host.invoke_script(_script_tool("python nope.py", script_env["root"]))
    assert result["ok"] is False
    assert "Script not found" in result["error"]
    assert script_env["run"].calls == []


def test_allowed_script_runs_with_its_arguments(script_env, caplog) -> None:
    script = script_env["root"] / "run.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    tool = _script_tool("python run.py --mode fast -c literal-arg", script_env["root"])

    with caplog.at_level("INFO", logger=host.__name__):
        result = host.invoke_script(tool, {"x": 1})

    assert result == {"ok": True, "returncode": 0, "output": {"ok": True}, "stderr": None}
    (call,) = script_env["run"].calls
    assert call["argv"] == ["python", "run.py", "--mode", "fast", "-c", "literal-arg"]
    assert call["shell"] is False
    assert call["cwd"] == str(script_env["root"])
    assert json.loads(call["input"]) == {"x": 1}
    assert script_env["invoked"] == ["tool-123"]
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("tool-123" in m and "running python" in m and "run.py" in m for m in messages)
    assert any("tool-123" in m and "exited 0" in m for m in messages)


@pytest.mark.parametrize("command", ["node app.mjs", "node app.js", "bash job.sh"])
def test_other_allowlisted_interpreters_accept_scripts(script_env, command: str) -> None:
    (script_env["root"] / command.split()[1]).write_text("// script\n", encoding="utf-8")
    result = host.invoke_script(_script_tool(command, script_env["root"]))
    assert result["ok"] is True
    assert len(script_env["run"].calls) == 1


def test_script_under_pipeline_root_accepted(script_env) -> None:
    script = script_env["pipeline"] / "scripts" / "job.py"
    script.write_text("print(1)\n", encoding="utf-8")
    result = host.invoke_script(_script_tool(f"python {script}", script_env["root"]))
    assert result["ok"] is True
    assert script_env["run"].calls[0]["argv"] == ["python", str(script)]


def test_working_dir_defaults_to_pipeline_root(script_env) -> None:
    (script_env["pipeline"] / "job.py").write_text("print(1)\n", encoding="utf-8")
    tool = _script_tool("python job.py", script_env["root"])
    tool["working_dir"] = None
    result = host.invoke_script(tool)
    assert result["ok"] is True
    assert script_env["run"].calls[0]["cwd"] == str(script_env["pipeline"])


@pytest.mark.parametrize("flag", [True, 1, "true"])
def test_allow_inline_restores_inline_flags(script_env, flag: Any) -> None:
    tool = _script_tool("python -c 'print(1)'", script_env["root"], allow_inline=flag)
    result = host.invoke_script(tool)
    assert result["ok"] is True
    assert script_env["run"].calls[0]["argv"] == ["python", "-c", "print(1)"]


def test_allow_inline_still_enforces_interpreter_allowlist(script_env) -> None:
    tool = _script_tool("perl -e 1", script_env["root"], allow_inline=True)
    result = host.invoke_script(tool)
    assert result["ok"] is False
    assert "not in allowed list" in result["error"]


def test_allowed_extensions_configurable(script_env, monkeypatch) -> None:
    monkeypatch.setattr(
        host,
        "_load_tool_host_config",
        lambda: {"allowed_script_interpreters": ["ruby"], "allowed_script_extensions": [".rb"]},
    )
    (script_env["root"] / "x.rb").write_text("puts 1\n", encoding="utf-8")
    (script_env["root"] / "x.py").write_text("print(1)\n", encoding="utf-8")
    assert host.invoke_script(_script_tool("ruby x.rb", script_env["root"]))["ok"] is True
    denied = host.invoke_script(_script_tool("ruby x.py", script_env["root"]))
    assert denied["ok"] is False
    assert ".rb" in denied["error"]


# ── proxy_to_service ────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"pong": true}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def proxy_env(monkeypatch):
    sent: list[urllib_request.Request] = []

    def _fake_urlopen(req, timeout):
        sent.append(req)
        return _FakeResponse()

    invoked: list[str] = []
    monkeypatch.setattr(host, "_urlopen", _fake_urlopen)
    monkeypatch.setattr(host, "_record_invocation", invoked.append)
    return {"sent": sent, "invoked": invoked}


def _service_tool(upstream: str = "http://svc.local:8080/api", **extra: Any) -> dict[str, Any]:
    return {"tool_id": "svc-1", "upstream_url": upstream, "timeout_seconds": 5, **extra}


def _sent_headers(req: urllib_request.Request) -> dict[str, str]:
    return {k.lower(): v for k, v in req.header_items()}


def test_proxy_denies_put_by_default(proxy_env) -> None:
    result = host.proxy_to_service(_service_tool(), method="PUT", body=b"{}")
    assert result == {"ok": False, "status": 405, "error": "Method 'PUT' not allowed for this tool"}
    assert proxy_env["sent"] == []
    assert proxy_env["invoked"] == []


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "TRACE", "CONNECT"])
def test_proxy_denies_other_methods_by_default(proxy_env, method: str) -> None:
    assert host.proxy_to_service(_service_tool(), method=method)["status"] == 405
    assert proxy_env["sent"] == []


def test_proxy_get_forwards_only_allowlisted_headers(proxy_env, caplog) -> None:
    incoming = {
        "Authorization": "Bearer secret-token-value",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Host": "evil.example",
        "Connection": "close, X-Custom",
        "Transfer-Encoding": "chunked",
        "Cookie": "session=1",
        "X-Custom": "1",
    }
    with caplog.at_level("INFO", logger=host.__name__):
        result = host.proxy_to_service(
            _service_tool(), method="get", subpath="ping", headers=incoming
        )

    assert result == {"ok": True, "status": 200, "data": {"pong": True}}
    (req,) = proxy_env["sent"]
    assert req.get_method() == "GET"
    assert req.full_url == "http://svc.local:8080/api/ping"
    assert _sent_headers(req) == {"accept": "application/json", "content-type": "application/json"}
    assert proxy_env["invoked"] == ["svc-1"]
    assert any(
        "svc-1" in r.getMessage() and "GET http://svc.local:8080/api/ping -> 200" in r.getMessage()
        for r in caplog.records
    )
    assert all("secret-token-value" not in r.getMessage() for r in caplog.records)


def test_proxy_forwards_authorization_only_when_allowlisted(proxy_env) -> None:
    tool = _service_tool(allowed_headers=["Authorization", "accept"])
    host.proxy_to_service(tool, headers={"Authorization": "Bearer t", "Accept": "*/*"})
    assert _sent_headers(proxy_env["sent"][0]) == {"authorization": "Bearer t", "accept": "*/*"}


def test_proxy_always_strips_hop_by_hop_headers(proxy_env) -> None:
    tool = _service_tool(
        allowed_headers=["host", "connection", "transfer-encoding", "te", "accept"]
    )
    host.proxy_to_service(
        tool,
        headers={
            "Host": "x",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "TE": "t",
        },
    )
    assert _sent_headers(proxy_env["sent"][0]) == {}


def test_proxy_drops_header_values_with_crlf(proxy_env) -> None:
    host.proxy_to_service(_service_tool(), headers={"Accept": "a\r\nX-Injected: 1"})
    assert _sent_headers(proxy_env["sent"][0]) == {}


def test_proxy_custom_method_allowlist(proxy_env) -> None:
    tool = _service_tool(allowed_methods=json.dumps(["PUT"]))  # stored JSON form
    assert host.proxy_to_service(tool, method="PUT", body=b"{}")["ok"] is True
    assert host.proxy_to_service(tool, method="GET")["status"] == 405
    req = proxy_env["sent"][0]
    assert req.get_method() == "PUT"
    assert _sent_headers(req)["content-type"] == "application/json"


@pytest.mark.parametrize(
    "upstream",
    [
        "file:///etc/passwd",
        "ftp://files.example/",
        "gopher://x/",
        "http:///no-host",
        "svc.local:8080",
        "http://svc.local/ space",
    ],
)
def test_proxy_rejects_non_http_upstreams(proxy_env, upstream: str) -> None:
    result = host.proxy_to_service(_service_tool(upstream))
    assert result["ok"] is False
    assert "upstream_url" in result["error"]
    assert proxy_env["sent"] == []


@pytest.mark.parametrize("subpath", ["../admin", "a/../../admin", "%2e%2e/admin", "x\r\ny", "a b"])
def test_proxy_rejects_subpath_escaping_upstream(proxy_env, subpath: str) -> None:
    result = host.proxy_to_service(_service_tool(), subpath=subpath)
    assert result == {"ok": False, "error": "Invalid subpath for this tool"}
    assert proxy_env["sent"] == []


def test_proxy_subpath_cannot_change_host(proxy_env) -> None:
    host.proxy_to_service(_service_tool("http://svc.local:8080"), subpath="@evil.example/x")
    req = proxy_env["sent"][0]
    assert req.host == "svc.local:8080"


def test_proxy_reports_upstream_http_errors(monkeypatch) -> None:
    def _raise(req, timeout):
        raise urllib_error.HTTPError(req.full_url, 503, "Service Unavailable", HTTPMessage(), None)

    monkeypatch.setattr(host, "_urlopen", _raise)
    monkeypatch.setattr(host, "_record_invocation", lambda tool_id: None)
    result = host.proxy_to_service(_service_tool())
    assert result == {"ok": False, "status": 503, "error": "Service Unavailable"}


# ── health_check_service ────────────────────────────────────────────


def test_health_check_rejects_file_url(proxy_env) -> None:
    result = host.health_check_service(_service_tool("file:///etc/passwd"))
    assert result["ok"] is False
    assert "http or https" in result["error"]
    assert proxy_env["sent"] == []


def test_health_check_defaults_endpoint_when_unset(proxy_env) -> None:
    result = host.health_check_service(_service_tool("http://svc.local:8080", health_endpoint=None))
    assert result == {"ok": True, "status": 200}
    assert proxy_env["sent"][0].full_url == "http://svc.local:8080/health"


def test_health_check_endpoint_cannot_change_host(proxy_env) -> None:
    host.health_check_service(_service_tool("http://svc.local", health_endpoint="@evil.example/h"))
    assert proxy_env["sent"][0].host == "svc.local"


# ── redirects ────────────────────────────────────────────────────────


def _redirect(req: urllib_request.Request, newurl: str) -> urllib_request.Request | None:
    handler = host._SafeRedirectHandler()
    return handler.redirect_request(req, io.BytesIO(b""), 302, "Found", HTTPMessage(), newurl)


@pytest.mark.parametrize("newurl", ["file:///etc/passwd", "ftp://files.example/x"])
def test_redirect_to_non_http_scheme_refused(newurl: str) -> None:
    req = urllib_request.Request("http://svc.local/a")
    with pytest.raises(urllib_error.HTTPError, match="refused redirect"):
        _redirect(req, newurl)


def test_redirect_drops_credentials_across_hosts() -> None:
    req = urllib_request.Request("http://svc.local/a", headers={"Authorization": "Bearer t"})
    same = _redirect(req, "http://svc.local/b")
    other = _redirect(req, "https://elsewhere.example/b")
    assert same is not None and same.get_header("Authorization") == "Bearer t"
    assert other is not None and other.get_header("Authorization") is None


# ── persistence of the new tool fields ──────────────────────────────


@pytest.fixture
def tool_db(tmp_path: Path):
    path = tmp_path / "pipeline.db"
    with db_mod.using_db(path) as resolved:
        yield resolved
    conn = db_mod._local.connections.pop(resolved, None)
    if conn is not None:
        conn.close()


def test_register_and_update_persist_new_fields(tool_db) -> None:
    script = host.register_tool(
        "inline", "script", "inline", command="python -c 1", allow_inline=True
    )
    assert script["allow_inline"] == 1
    assert script["allowed_methods"] is None
    assert script["allowed_headers"] is None

    svc = host.register_tool(
        "svc",
        "service",
        "svc",
        upstream_url="https://svc.example/api",
        allowed_methods=["get", "put", "GET"],
        allowed_headers=["Accept", "X-Api-Key"],
    )
    assert svc["allow_inline"] == 0
    assert svc["allowed_methods"] == ["GET", "PUT"]
    assert svc["allowed_headers"] == ["accept", "x-api-key"]

    updated = host.update_tool(
        svc["tool_id"], allowed_methods=["post"], allowed_headers=["content-type"]
    )
    assert updated is not None
    assert updated["allowed_methods"] == ["POST"]
    assert updated["allowed_headers"] == ["content-type"]
    toggled = host.update_tool(script["tool_id"], allow_inline=False)
    assert toggled is not None and toggled["allow_inline"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"upstream_url": "file:///etc/passwd"},
        {"upstream_url": "http://"},
        {"allowed_methods": ["GE T"]},
        {"allowed_headers": ["bad header"]},
        {"allowed_methods": "GET"},
    ],
)
def test_register_rejects_invalid_service_fields(tool_db, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        host.register_tool("bad", "service", "bad", **kwargs)
    assert host.get_tool("bad") is None


def test_update_rejects_file_upstream(tool_db) -> None:
    svc = host.register_tool("svc2", "service", "svc2", upstream_url="http://svc.example")
    with pytest.raises(ValueError, match="http or https"):
        host.update_tool(svc["tool_id"], upstream_url="file:///etc/shadow")
    stored = host.get_tool("svc2")
    assert stored is not None and stored["upstream_url"] == "http://svc.example"


_LEGACY_HOSTED_TOOLS = """
CREATE TABLE hosted_tools (
    tool_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    description TEXT, route TEXT NOT NULL UNIQUE, input_schema TEXT DEFAULT '{}',
    output_schema TEXT DEFAULT '{}', command TEXT, working_dir TEXT,
    timeout_seconds INTEGER DEFAULT 30, upstream_url TEXT, health_endpoint TEXT,
    local_path TEXT, node_id TEXT, enabled INTEGER DEFAULT 1, tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_invoked_at TEXT,
    invocation_count INTEGER DEFAULT 0
);
INSERT INTO hosted_tools (tool_id, name, kind, route, upstream_url, created_at, updated_at)
VALUES ('legacy-1', 'legacy', 'service', 'legacy', 'http://svc.local', 'x', 'x');
"""


def test_legacy_table_is_migrated_and_old_rows_use_defaults(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(_LEGACY_HOSTED_TOOLS)
    raw.commit()
    raw.close()

    with db_mod.using_db(path) as resolved:
        try:
            cols = {
                row["name"]
                for row in db_mod.get_connection().execute("PRAGMA table_info(hosted_tools)")
            }
            assert {"allow_inline", "allowed_methods", "allowed_headers"} <= cols
            tool = host.get_tool("legacy")
        finally:
            conn = db_mod._local.connections.pop(resolved, None)
            if conn is not None:
                conn.close()

    assert tool is not None
    assert tool["allow_inline"] == 0
    assert tool["allowed_methods"] is None

    sent: list[urllib_request.Request] = []
    monkeypatch.setattr(host, "_urlopen", lambda req, timeout: sent.append(req) or _FakeResponse())
    monkeypatch.setattr(host, "_record_invocation", lambda tool_id: None)
    assert host.proxy_to_service(tool, method="GET")["ok"] is True
    assert host.proxy_to_service(tool, method="POST", body=b"{}")["ok"] is True
    assert host.proxy_to_service(tool, method="PUT")["status"] == 405
    assert len(sent) == 2


def test_split_command_keeps_backslashes_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.os, "name", "nt")
    argv = host._split_command("python C:\\tools\\job.py 'two words' plain")
    assert argv == ["python", "C:\\tools\\job.py", "two words", "plain"]


def test_split_command_posix_escapes() -> None:
    monkeypatch_name = host.os.name
    if monkeypatch_name == "nt":  # pragma: no cover - exercised on POSIX runners
        pytest.skip("POSIX escaping is not used on Windows")
    assert host._split_command("python a\\ b.py 'c d'") == ["python", "a b.py", "c d"]
