"""Registry JSON Schema: shipped schema, validation on load/write, ``registry validate`` CLI.

``test_repo_registry_validates`` is the check CI relies on for the committed
``corpus/registry.json`` (equivalent to ``knowledge-engine registry validate
../corpus/registry.json`` run from ``engine/``).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from knowledge_engine import cli
from knowledge_engine.core import registry as registry_mod
from knowledge_engine.core.registry import (
    Registry,
    RegistryEntry,
    RegistryValidationError,
    RegistryValidationIssue,
    load_registry_document,
    registry_schema,
    validate_registry_document,
    validation_issues,
)

REPO = Path(__file__).resolve().parents[2]
PACKAGE_SCHEMA = REPO / "engine" / "src" / "knowledge_engine" / "core" / "registry.schema.json"
CORPUS_SCHEMA = REPO / "corpus" / "registry.schema.json"
CORPUS_REGISTRY = REPO / "corpus" / "registry.json"
FIXTURES = Path(__file__).parent / "fixtures" / "registry"

#: Every invalid fixture and the exact (pointer, keyword) pairs it must report.
INVALID_FIXTURES: dict[str, list[tuple[str, str]]] = {
    "invalid-unknown-key.json": [("", "additionalProperties")],
    "invalid-wrong-type.json": [
        ("/libraries/0/enabled", "type"),
        ("/libraries/0/tags", "type"),
        ("/lifecycle/watch_enabled", "type"),
        ("/skills", "type"),
    ],
    "invalid-missing-required.json": [("/kits/0", "required"), ("/kits/0", "required")],
}


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_error(path: Path) -> RegistryValidationError:
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry_document(path)
    return excinfo.value


def _write(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# ── The schema itself ────────────────────────────────────────────────


def test_schema_copies_are_identical() -> None:
    """The package copy is the source of truth; corpus/ keeps a byte-identical copy."""
    assert CORPUS_SCHEMA.read_bytes() == PACKAGE_SCHEMA.read_bytes(), (
        "corpus/registry.schema.json drifted from the packaged schema; copy "
        "engine/src/knowledge_engine/core/registry.schema.json over it"
    )


def test_schema_is_packaged_and_well_formed() -> None:
    schema = registry_schema()
    assert schema == json.loads(PACKAGE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == (
        "https://raw.githubusercontent.com/ARC-907/knowledge-engine/main/corpus/registry.schema.json"
    )
    assert schema["additionalProperties"] is False
    assert {"libraries", "skills", "kits", "tools", "lifecycle"} <= set(schema["properties"])
    assert schema["title"] and schema["description"]
    assert "registry.db" not in json.dumps(schema)
    # A fresh copy each call: callers cannot corrupt the cached schema.
    schema["properties"].clear()
    assert registry_schema()["properties"]


def test_schema_buckets_match_the_registry_kinds() -> None:
    schema = registry_schema()
    buckets = {
        name
        for name, prop in schema["properties"].items()
        if prop.get("type") == "array" and "items" in prop
    }
    assert buckets == set(registry_mod.BUCKET_TO_KIND)
    entry_props = set(schema["$defs"]["entry"]["properties"])
    assert set(RegistryEntry.__dataclass_fields__) <= entry_props


# ── Documents ────────────────────────────────────────────────────────


def test_repo_registry_validates() -> None:
    """The committed demo corpus/registry.json matches the schema (CI check)."""
    doc = load_registry_document(CORPUS_REGISTRY)
    assert {"libraries", "skills", "kits", "tools"} <= set(doc)


def test_valid_fixture_validates() -> None:
    assert validation_issues(_fixture("valid.json")) == []


@pytest.mark.parametrize("name", sorted(INVALID_FIXTURES))
def test_invalid_fixtures_report_every_error(name: str) -> None:
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry_document(FIXTURES / name)
    err = excinfo.value
    assert err.path == FIXTURES / name
    assert [(i.pointer, i.keyword) for i in err.errors] == INVALID_FIXTURES[name]
    count = len(INVALID_FIXTURES[name])
    assert f"({count} error" in err.message
    rendered = str(err)
    assert rendered.startswith(f"{FIXTURES / name}: does not match the registry schema")
    assert len(rendered.splitlines()) == 1 + count
    assert err.to_dict()["errors"] == [i.to_dict() for i in err.errors]


def test_error_messages_name_the_problem() -> None:
    issues = validation_issues(_fixture("invalid-unknown-key.json"))
    assert str(issues[0]) == "/: Additional properties are not allowed ('librarys' was unexpected)"
    missing = sorted(
        i.message for i in validation_issues(_fixture("invalid-missing-required.json"))
    )
    assert missing == ["'id' is a required property", "'path' is a required property"]


def test_engine_documents_validate(tmp_path: Path) -> None:
    """Everything the engine itself writes passes its own schema."""
    validate_registry_document(registry_mod._default_document())
    registry = Registry(tmp_path / "registry.json")
    registry.upsert_many(
        RegistryEntry(id=f"{kind}-x", kind=kind, name="X", path=f"{bucket}/x", tags=["t"])
        for kind, bucket in registry_mod.KIND_TO_BUCKET.items()
    )
    registry.set_enabled("kit-x", False)
    registry.set_lifecycle(watch_enabled=False, auto_register=True, custom_flag="kept")
    doc = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert validation_issues(doc) == []
    assert isinstance(doc["last_updated"], float)


def test_older_documents_stay_valid() -> None:
    """Shapes written against the earlier schema or by earlier releases still load."""
    validate_registry_document({"libraries": [], "skills": [], "kits": [], "tools": []})
    validate_registry_document(
        {
            "version": 1,
            "corpus_root": "corpus/",
            "libraries": [],
            "skills": [],
            "tools": [{"id": "t", "name": "T", "path": "capabilities/t", "kind": "service"}],
            "lifecycle": {
                "watch_enabled": True,
                "auto_register_new_folders": False,
                "on_toggle_reindex": "manual",
                "schedule": None,
            },
        }
    )


@pytest.mark.parametrize(
    ("doc", "pointer"),
    [
        ({"version": 2}, "/version"),
        ({"skills": [{"id": "", "name": "S", "path": "s"}]}, "/skills/0/id"),
        ({"kits": [{"id": "k", "name": "K", "path": "k", "kind": "skill"}]}, "/kits/0/kind"),
        ({"lifecycle": {"on_toggle_reindex": "sometimes"}}, "/lifecycle/on_toggle_reindex"),
        ({"libraries": [{"id": "l", "name": "L", "path": "l", "created": "today"}]}, None),
    ],
)
def test_specific_violations(doc: dict[str, Any], pointer: str | None) -> None:
    issues = validation_issues(doc)
    assert len(issues) == 1
    assert issues[0].pointer == (pointer or "/libraries/0/created")


def test_json_pointers_are_escaped() -> None:
    assert registry_mod._json_pointer(["a/b", "c~d", 0]) == "/a~1b/c~0d/0"
    assert registry_mod._json_pointer([]) == ""
    assert str(RegistryValidationIssue("", "boom")) == "/: boom"


def test_malformed_json_is_a_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text('{"version": 1,', encoding="utf-8")
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry_document(path)
    assert excinfo.value.message == "is not valid JSON"
    [issue] = excinfo.value.errors
    assert issue.keyword == "json"
    assert "line 1" in issue.message
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_non_object_is_a_validation_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "registry.json", [1, 2])
    with pytest.raises(RegistryValidationError, match="JSON object") as excinfo:
        load_registry_document(path)
    assert isinstance(excinfo.value, ValueError)
    assert [i.keyword for i in excinfo.value.errors] == ["type"]


def test_load_registry_document_never_creates_the_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_registry_document(tmp_path / "missing" / "registry.json")
    assert not (tmp_path / "missing").exists()


# ── Registry: validation on load and before every write ─────────────


def test_invalid_registry_fails_fast_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "registry.json"
    shutil.copy(FIXTURES / "invalid-wrong-type.json", path)
    before = path.read_bytes()
    with (
        caplog.at_level(logging.ERROR, logger="knowledge_engine.core.registry"),
        pytest.raises(RegistryValidationError) as excinfo,
    ):
        Registry(path)
    assert len(excinfo.value.errors) == 4
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    for pointer in ("/libraries/0/enabled", "/libraries/0/tags", "/skills"):
        assert pointer in record.getMessage()
    assert path.read_bytes() == before


def test_reload_of_an_invalid_file_keeps_the_loaded_document(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    registry = Registry(path)
    registry.upsert(RegistryEntry(id="kit-a", kind="kit", name="A", path="kits/a"))
    shutil.copy(FIXTURES / "invalid-unknown-key.json", path)
    with pytest.raises(RegistryValidationError):
        registry.reload()
    assert registry.get("kit-a") is not None
    shutil.copy(FIXTURES / "valid.json", path)
    registry.reload()
    assert registry.get("kit-a") is None
    assert registry.get("kit-onboarding") is not None
    # A legacy transport kind in the tools bucket is still reported as a tool.
    assert registry.get("tool-legacy")["kind"] == "tool"  # type: ignore[index]


def test_invalid_mutation_is_refused_without_writing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "registry.json"
    registry = Registry(path)
    before = path.read_bytes()
    bad = RegistryEntry(id="kit-a", kind="kit", name="A", path="kits/a", tags="a,b")  # type: ignore[arg-type]
    with (
        caplog.at_level(logging.ERROR, logger="knowledge_engine.core.registry"),
        pytest.raises(RegistryValidationError) as excinfo,
    ):
        registry.upsert(bad)
    assert [i.pointer for i in excinfo.value.errors] == ["/kits/0/tags"]
    assert "/kits/0/tags" in caplog.text
    with pytest.raises(RegistryValidationError):
        registry.set_lifecycle(watch_enabled="yes")
    assert path.read_bytes() == before
    assert registry.get("kit-a") is None
    assert registry.lifecycle() == {"watch_enabled": True, "auto_register": True}
    assert list(tmp_path.glob(".registry.json.*.tmp")) == []


# ── CLI: registry validate ───────────────────────────────────────────


def test_cli_validate_valid_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["registry", "validate", str(FIXTURES / "valid.json")]) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"{FIXTURES / 'valid.json'}: valid"


def test_cli_validate_repo_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["registry", "validate", str(CORPUS_REGISTRY)]) == 0
    assert capsys.readouterr().out.strip().endswith(": valid")


@pytest.mark.parametrize("name", sorted(INVALID_FIXTURES))
def test_cli_validate_invalid_file(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["registry", "validate", str(FIXTURES / name)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0] == f"{FIXTURES / name}: " + _load_error(FIXTURES / name).message
    assert len(lines) == 1 + len(INVALID_FIXTURES[name])
    for pointer, _keyword in INVALID_FIXTURES[name]:
        assert any(line.startswith(f"  {pointer or '/'}: ") for line in lines[1:])


def test_cli_validate_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    path = FIXTURES / "invalid-missing-required.json"
    assert cli.main(["registry", "validate", "--json", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is False
    assert report["path"] == str(path)
    assert [e["pointer"] for e in report["errors"]] == ["/kits/0", "/kits/0"]

    assert cli.main(["registry", "validate", "--json", str(FIXTURES / "valid.json")]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "valid": True,
        "path": str(FIXTURES / "valid.json"),
        "message": "valid",
        "errors": [],
    }


def test_cli_validate_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope" / "registry.json"
    assert cli.main(["registry", "validate", str(missing)]) == 1
    assert "cannot read the registry" in capsys.readouterr().err
    assert cli.main(["registry", "validate", "--json", str(missing)]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    assert not missing.parent.exists()


def test_cli_validate_defaults_to_the_configured_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "corpus" / "registry.json"
    monkeypatch.setenv("KE_REGISTRY_PATH", str(path))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["registry", "validate"]) == 1  # missing: not created
    assert not path.exists()
    assert not (tmp_path / "data").exists()
    capsys.readouterr()
    path.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "valid.json", path)
    assert cli.main(["registry", "validate"]) == 0
    assert capsys.readouterr().out.strip() == f"{path}: valid"


@pytest.mark.parametrize("command", [["bootstrap"], ["reindex"], ["info"], ["search", "x"]])
def test_corpus_commands_surface_validation_errors(
    command: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "libraries" / "lib").mkdir(parents=True)
    path = corpus / "registry.json"
    shutil.copy(FIXTURES / "invalid-wrong-type.json", path)
    before = path.read_bytes()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(path))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(tmp_path / "pipeline"))
    assert cli.main(command) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{path}: does not match the registry schema (4 errors)" in captured.err
    assert "  /libraries/0/tags: 'a,b' is not of type 'array'" in captured.err
    assert "knowledge-engine registry validate" in captured.err
    assert path.read_bytes() == before
