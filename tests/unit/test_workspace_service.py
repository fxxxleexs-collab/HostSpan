from __future__ import annotations

import pytest

from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.workspace import WorkspaceService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_workspace_revision_changes_with_file_content(runtime, tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("one", encoding="utf-8")

    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    workspace = await WorkspaceService(runtime).create("demo")
    workspace = await WorkspaceService(runtime).add_root(workspace.workspace_id, "src")
    workspace = await WorkspaceService(runtime).add_replica(
        workspace.workspace_id, endpoint.endpoint_id, str(source)
    )
    replica_id = workspace.replicas[0].replica_id

    workspace = await WorkspaceService(runtime).create_revision(workspace.workspace_id, replica_id)
    first = workspace.current_revision

    (source / "file.txt").write_text("two", encoding="utf-8")
    workspace = await WorkspaceService(runtime).create_revision(workspace.workspace_id, replica_id)
    second = workspace.current_revision

    assert first is not None
    assert second is not None
    assert first != second
