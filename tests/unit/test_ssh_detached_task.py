from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from environment_runtime.core.commands import CommandSpec
from environment_runtime.core.models import Task, TaskState
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.task import TaskService


class FakeSSHDetachedHandle:
    remote_pid = 4242
    remote_log_file = ".environment-runtime/logs/task.log"
    remote_status_file = ".environment-runtime/status/task.status"
    started_at = datetime.now(UTC)

    async def wait(self) -> int:
        return 0

    async def close(self) -> None:
        return None


class FakeSSHDetachedProvider:
    def __init__(self) -> None:
        self.started_with_endpoint_id: str | None = None
        self.reattached_with_endpoint_id: str | None = None

    async def start(self, command, cwd, env, on_output, task_id, endpoint):
        self.started_with_endpoint_id = endpoint.endpoint_id
        await on_output("stdout", "remote-started\n")
        return FakeSSHDetachedHandle()

    async def reattach(self, task, endpoint, on_output, resume_offset):
        self.reattached_with_endpoint_id = endpoint.endpoint_id
        await on_output("stdout", "remote-recovered\n")
        return SimpleNamespace(finished=True, alive=False, exit_code=0, finished_at=datetime.now(UTC))

    def _paths(self, task_id, endpoint):
        return {
            "launcher": ".environment-runtime/bin/_launcher.py",
            "log": ".environment-runtime/logs/task.log",
            "status": ".environment-runtime/status/task.status",
        }


@pytest.fixture
async def ssh_environment(runtime, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    endpoint = await EndpointService(runtime).add_ssh(
        name="ssh-demo",
        hostname="example.test",
        username="envrt",
        known_hosts_file=str(known_hosts),
    )
    environment = await EnvironmentService(runtime).create("ssh-env", [endpoint.endpoint_id])
    return endpoint, environment


@pytest.mark.unit
@pytest.mark.asyncio
async def test_persistent_ssh_task_routes_to_ssh_detached(runtime, ssh_environment) -> None:
    endpoint, environment = ssh_environment
    provider = FakeSSHDetachedProvider()
    runtime.providers.execution["ssh_detached"] = provider
    target_id = environment.default_execution_target_id
    assert target_id is not None

    task = await TaskService(runtime).start(
        environment.environment_id,
        target_id,
        ["python3", "-c", "print('hi')"],
        persistent=True,
    )

    assert provider.started_with_endpoint_id == endpoint.endpoint_id
    assert task.backend_ref is not None
    assert task.backend_ref["backend"] == "ssh_detached"
    assert task.backend_ref["endpoint_id"] == endpoint.endpoint_id
    assert task.state == TaskState.RUNNING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reattach_on_startup_supports_ssh_detached(runtime, ssh_environment) -> None:
    endpoint, environment = ssh_environment
    provider = FakeSSHDetachedProvider()
    runtime.providers.execution["ssh_detached"] = provider
    target_id = environment.default_execution_target_id
    assert target_id is not None
    task = Task(
        environment_id=environment.environment_id,
        target_id=target_id,
        command=CommandSpec(argv=["python3", "-c", "print('hi')"]),
        persistent=True,
        state=TaskState.RUNNING,
        backend_ref={
            "backend": "ssh_detached",
            "endpoint_id": endpoint.endpoint_id,
            "remote_pid": 4242,
            "remote_log_file": ".environment-runtime/logs/task.log",
            "remote_status_file": ".environment-runtime/status/task.status",
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    await runtime.tasks.upsert(task)

    reclaimed = await TaskService(runtime).reattach_on_startup(task)
    recovered = await TaskService(runtime).get(task.task_id)

    assert reclaimed is True
    assert provider.reattached_with_endpoint_id == endpoint.endpoint_id
    assert recovered.state == TaskState.SUCCEEDED
    assert recovered.exit_code == 0
