"""``knowledge migrate``: read-only dry run, backup, resumable apply, idempotent rerun."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine import cli
from knowledge_engine.knowledge import backfill
from knowledge_engine.knowledge.backfill import CORPUS_SOURCE_ID, corpus_record_id
from knowledge_engine.knowledge.contracts import CurationStatus, QueryContext, ResearchType
from knowledge_engine.knowledge.errors import InvalidInputError, UnavailableError
from knowledge_engine.knowledge.store import KnowledgeStore

FILES = {
    "libs/a/one.md": "# One\n\nalpha text\n",
    "libs/a/sub/two.md": "# Two\n",
    "libs/a/sub/three.txt": "plain three\n",
    "libs/a/image.png": "not indexed",
    "libs/off/four.md": "# Four\n",
    "skills/x/SKILL.md": "# Skill X\n",
}


def _entry(
    entry_id: str, path: str, *, enabled: bool = True, kind: str = "library"
) -> dict[str, Any]:
    return {"id": entry_id, "kind": kind, "name": entry_id, "path": path, "enabled": enabled}


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path / "corpus"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "libs/a/latin1.md").write_bytes(b"# Caf\xe9\n")
    registry = {
        "version": 1,
        "libraries": [
            _entry("lib-a", "libs/a"),
            _entry("lib-a-sub", "libs/a/sub"),  # nested inside lib-a
            _entry("lib-off", "libs/off", enabled=False),
            _entry("lib-missing", "libs/missing"),
        ],
        "skills": [_entry("skill-x", "skills/x", kind="skill")],
        "kits": [],
        "tools": [],
    }
    (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(root))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(root / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    return {
        "root": root,
        "registry": root / "registry.json",
        "data": data,
        "db": data / "knowledge.db",
    }


def _kw(corpus: dict[str, Path], **extra: Any) -> dict[str, Any]:
    return {
        "corpus_root": corpus["root"],
        "registry_path": corpus["registry"],
        "db_path": corpus["db"],
        "workspace_id": "default",
        **extra,
    }


EXPECTED_PATHS = {
    "libs/a/one.md",
    "libs/a/sub/two.md",
    "libs/a/sub/three.txt",
    "libs/a/latin1.md",
    "libs/off/four.md",
    "skills/x/SKILL.md",
}


def test_dry_run_reports_and_writes_nothing(corpus: dict[str, Path]) -> None:
    report = backfill.plan(**_kw(corpus))
    assert not corpus["data"].exists(), "a dry run must not create anything"
    assert report["mode"] == "dry-run"
    assert report["records"] == {
        "corpus_files": 6,
        "enabled_files": 5,
        "already_registered": 0,
        "to_create": 6,
    }
    assert report["entries"]["missing_content"] == ["lib-missing"]
    assert report["entries"] == {"total": 5, "enabled": 4, "missing_content": ["lib-missing"]}
    dups = report["duplicates"]
    assert dups["files_in_several_entries"] == 2
    assert {d["logical_path"] for d in dups["examples"]} == {
        "libs/a/sub/two.md",
        "libs/a/sub/three.txt",
    }
    assert dups["examples"][0]["entry_ids"] == ["lib-a", "lib-a-sub"]
    assert {d["owner_entry_id"] for d in dups["examples"]} == {"lib-a-sub"}
    assert report["unknown_metadata"]["research_type_defaulted_to_other"] == 6
    assert report["unknown_metadata"]["non_utf8_examples"] == ["libs/a/latin1.md"]
    assert report["disk"]["sufficient"] and report["disk"]["required_bytes"] > 0
    assert report["workspace"] == {"workspace_id": "default", "exists": False}
    assert report["schema"] == {"current_version": 0, "target_version": 1, "store_exists": False}
    assert report["resume"] is None


def test_apply_backs_up_then_registers_everything_unclassified(corpus: dict[str, Path]) -> None:
    report = backfill.apply(**_kw(corpus))
    assert report["complete"] and report["created"] == 6 and report["existing"] == 0
    assert report["workspace_created"] and report["source_created"]
    assert report["reconciliation"]["reconciled"]
    backup_dir = Path(report["backup"]["path"])
    assert backup_dir.parent == corpus["data"] / "backups"
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["knowledge_db_existed"] is False
    assert (backup_dir / "registry.json").read_bytes() == corpus["registry"].read_bytes()
    with KnowledgeStore.open(corpus["db"]) as store:
        assert [w.workspace_id for w in store.list_workspaces()] == ["default"]
        assert [s.source_id for s in store.list_sources("default")] == [CORPUS_SOURCE_ID]
        assert store.list_projects("default") == []
        assert store.list_collections("default") == []
        _, records, total = store.query_records(QueryContext(workspace_id="default"), limit=500)
        assert total == 6
        assert {r.logical_path for r in records} == EXPECTED_PATHS
        for record in records:
            assert record.research_type is ResearchType.OTHER
            assert record.curation_status is CurationStatus.UNCLASSIFIED
            assert record.scope.project_id is None and record.collection_ids == ()
            assert record.record_id == corpus_record_id(record.logical_path or "")
        titles = {r.logical_path: r.title for r in records}
        assert titles["libs/a/one.md"] == "One"
        assert titles["libs/a/sub/three.txt"] == "three"


def test_rerun_creates_no_duplicates_and_keeps_curation(corpus: dict[str, Path]) -> None:
    backfill.apply(**_kw(corpus))
    with KnowledgeStore.open(corpus["db"]) as store:
        rid = corpus_record_id("libs/a/one.md")
        store.classify(CORPUS_SOURCE_ID, rid, research_type="paper", curation_status="reviewed")
    again = backfill.apply(**_kw(corpus))
    assert again["complete"] and again["created"] == 0 and again["existing"] == 6
    assert not again["workspace_created"] and not again["source_created"]
    # The second backup holds the first run's database.
    assert "knowledge.db" in again["backup"]["files"]
    backups = sorted((corpus["data"] / "backups").iterdir())
    assert len(backups) == 2
    with KnowledgeStore.open(corpus["db"]) as store:
        assert store.count_records(CORPUS_SOURCE_ID) == 6
        record = store.get_record(CORPUS_SOURCE_ID, corpus_record_id("libs/a/one.md"))
        assert record.research_type is ResearchType.PAPER
        assert record.curation_status is CurationStatus.REVIEWED
    plan = backfill.plan(**_kw(corpus))
    assert plan["records"]["to_create"] == 0 and plan["workspace"]["exists"]


def test_restart_resumes_from_the_persisted_checkpoint(corpus: dict[str, Path]) -> None:
    first = backfill.apply(**_kw(corpus, batch_size=2, max_batches=1))
    assert not first["complete"] and first["processed"] == 2 and first["remaining"] == 4
    assert first["run"]["status"] == "running"
    # "Restart": a fresh process sees the checkpoint in the dry run ...
    plan = backfill.plan(**_kw(corpus))
    assert plan["resume"]["run_id"] == first["run_id"]
    assert plan["resume"]["processed"] == 2
    assert plan["records"]["already_registered"] == 2
    # ... and the next apply continues after it instead of starting over.
    second = backfill.apply(**_kw(corpus, batch_size=2))
    assert second["resumed"] and second["run_id"] == first["run_id"]
    assert second["processed"] == 4 and second["created"] == 4 and second["existing"] == 0
    assert second["complete"] and second["run"]["status"] == "completed"
    assert second["run"]["processed"] == 6 and second["run"]["batches"] == 3
    assert second["reconciliation"]["reconciled"]


def test_failed_batch_rolls_back_and_the_rerun_resumes(corpus: dict[str, Path]) -> None:
    def crash(batches: int, _processed: int) -> None:
        if batches == 2:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated"):
        backfill.apply(**_kw(corpus, batch_size=2, on_batch=crash))
    with KnowledgeStore.open(corpus["db"]) as store:
        run = store.open_run(CORPUS_SOURCE_ID)
        assert run is not None and run["status"] == "failed" and "simulated" in run["error"]
        assert run["processed"] == 4  # the second batch committed before the hook raised
        assert store.count_records(CORPUS_SOURCE_ID) == 4
    resumed = backfill.apply(**_kw(corpus, batch_size=2))
    assert resumed["resumed"] and resumed["created"] == 2 and resumed["complete"]


def test_insert_failure_inside_a_batch_commits_nothing(
    corpus: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = KnowledgeStore._insert_records
    calls = {"n": 0}

    def flaky(self: KnowledgeStore, conn: sqlite3.Connection, source_id: str, rows: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            original(self, conn, source_id, rows[:1])  # half-written batch ...
            raise sqlite3.IntegrityError("simulated")  # ... then a failure
        return original(self, conn, source_id, rows)

    monkeypatch.setattr(KnowledgeStore, "_insert_records", flaky)
    with pytest.raises(InvalidInputError):
        backfill.apply(**_kw(corpus, batch_size=2))
    with KnowledgeStore.open(corpus["db"]) as store:
        assert store.count_records(CORPUS_SOURCE_ID) == 2  # only the first batch
        run = store.open_run(CORPUS_SOURCE_ID)
        assert run is not None and run["processed"] == 2


def test_apply_refuses_without_disk_space(
    corpus: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    class Usage:
        free = 10

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: Usage())
    assert not backfill.plan(**_kw(corpus))["disk"]["sufficient"]
    with pytest.raises(UnavailableError, match="disk"):
        backfill.apply(**_kw(corpus))
    assert not corpus["db"].exists()


@pytest.mark.parametrize("extra", [{"batch_size": 0}, {"max_batches": 0}])
def test_apply_validates_its_options(corpus: dict[str, Path], extra: dict[str, int]) -> None:
    with pytest.raises(InvalidInputError):
        backfill.apply(**_kw(corpus, **extra))


def test_missing_or_invalid_registry(corpus: dict[str, Path]) -> None:
    corpus["registry"].write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidInputError):
        backfill.plan(**_kw(corpus))
    corpus["registry"].unlink()
    with pytest.raises(UnavailableError):
        backfill.plan(**_kw(corpus))


def test_restore_from_backup(corpus: dict[str, Path]) -> None:
    backfill.apply(**_kw(corpus))
    with KnowledgeStore.open(corpus["db"]) as store:
        store.create_project("default", "Post-backup project")
    report = backfill.apply(**_kw(corpus))  # takes a backup including the project
    with KnowledgeStore.open(corpus["db"]) as store:
        store.create_project("default", "Lost after restore")
    backup_db = Path(report["backup"]["path"]) / "knowledge.db"
    for suffix in ("-wal", "-shm"):
        Path(str(corpus["db"]) + suffix).unlink(missing_ok=True)
    corpus["db"].write_bytes(backup_db.read_bytes())
    with KnowledgeStore.open(corpus["db"]) as store:
        names = [p.display_name for p in store.list_projects("default")]
        assert names == ["Post-backup project"]
        assert store.count_records(CORPUS_SOURCE_ID) == 6


# ── CLI ──────────────────────────────────────────────────────────────


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = cli.main(["knowledge", *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else None, err


def test_cli_migrate_and_status(
    corpus: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, status, _ = _cli(capsys, "status")
    assert code == 0 and status["store_exists"] is False
    code, dry, _ = _cli(capsys, "migrate", "--dry-run")
    assert code == 0 and dry["records"]["to_create"] == 6
    assert not corpus["db"].exists()
    backups = tmp_path / "my-backups"
    code, applied, _ = _cli(
        capsys,
        "migrate",
        "--apply",
        "--backup-dir",
        str(backups),
        "--batch-size",
        "4",
        "--max-batches",
        "1",
    )
    assert code == 0 and not applied["complete"]
    assert Path(applied["backup"]["path"]).parent == backups.resolve()
    code, applied, _ = _cli(capsys, "migrate", "--apply")
    assert code == 0 and applied["complete"] and applied["resumed"]
    code, status, _ = _cli(capsys, "status")
    assert code == 0 and status["store_exists"]
    assert status["counts"] == {
        "workspaces": 1,
        "projects": 0,
        "sources": 1,
        "collections": 0,
        "records": 6,
        "collection_memberships": 0,
    }
    assert status["records_by_curation_status"] == {"unclassified": 6}
    assert status["last_backfill"]["status"] == "completed"


def test_cli_migrate_requires_a_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["knowledge", "migrate"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_cli_reports_typed_errors(
    corpus: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus["registry"].unlink()
    code, _, err = _cli(capsys, "migrate", "--dry-run")
    assert code == 4 and "error[unavailable]" in err
