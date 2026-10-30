"""The shared dispatcher: invocation sequence, limits, failures, jobs, ingestion, secrets."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.knowledge.errors import (
    DeniedError,
    IncompatibleError,
    InvalidInputError,
    InvalidScopeError,
    KnowledgeTimeoutError,
    OutputInvalidError,
    UnavailableError,
    UnknownScopeError,
)

# ── reference modules end to end ─────────────────────────────────────


def test_news_fetch_returns_canonical_url_timestamps_rights_and_artifact(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.news)
    url = mod_env.fixture_uri("news-fetcher", "solar-storage.html")
    result = host.invoke("example.news", "fetch", {"url": url}, surface="test")
    out = result["output"]
    assert (
        out["canonical_url"] == "https://news.example.org/2026/10/community-solar-seasonal-storage"
    )
    assert out["published_at"] == "2026-10-01T08:00:00Z"
    assert out["retrieved_at"] == "2026-10-30T12:00:00+00:00"
    assert out["rights"]["license"] == "CC-BY-4.0"
    [artifact] = result["artifacts"]
    assert artifact["media_type"] == "text/html"
    assert artifact["digest"].startswith("sha256:")
    assert artifact["scope"] == {"workspace_id": "default", "project_id": None}
    assert host.catalog.get_artifact(artifact["artifact_id"]) is not None
    # Same bytes of the same record -> same artifact id on a second run.
    again = host.invoke("example.news", "fetch", {"url": url})
    assert again["artifacts"][0]["artifact_id"] == artifact["artifact_id"]
    assert again["run_id"] != result["run_id"]
    runs = host.catalog.list_runs("example.news")
    assert [r["outcome"] for r in runs] == ["ok", "ok"]
    assert all(r["params_digest"].startswith("sha256:") for r in runs)


def test_news_fetch_refuses_unapproved_inputs(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.news)
    outside = mod_env.root / "secret.html"
    outside.write_text("private")
    with pytest.raises(DeniedError, match="fixture root"):
        host.invoke("example.news", "fetch", {"url": outside.as_uri()})
    with pytest.raises(DeniedError, match="live HTTP is disabled"):
        host.invoke("example.news", "fetch", {"url": "https://news.example.org/x"})


def _fetch_interview(host: Any, mod_env: Any, scope: Any = None) -> dict[str, Any]:
    host.configure("example.news", {"fixture_roots": ["fixtures", "../transcription/fixtures"]})
    url = mod_env.fixture_uri("transcription", "interview.txt")
    result = host.invoke("example.news", "fetch", {"url": url}, scope=scope)
    return result["artifacts"][0]  # type: ignore[no-any-return]


def test_transcription_job_returns_segments_engine_and_parent(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.news)
    helpers.enable(host, mod_env.transcription)
    parent = _fetch_interview(host, mod_env)
    accepted = host.invoke(
        "example.transcribe", "transcribe", {"artifact_id": parent["artifact_id"]}
    )
    assert accepted["status"] == "accepted"
    job = helpers.wait_job(host, accepted["job"]["job_id"])
    assert job["state"] == "succeeded", job
    out = job["result"]["output"]
    assert out["language"] == "en"
    assert out["engine"] == {
        "name": "fake-transcriber",
        "version": "1.0.0",
        "model": "deterministic-words-v1",
    }
    assert [s["speaker"] for s in out["segments"]] == ["Host", "Guest", "Host"]
    first, second = out["segments"][:2]
    assert (first["start_ms"], first["end_ms"]) == (0, 6 * 400)
    assert second["start_ms"] == first["end_ms"]
    assert out["parent"]["artifact_id"] == parent["artifact_id"]
    assert out["parent"]["digest"] == parent["digest"]
    [transcript] = job["result"]["artifacts"]
    assert transcript["parent_artifact_ids"] == [parent["artifact_id"]]
    assert job["result"]["provenance"]["parent_artifact_ids"] == [parent["artifact_id"]]


def test_transcription_only_accepts_artifacts_approved_for_the_scope(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.news)
    helpers.enable(host, mod_env.transcription)
    parent = _fetch_interview(host, mod_env, scope={"workspace_id": "research"})
    with pytest.raises(UnknownScopeError):
        host.invoke("example.transcribe", "transcribe", {"artifact_id": parent["artifact_id"]})
    with pytest.raises(UnknownScopeError):
        host.invoke("example.transcribe", "transcribe", {"artifact_id": "art_" + "0" * 32})
    ok = host.invoke(
        "example.transcribe",
        "transcribe",
        {"artifact_id": parent["artifact_id"]},
        scope={"workspace_id": "research", "project_id": "p1"},
        wait=True,
    )
    assert ok["state"] == "succeeded"


# ── ingestion (P6 interface) ─────────────────────────────────────────


def test_ingestion_is_explicit_scoped_idempotent_and_carries_derivation(
    mod_env: Any, host: Any, helpers: Any, ingestion: Any
) -> None:
    helpers.enable(host, mod_env.news)
    helpers.enable(host, mod_env.transcription)
    host.configure("example.news", {"fixture_roots": ["fixtures", "../transcription/fixtures"]})
    url = mod_env.fixture_uri("transcription", "interview.txt")
    scope = {"workspace_id": "research", "project_id": "p1"}
    with pytest.raises(InvalidInputError, match="explicit destination"):
        host.invoke("example.news", "fetch", {"url": url}, ingest=True)
    first = host.invoke("example.news", "fetch", {"url": url}, scope=scope, ingest=True)
    receipt = first["ingestion"]
    assert receipt["source_id"] == "module:example.news"
    assert receipt["destination"] == scope
    assert (receipt["created"], receipt["reused"]) == (1, 0)
    derivation = receipt["derivation"]
    assert derivation["module_id"] == "example.news"
    assert derivation["module_version"] == "1.0.0"
    assert derivation["run_id"] == first["run_id"]
    assert derivation["parameters_digest"].startswith("sha256:")
    # Restart / repeat: the same record and bytes is not a new revision.
    second = host.invoke("example.news", "fetch", {"url": url}, scope=scope, ingest=True)
    assert (second["ingestion"]["created"], second["ingestion"]["reused"]) == (0, 1)
    assert len(ingestion.revisions) == 1
    news_revision = receipt["artifact_revisions"][first["artifacts"][0]["artifact_id"]]

    job = host.invoke(
        "example.transcribe",
        "transcribe",
        {"artifact_id": first["artifacts"][0]["artifact_id"]},
        scope=scope,
        ingest=True,
        wait=True,
    )
    assert job["state"] == "succeeded", job
    lineage = job["result"]["ingestion"]["derivation"]
    assert lineage["parent_revision_ids"] == [news_revision]
    assert lineage["module_id"] == "example.transcribe"


def test_ingestion_rejects_unknown_destinations(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.news)
    url = mod_env.fixture_uri("news-fetcher", "solar-storage.html")
    with pytest.raises(UnknownScopeError):
        host.invoke(
            "example.news", "fetch", {"url": url}, scope={"workspace_id": "nowhere"}, ingest=True
        )
    with pytest.raises(InvalidScopeError):
        host.invoke("example.news", "fetch", {"url": url}, scope={"workspace_id": "Bad Id!"})


def test_ingestion_without_an_evidence_store_is_unavailable(
    mod_env: Any, make_host: Any, helpers: Any
) -> None:
    host = make_host(ingestion=None)
    helpers.enable(host, mod_env.news)
    url = mod_env.fixture_uri("news-fetcher", "solar-storage.html")
    with pytest.raises(UnavailableError, match="ingestion"):
        host.invoke(
            "example.news", "fetch", {"url": url}, scope={"workspace_id": "default"}, ingest=True
        )


# ── schemas and output ───────────────────────────────────────────────


def test_input_schema_and_size_rejections(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.news)
    with pytest.raises(InvalidInputError) as info:
        host.invoke("example.news", "fetch", {"link": "x"})
    assert info.value.details["errors"]
    with pytest.raises(InvalidInputError, match="limit"):
        host.invoke("example.news", "fetch", {"url": "file:///" + "a" * 5000})
    with pytest.raises(InvalidInputError, match="no tool"):
        host.invoke("example.news", "nope", {})
    with pytest.raises(InvalidInputError, match="not registered"):
        host.invoke("example.nothing", "fetch", {})
    assert [r["outcome"] for r in host.catalog.list_runs("example.news")] == []


@pytest.mark.parametrize(
    ("mode", "error", "needle"),
    [
        ("bad_output", OutputInvalidError, "schema"),
        ("garbage", OutputInvalidError, "not JSON"),
        ("big", OutputInvalidError, "exceeds 4096 bytes"),
        ("module_error", InvalidInputError, "did not like"),
    ],
)
def test_output_rejections(
    mod_env: Any, host: Any, helpers: Any, mode: str, error: type[Exception], needle: str
) -> None:
    helpers.enable(host, mod_env.probe)
    with pytest.raises(error, match=needle):
        host.invoke("test.probe", "run", {"mode": mode})
    assert host.catalog.list_runs("test.probe")[0]["outcome"] == error.code  # type: ignore[attr-defined]


def test_artifact_budgets(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    ok = host.invoke("test.probe", "run", {"mode": "artifacts", "count": 2, "size": 10})
    assert len(ok["artifacts"]) == 2
    with pytest.raises(OutputInvalidError, match="limit is 2"):
        host.invoke("test.probe", "run", {"mode": "artifacts", "count": 3, "size": 1})
    with pytest.raises(OutputInvalidError, match="byte budget"):
        host.invoke("test.probe", "run", {"mode": "artifacts", "count": 2, "size": 1500})


# ── limits, timeout, cancellation ────────────────────────────────────


def test_timeout_stops_the_call(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    started = time.monotonic()
    with pytest.raises(KnowledgeTimeoutError):
        host.invoke("test.probe", "run", {"mode": "sleep", "seconds": 30}, timeout_ms=600)
    assert time.monotonic() - started < 8


def test_caller_can_lower_but_not_raise_the_deadline(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)

    def deadline(timeout_ms: int | None) -> float:
        prepared = host._prepare(
            "test.probe",
            "run",
            {"mode": "echo"},
            scope=None,
            timeout_ms=timeout_ms,
            effects=None,
            expected_digest=None,
        )
        return float(prepared.timeout_s)

    assert deadline(None) == 10.0
    assert deadline(10**9) == 10.0
    assert deadline(250) == 0.25


def test_cancellation_kills_the_child_process_group(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    pid_file = mod_env.root / "grandchild.pid"
    accepted = host.invoke("test.probe", "slow_job", {"mode": "spawn", "pid_file": str(pid_file)})
    job_id = accepted["job"]["job_id"]
    helpers.wait_for(lambda: pid_file.exists() and pid_file.read_text().strip() != "")
    grandchild = int(pid_file.read_text())
    assert helpers.process_alive(grandchild)
    cancelled = host.job_cancel(job_id)
    assert cancelled["state"] == "cancelled", cancelled
    assert cancelled["error"]["code"] == "cancelled"
    helpers.wait_for(lambda: not helpers.process_alive(grandchild), timeout=10)


def test_cancellation_requested_from_another_process_reaches_the_owner(
    mod_env: Any, host: Any, make_host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    accepted = host.invoke("test.probe", "slow_job", {"mode": "sleep", "seconds": 60})
    other = make_host()  # e.g. the CLI: same catalog, does not own the job
    other.job_cancel(accepted["job"]["job_id"], wait_s=0)
    job = helpers.wait_job(host, accepted["job"]["job_id"])
    assert job["state"] == "cancelled"


def test_concurrency_limit(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    accepted = host.invoke("test.probe", "slow_job", {"mode": "sleep", "seconds": 60})
    with pytest.raises(UnavailableError, match="concurrency limit") as info:
        host.invoke("test.probe", "run", {"mode": "echo"})
    assert info.value.retryable
    host.job_cancel(accepted["job"]["job_id"])
    assert host.invoke("test.probe", "run", {"mode": "echo"})["status"] == "ok"


def test_concurrent_calls_race_for_one_slot(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    outcomes: list[str] = []

    def call() -> None:
        try:
            host.invoke("test.probe", "run", {"mode": "sleep", "seconds": 1.0})
            outcomes.append("ok")
        except UnavailableError:
            outcomes.append("busy")

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(outcomes) == ["busy", "busy", "ok"]


def test_retries_only_for_idempotent_tools(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    marker = mod_env.root / "idem.count"
    result = host.invoke("test.probe", "run_idem", {"mode": "fail_once", "marker": str(marker)})
    assert result["attempts"] == 2
    assert marker.read_text() == "2"
    marker2 = mod_env.root / "plain.count"
    with pytest.raises(UnavailableError, match="exited with status 3"):
        host.invoke("test.probe", "run", {"mode": "fail_once", "marker": str(marker2)})
    assert marker2.read_text() == "1"


def test_disable_rejects_new_work_and_cancels_active_jobs(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    accepted = host.invoke("test.probe", "slow_job", {"mode": "sleep", "seconds": 60})
    report = host.disable("test.probe", drain_s=5)
    assert report["cancelled"] == 1 and report["draining"] == 0
    assert report["state"] == "disabled"
    job = host.job_status(accepted["job"]["job_id"])
    assert job["state"] == "cancelled"
    with pytest.raises(UnavailableError, match="disabled"):
        host.invoke("test.probe", "run", {"mode": "echo"})


def test_disable_from_another_process_stops_running_calls(
    mod_env: Any, host: Any, make_host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    accepted = host.invoke("test.probe", "slow_job", {"mode": "sleep", "seconds": 60})
    make_host().catalog.set_enabled("test.probe", False)
    job = helpers.wait_job(host, accepted["job"]["job_id"])
    assert job["state"] == "cancelled"


# ── jobs ─────────────────────────────────────────────────────────────


def test_jobs_are_scoped_and_expire(mod_env: Any, host: Any, helpers: Any, clock: Any) -> None:
    helpers.enable(host, mod_env.probe)
    accepted = host.invoke(
        "test.probe", "slow_job", {"mode": "echo"}, scope={"workspace_id": "research"}
    )
    job_id = accepted["job"]["job_id"]
    assert accepted["job"]["workspace_id"] == "research"
    with pytest.raises(UnknownScopeError):
        host.job_status(job_id)  # default workspace
    done = helpers.wait_job(host, job_id, "research")
    assert done["state"] == "succeeded"
    assert [j["job_id"] for j in host.job_list("research")] == [job_id]
    assert host.job_list("default") == []
    clock.advance(seconds=3600)
    with pytest.raises(UnknownScopeError):
        host.job_status(job_id, "research")


def test_cancel_of_a_finished_job_is_a_no_op(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    job = host.invoke("test.probe", "slow_job", {"mode": "echo"}, wait=True)
    assert job["state"] == "succeeded"
    assert host.job_cancel(job["job_id"])["state"] == "succeeded"


# ── policy and enablement ────────────────────────────────────────────


def test_write_effect_needs_host_policy_and_read_only_is_enforced(
    mod_env: Any, make_host: Any, helpers: Any
) -> None:
    default = make_host()
    helpers.enable(default, mod_env.probe)
    assert "write" not in default.status("test.probe")["grants"]
    with pytest.raises(DeniedError, match="'write'"):
        default.invoke("test.probe", "write_thing", {})

    permissive = make_host(host_capabilities={"read", "write", "process"})
    permissive.enable("test.probe")
    assert permissive.invoke("test.probe", "write_thing", {})["status"] == "ok"
    with pytest.raises(DeniedError) as info:
        permissive.invoke("test.probe", "write_thing", {}, effects=["read"])
    assert info.value.details["policy"]["allowed_effects"] == ["read"]
    assert (
        permissive.invoke("test.probe", "run", {"mode": "echo"}, effects=["read"])["status"] == "ok"
    )


def test_grants_beyond_host_policy_are_denied(mod_env: Any, host: Any) -> None:
    host.register(mod_env.probe)
    with pytest.raises(DeniedError, match="host policy"):
        host.enable("test.probe", ["read", "write", "process"])
    with pytest.raises(DeniedError, match="transport needs"):
        host.enable("test.probe", ["read"])
    assert host.status("test.probe")["state"] == "failed"


def test_incompatible_engine_fails_enable(mod_env: Any, make_host: Any) -> None:
    host = make_host(engine_version="9.0.0")
    host.register(mod_env.news)
    with pytest.raises(IncompatibleError):
        host.enable("example.news")
    status = host.status("example.news")
    assert status["state"] == "failed" and status["enabled"] is False


def test_unallowlisted_interpreter_is_denied(mod_env: Any, host: Any) -> None:
    data = json.loads((mod_env.probe / "module.json").read_text())
    data["transport"]["command"] = ["ruby", "probe.rb"]
    data.pop("health")
    (mod_env.probe / "probe.rb").write_text("")
    (mod_env.probe / "module.json").write_text(json.dumps(data))
    host.register(mod_env.probe)
    with pytest.raises(DeniedError, match="not in allowed list"):
        host.enable("test.probe")


def test_required_secret_reference_must_be_set(mod_env: Any, make_host: Any) -> None:
    data = json.loads((mod_env.probe / "module.json").read_text())
    data["configuration"]["secrets"][0]["required"] = True
    (mod_env.probe / "module.json").write_text(json.dumps(data))
    host = make_host(environ={})
    host.register(mod_env.probe)
    with pytest.raises(UnavailableError) as info:
        host.enable("test.probe")
    assert info.value.details == {"env": ["PROBE_TOKEN"]}
    host.configure("test.probe", secrets={"api_token": "OTHER_TOKEN_VAR"})
    with pytest.raises(UnavailableError) as info:
        host.enable("test.probe")
    assert info.value.details == {"env": ["OTHER_TOKEN_VAR"]}


def test_configuration_validation(mod_env: Any, host: Any) -> None:
    host.register(mod_env.news)
    with pytest.raises(InvalidInputError, match="unknown setting"):
        host.configure("example.news", {"colour": "blue"})
    with pytest.raises(InvalidInputError, match="must be a boolean"):
        host.configure("example.news", {"allow_live_http": "perhaps"})
    with pytest.raises(InvalidInputError, match="credentials"):
        host.configure("example.news", {"default_license": "https://u:p@example.org/x"})
    host.register(mod_env.probe)
    with pytest.raises(InvalidInputError, match="environment variable"):
        host.configure("test.probe", secrets={"api_token": "tok-literal-value"})
    configured = host.configure("example.news", {"allowed_hosts": "a.example.org, b.example.org"})
    assert configured["settings"] == {"allowed_hosts": ["a.example.org", "b.example.org"]}
    assert configured["state"] == "disabled"


def test_failed_module_start_is_isolated(
    mod_env: Any, host: Any, make_host: Any, helpers: Any
) -> None:
    import shutil

    helpers.enable(host, mod_env.news)
    helpers.enable(host, mod_env.probe)
    shutil.rmtree(mod_env.probe)
    restarted = make_host()
    states = restarted.start()
    assert states == {"example.news": "ready", "test.probe": "failed"}
    assert [name for name, *_ in restarted.advertised_tools()] == ["example.news.fetch"]
    with pytest.raises(UnavailableError, match="failed to start"):
        restarted.invoke("test.probe", "run", {"mode": "echo"})


def test_health_probe(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.probe)
    assert host.status("test.probe", probe=True)["probe"] == {"ok": True}
    (mod_env.probe / "probe.py").rename(mod_env.probe / "gone.py")
    report = host.status("test.probe", probe=True)
    assert report["probe"]["ok"] is False
    assert report["state"] == "degraded"


# ── credentials ──────────────────────────────────────────────────────


def test_credentials_never_appear_in_results_or_logs(
    mod_env: Any, host: Any, helpers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    helpers.enable(host, mod_env.probe)
    result = host.invoke("test.probe", "run", {"mode": "secret"})
    assert result["output"]["leak"] == "value ***"
    with pytest.raises(UnavailableError) as info:
        host.invoke("test.probe", "run", {"mode": "secret_error"})
    assert helpers.SECRET not in json.dumps(info.value.to_dict())
    assert "***" in info.value.message
    with pytest.raises(OutputInvalidError, match="credential"):
        host.invoke("test.probe", "run", {"mode": "secret_artifact"})
    status = host.status("test.probe", probe=True)
    assert helpers.SECRET not in json.dumps(status)
    assert helpers.SECRET not in caplog.text
    assert helpers.SECRET.encode() not in Path(host.catalog.path).read_bytes()


def test_module_processes_do_not_inherit_the_host_environment(
    mod_env: Any, host: Any, helpers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_CLOUD_API_KEY", "host-only-credential")
    helpers.enable(host, mod_env.probe)
    probe_script = mod_env.probe / "probe.py"
    probe_script.write_text(
        "import json, os, sys\njson.load(sys.stdin)\n"
        "json.dump({'output': {'mode': 'env', 'keys': sorted(os.environ)}}, sys.stdout)\n"
    )
    keys = host.invoke("test.probe", "run", {"mode": "env"})["output"]["keys"]
    assert "KE_CLOUD_API_KEY" not in keys
    assert "PROBE_TOKEN" in keys and "KNOWLEDGE_ENGINE_RUN_ID" in keys
