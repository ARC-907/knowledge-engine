"""Unit tests for the opt-in pipeline substrate: queue, worker registry,
task classifier (no network) and the foundation config loaders."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from knowledge_engine.core.ollama import OllamaError
from knowledge_engine.runtime.foundation import config, db
from knowledge_engine.runtime.pipeline import queue, task_classifier, worker_registry


@pytest.fixture
def pipeline_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pipeline.db"
    monkeypatch.setenv("KE_PIPELINE_DB", str(path))
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path / "config"))  # no YAML: defaults
    return path


def _events(event_type: str) -> list[Any]:
    return (
        db.get_connection()
        .execute("SELECT * FROM events WHERE event_type = ? ORDER BY event_id", (event_type,))
        .fetchall()
    )


# ── queue ────────────────────────────────────────────────────────────


def test_create_task_defaults_and_validation(pipeline_db: Path) -> None:
    task = queue.create_task("audit", "lib", chapter_key="ch1", depends_on=["x"], meta="{}")
    assert task["status"] == "queued"
    assert task["depends_on"] == ["x"]  # JSON columns decoded
    assert task["source_policy"]["allow_cloud_synthesis"] is False
    assert task["audit_trail"][0]["event"] == "created"
    assert len(_events("task_created")) == 1
    assert queue.create_task("pipeline:custom", "lib")["task_type"] == "pipeline:custom"
    with pytest.raises(ValueError, match="Invalid task_type"):
        queue.create_task("nonsense", "lib")
    assert queue.read_task("missing") is None


def test_transitions_update_timestamps_audit_and_guard_states(pipeline_db: Path) -> None:
    task = queue.create_task("audit", "lib")
    tid = task["task_id"]
    claimed = queue.transition_task(tid, "claimed", worker="w1")
    assert claimed["assigned_worker"] == "w1" and claimed["lease_expires_at"]
    running = queue.transition_task(tid, "running", worker="w1")
    assert running["started_at"]
    failed = queue.transition_task(
        tid,
        "failed",
        detail="boom",
        extra_fields={"notes": "n", "output_artifacts": ["a"], "status": "hijack"},
    )
    assert failed["status"] == "failed" and failed["failure_reason"] == "boom"
    assert failed["notes"] == "n" and failed["output_artifacts"] == ["a"]
    assert failed["lease_expires_at"] is None and failed["completed_at"]
    assert [e["event"] for e in failed["audit_trail"]][-1] == "running -> failed"
    # Same state: no-op; unknown state / task: errors.
    assert queue.transition_task(tid, "failed")["status"] == "failed"
    with pytest.raises(ValueError, match="Invalid state"):
        queue.transition_task(tid, "teleported")
    with pytest.raises(FileNotFoundError):
        queue.transition_task("missing", "queued")


def test_claim_respects_layers_capabilities_dependencies_and_priority(pipeline_db: Path) -> None:
    dep = queue.create_task("audit", "lib", priority=5)
    queue.create_task("audit", "lib", priority=1, depends_on=[dep["task_id"]])
    queue.create_task("audit", "lib", priority=2, assigned_capability=["gpu"])
    queue.create_task("audit", "lib", priority=3, allowed_layer="cloud")
    plain = queue.create_task("audit", "lib", priority=4)

    first = queue.claim_task("w1", capabilities=[])
    assert first is not None and first["task_id"] == plain["task_id"]
    gpu = queue.claim_task("w2", capabilities=["gpu"])
    assert gpu is not None and gpu["assigned_capability"] == ["gpu"]
    # The dependent task waits until its dependency is complete.
    nxt = queue.claim_task("w3", capabilities=[])
    assert nxt is not None and nxt["task_id"] == dep["task_id"]
    assert queue.claim_task("w4", capabilities=[]) is None
    queue.transition_task(dep["task_id"], "complete")
    blocked_on = queue.claim_task("w4", capabilities=[])
    assert blocked_on is not None and blocked_on["depends_on"] == [dep["task_id"]]
    cloud = queue.claim_task("w5", capabilities=[], allowed_layers=["cloud"])
    assert cloud is not None and cloud["allowed_layer"] == "cloud"


def test_expired_leases_are_released(pipeline_db: Path) -> None:
    task = queue.create_task("audit", "lib")
    queue.transition_task(task["task_id"], "claimed", worker="w1")
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    conn = db.get_connection()
    conn.execute("UPDATE tasks SET lease_expires_at = ? WHERE task_id = ?", (past, task["task_id"]))
    conn.commit()
    assert queue.release_expired_claims() == [task["task_id"]]
    released = queue.read_task(task["task_id"])
    assert released is not None
    assert released["status"] == "queued" and released["assigned_worker"] is None
    summary = queue.get_queue_summary()
    assert summary["queued"] == 1 and set(summary) == set(queue.VALID_STATES)


def test_claim_timeout_comes_from_the_policy_file(pipeline_db: Path, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline-policy.yaml").write_text(
        "queue_policy:\n  claim_timeout_seconds: 3600\n", encoding="utf-8"
    )
    task = queue.create_task("audit", "lib")
    claimed = queue.transition_task(task["task_id"], "claimed", worker="w")
    lease = datetime.fromisoformat(claimed["lease_expires_at"])
    assert timedelta(minutes=55) < lease - datetime.now(UTC) <= timedelta(hours=1)


# ── worker registry ──────────────────────────────────────────────────


def test_register_heartbeat_and_safeguards(pipeline_db: Path) -> None:
    w = worker_registry.register_worker("n1", capabilities=["gpu"], role="gpu-worker")
    assert w["status"] == "online" and w["capabilities"] == ["gpu"]
    assert w["hostname"] == "n1" and w["model_class"] == "unknown"
    again = worker_registry.register_worker("n1", hostname="h2")
    assert again["hostname"] == "h2"  # upsert
    assert [x["node_id"] for x in worker_registry.get_workers_with_capability("gpu")] == []

    hb = worker_registry.send_heartbeat("n1", current_tasks=["t1"], resources={"cpu": 0.5})
    assert hb is not None and hb["current_tasks"] == ["t1"]
    assert json.loads(hb["resources_json"]) == {"cpu": 0.5}
    assert worker_registry.check_heartbeat("n1") == hb["last_heartbeat"]
    assert worker_registry.check_heartbeat("ghost") is None

    locked = worker_registry.set_safeguard("n1", True, "maintenance")
    assert locked is not None and locked["safeguard_locked"] == 1
    status = worker_registry.get_safeguard_status("n1")
    assert status == {
        "node_id": "n1",
        "safeguard_locked": True,
        "safeguard_reason": "maintenance",
        "status": "online",
    }
    assert worker_registry.get_safeguard_status("ghost") is None
    assert worker_registry.list_safeguard_statuses()[0]["safeguard_locked"] is True
    assert len(_events("safeguard_lock")) == 1


def test_expired_workers_go_offline_and_unregister(pipeline_db: Path) -> None:
    worker_registry.register_worker("old")
    worker_registry.register_worker("fresh")
    conn = db.get_connection()
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE workers SET last_heartbeat = ? WHERE node_id = 'old'", (stale,))
    conn.commit()
    assert worker_registry.get_expired_workers(timeout_seconds=60) == ["old"]
    worker_registry.mark_workers_offline(["old"])
    assert [w["node_id"] for w in worker_registry.list_workers(online_only=True)] == ["fresh"]
    assert worker_registry.unregister_worker("old") is True
    assert worker_registry.unregister_worker("old") is False


def test_register_fills_from_nodes_yaml(pipeline_db: Path, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "nodes.yaml").write_text(
        "nodes:\n  n9:\n    hostname: box9\n    model_class: big\n    capabilities: [ocr]\n",
        encoding="utf-8",
    )
    w = worker_registry.register_worker("n9")
    assert (w["hostname"], w["model_class"], w["capabilities"]) == ("box9", "big", ["ocr"])


# ── task classifier (no network) ─────────────────────────────────────


class _FakeClient:
    calls: ClassVar[list[tuple[str, float]]] = []
    response: ClassVar[Any] = {}
    error: Exception | None = None

    def __init__(self, base_url: str, timeout: float) -> None:
        type(self).calls.append((base_url, timeout))

    def generate(self, model: str, prompt: str, **_kwargs: Any) -> Any:
        if type(self).error is not None:
            raise type(self).error
        return type(self).response


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.calls = []
    _FakeClient.response = {}
    _FakeClient.error = None
    monkeypatch.setattr(task_classifier, "OllamaClient", _FakeClient)
    return _FakeClient


def test_classify_parses_json_and_normalizes(fake_ollama: type[_FakeClient]) -> None:
    fake_ollama.response = {
        "response": 'Sure: {"domain": "code", "complexity": "complex", '
        '"confidence": 0.9, "routing_note": "' + "x" * 300 + '"} done'
    }
    out = task_classifier.classify_task("refactor it", endpoint="http://gpu:11434")
    assert (out["domain"], out["complexity"], out["confidence"]) == ("code", "complex", 0.9)
    assert len(out["routing_note"]) == 200
    assert fake_ollama.calls == [("http://gpu:11434", 15.0)]


@pytest.mark.parametrize(
    "text",
    [
        '{"domain": "astrology", "complexity": "epic", "confidence": 7}',
        "[1, 2, 3]",
        "not json at all",
    ],
)
def test_classify_degrades_safely(fake_ollama: type[_FakeClient], text: str) -> None:
    fake_ollama.response = {"response": text}
    out = task_classifier.classify_task("x")
    assert out["domain"] == "general" and out["complexity"] == "moderate"
    assert 0.0 <= out["confidence"] <= 1.0


def test_classify_returns_default_when_ollama_fails(fake_ollama: type[_FakeClient]) -> None:
    fake_ollama.error = OllamaError("down")
    assert task_classifier.classify_task("x")["confidence"] == 0.0
    fake_ollama.error = None
    fake_ollama.response = {"response": 42}
    assert task_classifier.classify_task("x")["confidence"] == 0.0


def test_classify_rejects_a_non_string_endpoint() -> None:
    with pytest.raises(RuntimeError):
        task_classifier.classify_task("x", endpoint=123)  # type: ignore[arg-type]


# ── foundation config loaders ────────────────────────────────────────


def test_loaders_fall_back_to_defaults_without_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path / "none"))
    assert config.load_nodes() == {"nodes": {}}
    assert config.load_apis() == {}
    assert config.load_tools() == {"tools": []}
    assert config.load_policy() == {"queue_policy": {"claim_timeout_seconds": 600}}
    assert config.load_message_board() == {}
    assert config.load_routing() == {}
    assert config.load_tool_host() == {"tool_host": {}}
    assert config.load_agent_keys() == {"agent_keys": {}}
    failover = config.load_proxy_failover()
    assert failover["candidates"][0]["host"] == "127.0.0.1"
    assert set(config.load_all_config()) == {"nodes", "apis", "tools", "policy"}


def test_loaders_read_yaml_and_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "apis.yaml").write_text(
        "research_apis:\n  priority_order: [a, b]\nsynthesis_apis:\n  priority_order: [c]\n",
        encoding="utf-8",
    )
    (tmp_path / "pipeline-policy.yaml").write_text(
        "cloud_gate:\n  min: 1\nqueue_policy:\n  claim_timeout_seconds: 5\n", encoding="utf-8"
    )
    (tmp_path / "nodes.yaml").write_text(
        "nodes: {a: {enabled: true}, b: {enabled: false}}\n"
        "proxy_failover:\n  candidates: [{host: h}]\n",
        encoding="utf-8",
    )
    assert config.get_research_api_priority() == ["a", "b"]
    assert config.get_synthesis_api_priority() == ["c"]
    assert config.get_cloud_gate_thresholds() == {"min": 1}
    assert config.get_queue_policy() == {"claim_timeout_seconds": 5}
    assert config.get_enabled_nodes() == {"a": {"enabled": True}}
    assert config.load_proxy_failover() == {"candidates": [{"host": "h"}]}
    monkeypatch.setenv("KE_TEST_API_KEY", "secret")
    assert config.get_api_key("KE_TEST_API_KEY") == "secret"


def test_write_yaml_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path / "new"))
    path = config._write_yaml("tools.yaml", {"tools": [{"name": "t"}]})
    assert path.parent == (tmp_path / "new").resolve()
    assert config.load_tools() == {"tools": [{"name": "t"}]}


def test_non_mapping_yaml_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "nodes.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        config.load_nodes()
