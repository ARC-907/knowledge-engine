"""Serena adapter units: witness, configuration, allowlist, normalization, repository state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge_engine.knowledge.contracts import CodeLocator
from knowledge_engine.knowledge.errors import InvalidInputError, OutputInvalidError
from knowledge_engine.serena.config import SerenaConfig, load_projects
from knowledge_engine.serena.repo import PathEscapeError, RepoState, compute_state, relative_path
from knowledge_engine.serena.session import CHILD_ENV, LAUNCH_ARGS, child_environment
from knowledge_engine.serena.tools import (
    ALLOWED_TOOLS,
    FindSymbolArgs,
    ReferencingSymbolsArgs,
    normalize,
    parse_args,
    path_for,
    record_id_for,
    tool_definitions,
    upstream_arguments,
    upstream_tool,
)
from knowledge_engine.serena.witness import compatibility_problems, load_witness, schema_hash

HERE = Path(__file__).resolve().parent
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
SHAPES_A = b"""class Shape:
    def area(self):
        return 0


def make_a():
    return Shape()
"""
SCHEMAS: dict[str, Any] = json.loads(
    (HERE / "serena_2_1_0_read_schemas.json").read_text(encoding="utf-8")
)

# ── witness ──────────────────────────────────────────────────────────


def test_witness_hashes_match_the_recorded_schemas() -> None:
    witness = load_witness()
    assert (witness.server_name, witness.server_version) == ("Serena", "2.1.0")
    assert set(witness.tool_schema_sha256) == set(ALLOWED_TOOLS)
    for tool, expected in witness.tool_schema_sha256.items():
        assert schema_hash(SCHEMAS[tool]) == expected
    # Key order does not matter; any content change does.
    reordered = dict(reversed(list(SCHEMAS["find_symbol"].items())))
    assert schema_hash(reordered) == witness.tool_schema_sha256["find_symbol"]


def test_compatibility_problems_name_every_difference() -> None:
    ok = compatibility_problems("Serena", "2.1.0", SCHEMAS, expected_version="2.1.0")
    assert ok == []
    drifted = {**SCHEMAS, "find_symbol": {**SCHEMAS["find_symbol"], "required": []}}
    problems = compatibility_problems("Serena", "2.2.0", drifted, expected_version="2.1.0")
    assert any("version" in p for p in problems)
    assert any("find_symbol" in p for p in problems)
    missing = {k: v for k, v in SCHEMAS.items() if k != "find_referencing_symbols"}
    problems = compatibility_problems("Other", "2.1.0", missing, expected_version="2.1.0")
    assert any("server name" in p for p in problems)
    assert any("not offered" in p for p in problems)


# ── launch ───────────────────────────────────────────────────────────


def test_launch_forces_the_tool_interface_and_disables_reporting(tmp_path: Path) -> None:
    flags = list(LAUNCH_ARGS)
    assert flags[flags.index("--agent-interface") + 1] == "tools"
    assert flags[flags.index("--transport") + 1] == "stdio"
    assert "--project-from-cwd" not in flags
    for flag in ("--enable-web-dashboard", "--open-web-dashboard", "--enable-gui-log-window"):
        assert flags[flags.index(flag) + 1] == "false"
    env = child_environment(tmp_path / "home", {"PATH": "", "HOME": "/elsewhere"})
    assert env["SERENA_USAGE_REPORTING"] == "false" == CHILD_ENV["SERENA_USAGE_REPORTING"]
    assert env["HOME"] == str(tmp_path / "home")


# ── configuration ────────────────────────────────────────────────────


def test_project_file_parses_toml_and_json_and_skips_bad_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    toml = tmp_path / "projects.toml"
    toml.write_bytes(
        (
            "[[projects]]\n"
            f'project_id = "p1"\nroot = "{repo.as_posix()}"\nrepository_id = "example.org/r"\n'
            "[[projects]]\n"
            'project_id = "Bad Id"\nroot = "/x"\n'
            "[[projects]]\n"
            'project_id = "p3"\nroot = "relative/path"\n'
        ).encode()
    )
    projects, warnings = load_projects(toml)
    assert list(projects) == ["p1"]
    assert projects["p1"].root == repo.resolve()
    assert projects["p1"].repository_id == "example.org/r"
    assert len(warnings) == 2
    as_json = tmp_path / "projects.json"
    as_json.write_bytes(
        json.dumps({"projects": [{"project_id": "p1", "root": str(repo)}]}).encode()
    )
    projects, _ = load_projects(as_json)
    assert projects["p1"].repository_id == "repo"
    assert load_projects(tmp_path / "absent.toml") == ({}, ())


def test_project_file_rejects_duplicates_and_shared_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "p.json"
    entry = {"project_id": "p1", "root": str(repo)}
    path.write_bytes(json.dumps({"projects": [entry, entry]}).encode())
    with pytest.raises(InvalidInputError, match="twice"):
        load_projects(path)
    path.write_bytes(
        json.dumps({"projects": [entry, {"project_id": "p2", "root": str(repo)}]}).encode()
    )
    with pytest.raises(InvalidInputError, match="share the root"):
        load_projects(path)
    path.write_bytes(b"not json")
    with pytest.raises(InvalidInputError):
        load_projects(path)


def test_settings_resolve_and_disabled_reads_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.core.settings import Settings

    broken = tmp_path / "broken.toml"
    broken.write_bytes(b"[[[")
    monkeypatch.setenv("KE_SERENA_PROJECTS", str(broken))
    disabled = SerenaConfig.from_settings(Settings())
    assert disabled.enabled is False and disabled.projects == {}
    monkeypatch.setenv("KE_SERENA_ENABLED", "1")
    monkeypatch.setenv("KE_SERENA_COMMAND", "uvx --from serena-agent==2.1.0 serena")
    with pytest.raises(InvalidInputError):
        SerenaConfig.from_settings(Settings())
    broken.write_bytes(b"projects = []\n")
    config = SerenaConfig.from_settings(Settings())
    assert config.command == ("uvx", "--from", "serena-agent==2.1.0", "serena")
    assert config.expected_version == "2.1.0"
    with pytest.raises(InvalidInputError, match="no Serena repository"):
        config.project("p1")


# ── allowlist and arguments ──────────────────────────────────────────


@pytest.mark.parametrize(
    "tool",
    [
        "execute_shell_command",
        "create_text_file",
        "replace_symbol_body",
        "insert_after_symbol",
        "rename_symbol",
        "safe_delete_symbol",
        "write_memory",
        "delete_memory",
        "onboarding",
        "activate_project",
        "serena_repl",
        "serena.execute_shell_command",
        "a_tool_added_in_a_future_release",
    ],
)
def test_only_the_three_read_tools_are_forwarded(tool: str) -> None:
    with pytest.raises(InvalidInputError, match="not allowed"):
        upstream_tool(tool)


def test_engine_tools_map_to_upstream_names_and_have_engine_schemas() -> None:
    assert upstream_tool("serena.find_symbol") == "find_symbol"
    names = {d["name"] for d in tool_definitions()}
    assert names == {f"serena.{t}" for t in ALLOWED_TOOLS}
    for definition in tool_definitions():
        schema = definition["inputSchema"]
        assert "project_id" in schema["required"]
        assert schema.get("additionalProperties") is False


def test_arguments_are_validated_and_paths_contained(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_bytes(b"x = 1\n")
    with pytest.raises(InvalidInputError):
        parse_args("serena.find_symbol", {"project_id": "p1"})
    with pytest.raises(InvalidInputError):
        parse_args("serena.find_symbol", {"project_id": "p1", "name_path_pattern": "A", "x": 1})
    args = parse_args(
        "serena.find_symbol",
        {"project_id": "p1", "name_path_pattern": "A", "relative_path": "pkg\\a.py"},
    )
    forwarded = upstream_arguments(args, root)
    assert forwarded["relative_path"] == "pkg/a.py"
    assert set(forwarded) <= set(json.loads(json.dumps(SCHEMAS["find_symbol"]))["properties"])
    for escape in ("../outside.py", "/etc/passwd", "pkg/../../x"):
        bad = parse_args(
            "serena.get_symbols_overview", {"project_id": "p1", "relative_path": escape}
        )
        with pytest.raises(InvalidInputError, match="inside the project"):
            upstream_arguments(bad, root)


def test_relative_path_rejects_symlink_escapes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret = 1\n")
    link = root / "link.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(PathEscapeError):
        relative_path(root, "link.py")


# ── normalization ────────────────────────────────────────────────────

STATE = RepoState("a" * 40, None)


def test_find_symbol_lines_become_one_based_exclusive(tmp_path: Path) -> None:
    root = tmp_path
    (root / "pkg").mkdir()
    (root / "pkg" / "shapes.py").write_bytes(SHAPES_A)
    payload = [
        {
            "name_path": "Shape/area",
            "kind": "Method",
            "relative_path": "pkg/shapes.py",
            "body_location": {"start_line": 1, "end_line": 2},
        },
        {"name_path": "Evil", "kind": "Class", "relative_path": "../evil.py"},
    ]
    args = FindSymbolArgs(project_id="p1", name_path_pattern="area")
    result = normalize(
        "find_symbol", args, payload, root=root, repository_id="example.org/a", state=STATE
    )
    (entry,) = result.results
    locator = CodeLocator.model_validate(entry["locator"])
    assert (locator.start_line, locator.end_line) == (2, 4)
    assert locator.symbol == "Shape.area" and locator.commit == "a" * 40
    lines = SHAPES_A.splitlines(keepends=True)[locator.start_line - 1 : locator.end_line - 1]
    assert b"".join(lines).startswith(b"    def area")
    assert result.rejected_paths == ["../evil.py"]
    assert path_for(entry["record_id"]) == "pkg/shapes.py"


def test_references_are_flattened_sorted_and_limited(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x\n" * 20)
    (tmp_path / "b.py").write_bytes(b"y\n" * 20)
    payload = {
        "b.py": {
            "Function": [{"name_path": "f", "body_location": {"start_line": 3, "end_line": 3}}]
        },
        "a.py": {
            "Variable": [
                {"name_path": "v", "body_location": {"start_line": 5, "end_line": 5}},
                {"name_path": "w", "body_location": {"start_line": 1, "end_line": 1}},
            ]
        },
        "/abs/escape.py": {"File": [{"name_path": "z"}]},
    }
    args = ReferencingSymbolsArgs(
        project_id="p1", name_path="X", relative_path="a.py", max_results=2
    )
    result = normalize(
        "find_referencing_symbols", args, payload, root=tmp_path, repository_id="r", state=STATE
    )
    assert [(e["path"], e["name_path"]) for e in result.results] == [("a.py", "w"), ("a.py", "v")]
    assert result.truncated is True
    assert result.rejected_paths == ["/abs/escape.py"]


def test_malformed_answers_are_output_invalid(tmp_path: Path) -> None:
    args = FindSymbolArgs(project_id="p1", name_path_pattern="A")
    with pytest.raises(OutputInvalidError):
        normalize(
            "find_symbol", args, {"not": "a list"}, root=tmp_path, repository_id="r", state=STATE
        )


def test_unusable_repository_state_gives_unsupported_locators(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"def f():\n    pass\n")
    payload = [
        {
            "name_path": "f",
            "kind": "Function",
            "relative_path": "a.py",
            "body_location": {"start_line": 0, "end_line": 1},
        }
    ]
    state = RepoState(None, None, "not a git work tree with a commit")
    result = normalize(
        "find_symbol",
        FindSymbolArgs(project_id="p1", name_path_pattern="f"),
        payload,
        root=tmp_path,
        repository_id="r",
        state=state,
    )
    assert result.results[0]["locator"] == {
        "kind": "unsupported",
        "reason": "not a git work tree with a commit",
    }


def test_record_ids_round_trip_and_stay_in_the_id_pattern() -> None:
    import re

    from knowledge_engine.knowledge.contracts import RECORD_ID_PATTERN

    for path in ("pkg/shapes.py", "a b/ü.py", "x" * 100 + ".py"):
        rid = record_id_for(path)
        assert re.fullmatch(RECORD_ID_PATTERN, rid)
        assert path_for(rid) == path
    long_id = record_id_for("d/" * 200 + "f.py")
    assert long_id.startswith("h.") and path_for(long_id) is None


# ── repository state ─────────────────────────────────────────────────


@needs_git
def test_clean_tree_is_a_commit_and_dirty_tree_a_snapshot(
    tmp_path: Path, kit: SimpleNamespace
) -> None:
    git = kit.git
    root = kit.make_repo(tmp_path / "r", {"a.py": b"x = 1\n"})
    clean = compute_state(root)
    assert clean.commit == git(root, "rev-parse", "HEAD") and clean.snapshot_digest is None
    (root / "a.py").write_bytes(b"x = 2\n")
    dirty = compute_state(root)
    assert dirty.commit is None and dirty.snapshot_digest is not None
    assert compute_state(root) == dirty  # deterministic
    (root / "a.py").write_bytes(b"x = 3\n")
    assert compute_state(root).snapshot_digest != dirty.snapshot_digest
    (root / "a.py").write_bytes(b"x = 1\n")
    (root / "new.py").write_bytes(b"y = 1\n")  # untracked files make the tree dirty too
    assert compute_state(root).snapshot_digest is not None
    (root / "new.py").unlink()
    assert compute_state(root) == clean
    # Serena writes its own state into the project root; that is not a code change.
    (root / ".serena" / "cache").mkdir(parents=True)
    (root / ".serena" / "project.yml").write_bytes(b"project_name: r\n")
    (root / ".serena" / "cache" / "x.pkl").write_bytes(b"\x00")
    assert compute_state(root) == clean


@needs_git
def test_non_git_root_has_no_usable_version(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    state = compute_state(plain)
    assert not state.usable and state.reason
