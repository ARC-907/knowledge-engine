"""Consistency checks for the development tooling that lives outside the package.

Covers deploy/observability (compose, Loki, Alloy, Grafana provisioning),
noxfile.py, .serena/project.yml and scripts/index_repo.py. These files are
not imported by the engine, so nothing else would notice them drifting apart
(a renamed datasource uid, a log directory the shipper no longer tails, a
Python version CI tests but nox does not).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest
import yaml

from knowledge_engine.core.registry import validation_issues

REPO = Path(__file__).resolve().parents[2]
OBS = REPO / "deploy" / "observability"

pytestmark = pytest.mark.skipif(
    not (REPO / "noxfile.py").is_file(), reason="needs a repository checkout"
)


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_script(name: str) -> ModuleType:
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_devtool_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── observability stack ──────────────────────────────────────────


def test_compose_pins_images_and_binds_loopback() -> None:
    compose = _yaml(OBS / "compose.yaml")
    services = compose["services"]
    assert {"loki", "alloy", "grafana", "engine"} <= set(services)
    for name, service in services.items():
        image = service["image"]
        if name != "engine":
            assert re.fullmatch(r"grafana/[a-z]+:v?\d+\.\d+\.\d+", image), image
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"
    assert services["engine"]["profiles"] == ["engine"]
    assert services["engine"]["environment"]["KE_LOG_FORMAT"] == "json"


def test_alloy_tails_the_directory_the_engine_writes() -> None:
    compose = _yaml(OBS / "compose.yaml")
    alloy_mounts = {
        m["target"]: m for m in compose["services"]["alloy"]["volumes"] if isinstance(m, dict)
    }
    engine_env = compose["services"]["engine"]["environment"]
    log_dir = str(PurePosixPath(engine_env["KE_LOG_FILE"]).parent)
    assert log_dir in alloy_mounts
    assert alloy_mounts[log_dir]["read_only"] is True
    assert engine_env["KE_LOG_FILE"].endswith(".jsonl")

    alloy = (OBS / "config.alloy").read_text(encoding="utf-8")
    assert f'"__path__" = "{log_dir}/*.jsonl"' in alloy
    # Every key the Alloy pipeline extracts is one the JSON formatter writes.
    import logging

    from knowledge_engine.core.logging_config import JsonFormatter

    record = logging.LogRecord("knowledge_engine.x", logging.INFO, __file__, 1, "m", None, None)
    written = set(json.loads(JsonFormatter().format(record)))
    block = re.search(r"stage\.json \{\s*expressions = \{(.*?)\}", alloy, re.S)
    assert block is not None
    extracted = set(re.findall(r'=\s*"(\w+)"', block.group(1)))
    assert extracted and extracted <= written, extracted - written
    assert 'url = sys.env("LOKI_URL")' in alloy
    assert compose["services"]["alloy"]["environment"]["LOKI_URL"].startswith("http://loki:3100/")


def test_loki_config_is_single_node_with_retention() -> None:
    loki = _yaml(OBS / "loki.yaml")
    assert loki["auth_enabled"] is False
    assert loki["common"]["replication_factor"] == 1
    assert loki["schema_config"]["configs"][0]["schema"] == "v13"
    assert loki["compactor"]["retention_enabled"] is True
    assert loki["limits_config"]["retention_period"]
    assert loki["analytics"]["reporting_enabled"] is False


def test_grafana_dashboard_uses_the_provisioned_datasource() -> None:
    datasources = _yaml(OBS / "grafana/provisioning/datasources/loki.yaml")["datasources"]
    uids = {d["uid"] for d in datasources}
    assert len(datasources) == 1 and datasources[0]["url"] == "http://loki:3100"

    providers = _yaml(OBS / "grafana/provisioning/dashboards/knowledge-engine.yaml")["providers"]
    compose = _yaml(OBS / "compose.yaml")
    mounts = compose["services"]["grafana"]["volumes"]
    assert f"./grafana/dashboards:{providers[0]['options']['path']}:ro" in mounts

    dashboard = json.loads(
        (OBS / "grafana/dashboards/knowledge-engine-logs.json").read_text(encoding="utf-8")
    )
    assert dashboard["uid"] == "ke-logs"
    home = compose["services"]["grafana"]["environment"][
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"
    ]
    assert home.endswith("/knowledge-engine-logs.json")
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] in uids
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in uids
            assert '{job="knowledge-engine"' in target["expr"]
            used = set(re.findall(r"\$(\w+)", target["expr"])) - {"__interval", "__range"}
            assert used <= variables, used - variables


# ── nox ──────────────────────────────────────────────────────────


def _nox_constants() -> dict[str, Any]:
    tree = ast.parse((REPO / "noxfile.py").read_text(encoding="utf-8"))
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


def test_nox_python_matrix_matches_ci() -> None:
    ci = _yaml(REPO / ".github/workflows/ci.yml")
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    ci_pythons = {str(v) for v in matrix.get("python", matrix.get("python-version", []))}
    for extra in matrix.get("include", []):
        ci_pythons.add(str(extra.get("python", extra.get("python-version", ""))))
    ci_pythons.discard("")
    assert set(_nox_constants()["PYTHONS"]) == ci_pythons


def test_nox_installs_only_declared_dependency_groups() -> None:
    import tomllib

    groups = set(
        tomllib.loads((REPO / "engine/pyproject.toml").read_text("utf-8"))["dependency-groups"]
    )
    source = (REPO / "noxfile.py").read_text(encoding="utf-8")
    used = set(re.findall(r'sync\(session(?:, "(\w+)")(?:, "(\w+)")?', source))
    flat = {g for pair in used for g in pair if g}
    assert flat and flat <= groups, flat - groups
    assert _nox_constants()["SERENA_SPEC"].startswith("serena-agent==")


# ── Serena ───────────────────────────────────────────────────────


def test_serena_project_forces_tool_interface() -> None:
    project = _yaml(REPO / ".serena/project.yml")
    assert project["project_name"] == "knowledge-engine"
    assert project["language_servers"] == ["python"]
    # Serena's default agent context otherwise exposes only its REPL tool.
    assert project["agent_interface"] == "tools"
    assert project["ignore_all_files_in_gitignore"] is True
    for rel in project["ignored_paths"]:
        assert not Path(rel).is_absolute()

    check = _load_script("serena_check")
    assert {"--agent-interface", "tools"} <= set(check.SERENA_ARGS)
    assert (REPO / check.PROBE_FILE).is_file()
    assert f"class {check.PROBE_SYMBOL}" in (REPO / check.PROBE_FILE).read_text("utf-8")
    assert check.serena_env()["SERENA_USAGE_REPORTING"] == "false"


# ── repository self-index ────────────────────────────────────────


def test_self_index_registry_is_valid_and_covers_the_docs() -> None:
    index_repo = _load_script("index_repo")
    document = index_repo.build_registry(REPO)
    assert validation_issues(document) == []
    paths = {entry["path"] for entry in document["libraries"]}
    assert {"01-user-docs", "02-dev-docs", "02-dev-docs/adr", "README.md"} <= paths
    for rel in paths:
        assert (REPO / rel).exists(), rel


def test_self_index_skips_missing_entries(tmp_path: Path) -> None:
    index_repo = _load_script("index_repo")
    (tmp_path / "01-user-docs").mkdir()
    document = index_repo.build_registry(tmp_path)
    assert [e["path"] for e in document["libraries"]] == ["01-user-docs"]
    assert validation_issues(document) == []


def test_self_index_environment_is_isolated(tmp_path: Path) -> None:
    index_repo = _load_script("index_repo")
    env = index_repo.engine_env(
        REPO, tmp_path, provider="fake", model="bge-m3", ollama_url="http://127.0.0.1:11434"
    )
    for key in ("KE_HOME", "KE_REGISTRY_PATH", "KE_DATA_DIR"):
        assert env[key].startswith(str(tmp_path))
    assert env["KE_CORPUS_ROOT"] == str(REPO)
    assert env["KE_EMBED_PROVIDER"] == "fake"


def test_self_index_reports_unreachable_ollama() -> None:
    index_repo = _load_script("index_repo")
    ok, reason = index_repo.ollama_has_model("http://127.0.0.1:9", "bge-m3", timeout=0.5)
    assert ok is False
    assert "not reachable" in reason


def test_self_index_end_to_end_with_fake_embeddings(tmp_path: Path) -> None:
    index_repo = _load_script("index_repo")
    out = tmp_path / "home"
    assert index_repo.main(["--out", str(out), "--provider", "fake", "--query", "ownership"]) == 0
    registry = json.loads((out / "registry.json").read_text(encoding="utf-8"))
    assert registry["libraries"]
    assert (out / "data" / "embeddings.db").is_file()
