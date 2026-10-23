"""Registry JSON persistence: atomic replacement keeps registry.json's permission bits."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from knowledge_engine.core.registry import Registry, RegistryEntry


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _umask_default_mode(directory: Path) -> int:
    """What a plain ``open()`` gives a new file here (0o666 masked by the umask)."""
    probe = directory / "probe.txt"
    probe.write_text("", encoding="utf-8")
    try:
        return _mode(probe)
    finally:
        probe.unlink()


def _entry(entry_id: str) -> RegistryEntry:
    return RegistryEntry(id=entry_id, kind="kit", name=entry_id, path=f"kits/{entry_id}")


def test_fresh_registry_json_gets_the_umask_default_mode_not_0600(tmp_path: Path) -> None:
    registry_path = tmp_path / "corpus" / "registry.json"
    reg = Registry(registry_path, tmp_path / "registry.db")
    try:
        assert registry_path.exists()
        # Same bits a plain open() would give (0o644 under the usual 022 umask),
        # not the private 0600 of a mkstemp temp file.
        assert _mode(registry_path) == _umask_default_mode(tmp_path)
    finally:
        reg.close()


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not representable on Windows"
)
@pytest.mark.parametrize("mode", [0o640, 0o664, 0o600])
def test_persist_preserves_an_existing_registry_json_mode(tmp_path: Path, mode: int) -> None:
    registry_path = tmp_path / "registry.json"
    reg = Registry(registry_path, tmp_path / "registry.db")
    try:
        registry_path.chmod(mode)
        assert _mode(registry_path) == mode

        reg.upsert(_entry("kit-a"))
        assert _mode(registry_path) == mode, "upsert changed the file mode"

        reg.set_enabled("kit-a", False)
        assert _mode(registry_path) == mode, "set_enabled changed the file mode"

        reg.set_lifecycle(watch_enabled=False)
        assert _mode(registry_path) == mode, "set_lifecycle changed the file mode"

        removed = reg.remove("kit-a")
        assert removed is True
        assert _mode(registry_path) == mode, "remove changed the file mode"
    finally:
        reg.close()
    assert list(tmp_path.glob(".registry.json.*.tmp")) == []


def test_persist_is_atomic_and_leaves_no_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    reg = Registry(registry_path, tmp_path / "registry.db")
    try:
        reg.upsert(_entry("kit-a"))
        before = registry_path.read_text(encoding="utf-8")

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            reg.upsert(_entry("kit-b"))
    finally:
        reg.close()
    assert registry_path.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob(".registry.json.*.tmp")) == []
