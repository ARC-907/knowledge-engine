"""Manifest v1: validation as data, the committed JSON Schema, discovery and registration rules.

Regenerate the committed schema after an intended change with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/modules/test_manifest.py
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.knowledge.errors import (
    DeniedError,
    IncompatibleError,
    InvalidInputError,
    KnowledgeError,
)
from knowledge_engine.modules.manifest import (
    find_embedded_secrets,
    load_manifest,
    manifest_json_schema,
    parse_manifest,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "knowledge_engine"
    / "modules"
    / "manifest.schema.json"
)


def _base(helpers: Any) -> dict[str, Any]:
    return json.loads((helpers.EXAMPLES / "news-fetcher" / "module.json").read_text())


def test_committed_schema_matches_the_model() -> None:
    rendered = json.dumps(manifest_json_schema(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SCHEMA_FILE.write_text(rendered, encoding="utf-8")
    assert SCHEMA_FILE.read_text(encoding="utf-8") == rendered, (
        "manifest schema changed; regenerate with UPDATE_SNAPSHOTS=1 (see module docstring)"
    )


def test_reference_manifests_are_valid(helpers: Any) -> None:
    news = load_manifest(helpers.EXAMPLES / "news-fetcher")
    transcription = load_manifest(helpers.EXAMPLES / "transcription")
    assert news.module_id == "example.news"
    assert news.tool("fetch") is not None and news.tool("fetch").idempotent  # type: ignore[union-attr]
    tool = transcription.tool("transcribe")
    assert tool is not None and tool.long_running and tool.artifact_inputs == ("artifact_id",)


def test_reference_manifests_validate_against_the_committed_schema(helpers: Any) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_FILE.read_text())
    for name in ("news-fetcher", "transcription"):
        data = json.loads((helpers.EXAMPLES / name / "module.json").read_text())
        assert not list(Draft202012Validator(schema).iter_errors(data)), name


def test_unsupported_manifest_version_is_incompatible(helpers: Any) -> None:
    data = _base(helpers)
    data["manifest_version"] = 2
    with pytest.raises(IncompatibleError) as info:
        parse_manifest(data)
    assert info.value.details["supported"] == [1]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: d["capabilities"].append("teleport"), "capabilities"),
        (lambda d: d["tools"][0].update(effect="write"), "not in the module's capabilities"),
        (lambda d: d.update(capabilities=["read", "network"]), "needs capabilities"),
        (lambda d: d.update(module_id="search"), "reserved"),
        (lambda d: d.update(module_id="project_context"), "reserved"),
        (lambda d: d.update(module_id="Bad Id"), "module_id"),
        (lambda d: d.update(version="one"), "version"),
        (lambda d: d["engine_compatibility"].update(engine="about three"), "comparator"),
        (lambda d: d["tools"].append(copy.deepcopy(d["tools"][0])), "duplicate tool"),
        (
            lambda d: d["tools"][0].update(input_schema={"type": "object", "properties": 5}),
            "JSON Schema",
        ),
        (lambda d: d["tools"][0].update(input_schema={"type": "string"}), "type 'object'"),
        (lambda d: d["tools"][0].update(name="Fetch.Now"), "name"),
        (
            lambda d: d.update(module_id="a" * 64, tools=[{**d["tools"][0], "name": "b" * 64}]),
            "MCP name",
        ),
        (lambda d: d.update(surprise=True), "surprise"),
        (lambda d: d["transport"].update(command=[]), "command"),
    ],
)
def test_invalid_manifests_are_rejected(helpers: Any, mutate: Any, needle: str) -> None:
    data = _base(helpers)
    mutate(data)
    with pytest.raises(InvalidInputError) as info:
        parse_manifest(data)
    text = info.value.message + " " + " ".join(info.value.details.get("errors", []))
    assert needle in text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["configuration"].update(
            secrets=[{"name": "api_key", "env": "NEWS_KEY", "value": "hunter2-hunter2"}]
        ),
        lambda d: d["configuration"]["settings"].append(
            {"name": "api_token", "type": "string", "default": "abcdef123456"}
        ),
        lambda d: d["configuration"]["settings"].append(
            {"name": "endpoint", "type": "string", "default": "https://user:pw@feeds.example.org/"}
        ),
        lambda d: d["configuration"]["settings"].append(
            {"name": "region", "type": "string", "default": "ghp_" + "a" * 36}
        ),
        lambda d: d["configuration"]["settings"].append(
            {"name": "x", "type": "string", "default": "-----BEGIN RSA PRIVATE KEY-----"}
        ),
    ],
)
def test_embedded_secret_values_are_rejected(helpers: Any, mutate: Any) -> None:
    data = _base(helpers)
    mutate(data)
    with pytest.raises(InvalidInputError) as info:
        parse_manifest(data)
    assert info.value.details["secret_paths"]
    # The reported paths never carry the value itself.
    assert "hunter2" not in json.dumps(info.value.to_dict())


def test_secret_references_by_env_name_are_fine(helpers: Any) -> None:
    data = _base(helpers)
    data["configuration"]["secrets"] = [{"name": "api_key", "env": "NEWS_API_KEY"}]
    assert parse_manifest(data).configuration.secrets[0].env == "NEWS_API_KEY"
    assert find_embedded_secrets(data) == []


def test_yaml_manifest(tmp_path: Path, helpers: Any) -> None:
    import yaml

    target = tmp_path / "mod"
    target.mkdir()
    (target / "module.yaml").write_text(yaml.safe_dump(_base(helpers)))
    assert load_manifest(target).module_id == "example.news"


def test_unreadable_manifests(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="no module manifest"):
        load_manifest(tmp_path)
    (tmp_path / "module.json").write_text("{nope")
    with pytest.raises(InvalidInputError, match="not valid JSON"):
        load_manifest(tmp_path)
    (tmp_path / "module.json").write_text("x" * 300_000)
    with pytest.raises(InvalidInputError, match="too large"):
        load_manifest(tmp_path)


def test_engine_range() -> None:
    from knowledge_engine.modules.manifest import EngineCompatibility

    compat = EngineCompatibility(engine=">=3.0.0,<4.0.0")
    assert compat.accepts_engine("3.0.0")
    assert compat.accepts_engine("3.9.1.dev2")
    assert not compat.accepts_engine("4.0.0")
    assert not compat.accepts_engine("garbage")


# ── discovery and registration ───────────────────────────────────────


def test_discover_never_executes_module_code(mod_env: Any, host: Any) -> None:
    marker = mod_env.root / "executed"
    evil = mod_env.modules / "evil"
    evil.mkdir()
    (evil / "evil.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    data = json.loads((mod_env.probe / "module.json").read_text())
    data.update(
        module_id="test.evil", transport={"kind": "script", "command": ["python", "evil.py"]}
    )
    data["health"] = {"command": ["python", "evil.py"]}
    (evil / "module.json").write_text(json.dumps(data))
    (mod_env.modules / "broken").mkdir()
    (mod_env.modules / "broken" / "module.json").write_text('{"manifest_version": 7}')

    found = {item["directory"]: item for item in host.discover()}
    assert found["news-fetcher"]["valid"] and found["news-fetcher"]["module_id"] == "example.news"
    assert found["broken"]["valid"] is False
    assert found["broken"]["error"]["code"] == "incompatible"
    host.register(evil)
    assert not marker.exists(), "discovery/registration must not run module code"
    assert host.status("test.evil")["state"] == "registered"
    assert host.status("test.evil")["enabled"] is False


def test_register_inside_the_corpus_is_denied(mod_env: Any, host: Any, helpers: Any) -> None:
    import shutil

    inside = mod_env.corpus / "tools" / "news"
    shutil.copytree(helpers.EXAMPLES / "news-fetcher", inside)
    with pytest.raises(DeniedError, match="corpus"):
        host.register(inside)
    assert host.list_modules() == []


def test_corpus_bootstrap_never_registers_modules(mod_env: Any, host: Any, helpers: Any) -> None:
    import shutil

    from knowledge_engine.cli import main

    shutil.copytree(helpers.EXAMPLES / "news-fetcher", mod_env.corpus / "news-tool")
    assert main(["bootstrap"]) == 0
    assert host.list_modules() == []


def test_http_style_registration_takes_names_only(mod_env: Any, host: Any) -> None:
    for bad in ("../news-fetcher", "/etc", "a/b", ".."):
        with pytest.raises(InvalidInputError):
            host.register_named(bad)
    assert host.register_named("news-fetcher")["module_id"] == "example.news"


def test_reregistering_disables_and_keeps_configuration(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.news, allow_live_http=False)
    assert host.status("example.news")["enabled"] is True
    again = host.register(mod_env.news)
    assert again["created"] is False
    assert again["enabled"] is False
    assert again["settings"] == {"allow_live_http": False}


def test_catalog_refuses_a_newer_schema(mod_env: Any) -> None:
    import sqlite3

    from knowledge_engine.modules.catalog import ModuleCatalog

    path = mod_env.data / "modules.db"
    ModuleCatalog.open(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (99, 'x')")
    with pytest.raises(KnowledgeError) as info:
        ModuleCatalog.open(path)
    assert info.value.code == "incompatible"
