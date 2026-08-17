from __future__ import annotations

from mini_harness.file_ops import (
    LocalDiskWorkspaceFileOps,
    RuntimeWorkspaceFileOps,
    parent_directory,
)
from mini_harness.runtime.work_context import WorkContext


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
    )


def test_parent_directory_returns_normalized_parent() -> None:
    assert parent_directory("app.py") is None
    assert parent_directory("src/app.py") == "src"
    assert parent_directory("src/pkg/app.py") == "src/pkg"
    assert parent_directory(r"src\pkg\app.py") == "src/pkg"


def test_runtime_file_ops_reads_with_location_metadata(fake_runtime) -> None:
    ops = RuntimeWorkspaceFileOps(fake_runtime, _context())

    result = ops.read_text("calculator.py")

    assert "return a - b" in result.text
    assert result.location.path == "calculator.py"
    assert result.location.runtime_path == "calculator.py"
    assert result.location.target == "local"
    assert result.location.backend == "runtime"
    assert fake_runtime.requests[-1] == (
        "read_text",
        {"endpoint_id": "endpoint_1", "path": "calculator.py"},
    )


def test_runtime_file_ops_writes_and_ensures_parent(fake_runtime) -> None:
    ops = RuntimeWorkspaceFileOps(fake_runtime, _context())

    result = ops.write_text("src/app.py", "print('ok')\n")

    assert result.size == len(b"print('ok')\n")
    assert result.location.path == "src/app.py"
    assert result.parent_directory.path == "src"
    assert result.parent_directory.runtime_path == "src"
    assert result.parent_directory.ensured is True
    assert fake_runtime.requests[-2:] == [
        ("ensure_dir", {"endpoint_id": "endpoint_1", "path": "src"}),
        ("write_text", {"endpoint_id": "endpoint_1", "path": "src/app.py"}),
    ]


def test_runtime_file_ops_maps_remote_paths(fake_runtime) -> None:
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )
    ops = RuntimeWorkspaceFileOps(fake_runtime, context)

    result = ops.write_text("src/app.py", "print('ok')\n")

    assert result.location.path == "src/app.py"
    assert result.location.runtime_path == "/srv/app/src/app.py"
    assert result.location.target == "remote"
    assert result.parent_directory.runtime_path == "/srv/app/src"
    assert fake_runtime.requests[-2:] == [
        ("ensure_dir", {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src"}),
        ("write_text", {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src/app.py"}),
    ]


def test_runtime_file_ops_can_target_local_binding_in_ssh_mode(fake_runtime) -> None:
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
    )
    ops = RuntimeWorkspaceFileOps(fake_runtime, context, target="local")

    result = ops.write_text("src/app.py", "print('ok')\n")

    assert result.location.target == "local"
    assert result.location.endpoint_id == "endpoint_1"
    assert result.location.runtime_path == "src/app.py"
    assert fake_runtime.requests[-2:] == [
        ("ensure_dir", {"endpoint_id": "endpoint_1", "path": "src"}),
        ("write_text", {"endpoint_id": "endpoint_1", "path": "src/app.py"}),
    ]


def test_local_disk_file_ops_write_updates_project_files(tmp_path) -> None:
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
    )
    ops = LocalDiskWorkspaceFileOps(context)

    result = ops.write_text("src/app.py", "print('ok')\n")

    assert result.location.backend == "local-disk"
    assert result.location.runtime_path == str(tmp_path / "src" / "app.py")
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
