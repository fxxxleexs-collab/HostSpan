from __future__ import annotations

import asyncio
import sys

import pytest

from environment_runtime.core.models import InputType
from environment_runtime.core.paths import WorkspacePath
from environment_runtime.services.artifact import ArtifactService
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.interaction import InteractionService
from environment_runtime.services.security import WriterLeaseService
from environment_runtime.services.session import SessionService
from environment_runtime.services.task import TaskService
from environment_runtime.services.workspace import WorkspaceService


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_task_and_artifact_flow(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    workspace_service = WorkspaceService(runtime)
    workspace = await workspace_service.create("demo")
    workspace = await workspace_service.add_root(workspace.workspace_id, "src")

    source_root = tmp_path / "workspace"
    (source_root / "src").mkdir(parents=True)
    (source_root / "src" / "hello.txt").write_text("artifact-data", encoding="utf-8")
    workspace = await workspace_service.add_replica(
        workspace.workspace_id, endpoint.endpoint_id, str(source_root)
    )
    workspace = await workspace_service.create_revision(workspace.workspace_id, workspace.replicas[0].replica_id)

    environment = await EnvironmentService(runtime).create(
        "env", [endpoint.endpoint_id], [workspace.workspace_id]
    )
    target_id = environment.default_execution_target_id
    assert target_id is not None

    task = await TaskService(runtime).start(
        environment.environment_id,
        target_id,
        [
            sys.executable,
            "-c",
            "print('hello-from-task')",
        ],
        cwd=str(source_root),
    )
    await asyncio.sleep(1)
    task = await TaskService(runtime).get(task.task_id)
    logs = await TaskService(runtime).logs(task.task_id)

    artifact = await ArtifactService(runtime).register(
        WorkspacePath(
            workspace_id=workspace.workspace_id,
            root_id=workspace.roots[0].root_id,
            relative_path="hello.txt",
        ),
        task_id=task.task_id,
        media_type="text/plain",
    )
    downloaded = tmp_path / "downloaded.txt"
    await ArtifactService(runtime).download(artifact.artifact_id, str(downloaded))

    assert task.state == "SUCCEEDED"
    assert any("hello-from-task" in entry["chunk"] for entry in logs)
    assert downloaded.read_text(encoding="utf-8") == "artifact-data"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_input_flow(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None

    session = await SessionService(runtime).create(
        environment.environment_id,
        target_id,
        [
            sys.executable,
            "-u",
            "-c",
            "print('ready'); value=input(); print(f'got={value}')",
        ],
        cwd=str(tmp_path),
    )
    await WriterLeaseService(runtime).acquire(session.session_id, "automation", "agent-1")
    request = await InteractionService(runtime).create_request(
        session.session_id,
        InputType.TEXT,
        prompt="enter value",
    )
    await InteractionService(runtime).submit_input(request.request_id, "agent-1", "hello\n")
    await asyncio.sleep(1)
    events = await runtime.event_store.list_events()
    frames = await SessionService(runtime).terminal_frames(session.session_id)
    tail = await SessionService(runtime).terminal_tail(session.session_id)

    assert any(event.event_type == "interaction.resolved" for event in events)
    assert any(event.event_type == "session.output" and "got=hello" in str(event.payload) for event in events)
    assert any(frame.kind == "output" and "got=hello" in frame.data for frame in frames)
    assert any(frame.kind == "redacted" and frame.data == "[REDACTED_INPUT]" for frame in frames)
    assert "got=hello" in str(tail["text"])
    assert "hello\n" not in str(tail["text"])
