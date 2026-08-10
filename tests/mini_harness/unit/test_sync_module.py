from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.config import load_harness_config
from mini_harness.sync.config import SyncConfig
from mini_harness.sync.engine import SyncEngine
from mini_harness.sync.ignore import SyncIgnoreMatcher
from mini_harness.sync.manifest import FileRecord, SyncManifest, scan_local_manifest
from mini_harness.sync.planner import plan_push
from mini_harness.sync.state import SyncRunMetadata, SyncState, SyncStateStore


def test_sync_ignore_matches_default_secret_and_cache_patterns() -> None:
    matcher = SyncIgnoreMatcher()

    assert matcher.should_ignore(".env")
    assert matcher.should_ignore(".venv/pyvenv.cfg")
    assert matcher.should_ignore("src/__pycache__/module.pyc")
    assert not matcher.should_ignore("src/app.py")


def test_scan_local_manifest_hashes_text_files_and_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01")

    result = scan_local_manifest(tmp_path)

    assert set(result.manifest.files) == {"src/app.py"}
    assert result.manifest.files["src/app.py"].size == len(
        (tmp_path / "src" / "app.py").read_bytes()
    )
    assert {item.path: item.reason for item in result.skipped} == {
        ".env": "ignored",
        "image.bin": "binary",
    }


def test_plan_push_uploads_changed_files_and_keeps_unchanged() -> None:
    old = SyncManifest(
        files={
            "a.py": FileRecord(path="a.py", sha256="old", size=1, mtime_ns=1),
            "b.py": FileRecord(path="b.py", sha256="same", size=1, mtime_ns=1),
        }
    )
    current = SyncManifest(
        files={
            "a.py": FileRecord(path="a.py", sha256="new", size=2, mtime_ns=2),
            "b.py": FileRecord(path="b.py", sha256="same", size=1, mtime_ns=1),
        }
    )

    plan = plan_push(local=current, last_pushed=old)

    assert [item.path for item in plan.uploads] == ["a.py"]
    assert plan.unchanged == ["b.py"]
    assert not plan.conflicts


def test_plan_push_detects_remote_manifest_conflict() -> None:
    old = SyncManifest(files={"a.py": FileRecord(path="a.py", sha256="old", size=1, mtime_ns=1)})
    current = SyncManifest(
        files={"a.py": FileRecord(path="a.py", sha256="new", size=1, mtime_ns=2)}
    )
    remote = SyncManifest(
        files={"a.py": FileRecord(path="a.py", sha256="remote", size=1, mtime_ns=3)}
    )

    plan = plan_push(local=current, last_pushed=old, remote=remote)

    assert not plan.ok
    assert plan.conflicts[0].path == "a.py"


def test_sync_state_store_round_trips_manifest(tmp_path: Path) -> None:
    store = SyncStateStore(tmp_path)
    manifest = SyncManifest(
        files={"a.py": FileRecord(path="a.py", sha256="hash", size=1, mtime_ns=1)}
    )

    state = SyncState(
        metadata=SyncRunMetadata(workspace_id="demo", remote_root="/srv/app"),
        manifest=manifest,
    )

    path = store.save("demo", state)
    loaded = store.load("demo")

    assert path.exists()
    assert loaded is not None
    assert loaded.manifest.files["a.py"].sha256 == "hash"


def test_sync_engine_push_writes_uploads_and_manifests(tmp_path: Path, fake_runtime) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    engine = SyncEngine(
        runtime=fake_runtime,
        endpoint_id="endpoint_ssh",
        local_root=tmp_path,
        remote_root="/srv/app",
        workspace_id="demo",
    )

    result = engine.push()

    assert result.ok
    assert result.uploaded == ["src/app.py"]
    assert result.remote_manifest_path == "/srv/app/.mini-harness/sync-manifest.json"
    assert (
        "ensure_dir",
        {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src"},
    ) in fake_runtime.requests
    assert (
        "write_text",
        {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src/app.py"},
    ) in fake_runtime.requests


def test_load_config_supports_sync(tmp_path: Path) -> None:
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[sync]
enabled = true
mode = "push"
delete_remote = true
max_file_bytes = 4096
text_only = true
remote_manifest_path = ".mini-harness/remote-manifest.json"

[sync.ignore]
patterns = [".git/**", "dist/**"]
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.sync.enabled is True
    assert config.sync.delete_remote is True
    assert config.sync.max_file_bytes == 4096
    assert config.sync.remote_manifest_path == ".mini-harness/remote-manifest.json"
    assert config.sync.ignore.patterns == [".git/**", "dist/**"]


def test_sync_config_rejects_parent_manifest_path() -> None:
    with pytest.raises(ValueError):
        SyncConfig(remote_manifest_path="../manifest.json")
