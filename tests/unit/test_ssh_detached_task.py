from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from environment_runtime.core.commands import CommandSpec
from environment_runtime.core.models import Endpoint, Task, TaskState
from environment_runtime.providers.execution.ssh_detached import (
    SSHDetachedExecutionProvider,
    _launcher_bytes,
    _sh_launcher_bytes,
)
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


class FakeRunResult:
    def __init__(self, stdout: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.exit_status = exit_status


class FakeSSHConnection:
    def __init__(self, probe_stdout: str) -> None:
        self.probe_stdout = probe_stdout
        self.commands: list[tuple[str, bool]] = []

    async def run(self, command: str, check: bool = False) -> FakeRunResult:
        self.commands.append((command, check))
        if "ENVRT_LAUNCHER_PROBE_BEGIN" in command:
            return FakeRunResult(self.probe_stdout)
        if command.startswith("kill -0 "):
            return FakeRunResult("", 0)
        return FakeRunResult("4242\n")


class FakeSSHTransport:
    def __init__(self, probe_stdout: str) -> None:
        self.connection = FakeSSHConnection(probe_stdout)

    async def connect(self, endpoint: Endpoint) -> FakeSSHConnection:
        _ = endpoint
        return self.connection


class FakeSFTPProvider:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    async def write_bytes(self, endpoint: Endpoint, path: str, data: bytes) -> None:
        _ = endpoint
        self.writes.append((path, data))

    async def exists(self, endpoint: Endpoint, path: str) -> bool:
        _ = endpoint, path
        return False

    async def read_bytes(self, endpoint: Endpoint, path: str) -> bytes:
        _ = endpoint, path
        return b""


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


def test_ssh_detached_launcher_resource_is_readable() -> None:
    data = _launcher_bytes()

    assert b"Bundled launcher for detached persistent tasks" in data
    assert b"from datetime import UTC" not in data
    assert b"timezone.utc" in data


def test_ssh_detached_sh_launcher_resource_is_readable() -> None:
    data = _sh_launcher_bytes()

    assert b"POSIX fallback launcher for detached SSH tasks" in data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_detached_falls_back_to_sh_launcher_when_python_is_missing() -> None:
    endpoint = Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": "known_hosts",
        },
    )
    transport = FakeSSHTransport(
        "\n".join(
            [
                "ENVRT_LAUNCHER_PROBE_BEGIN",
                "nohup_path=/usr/bin/nohup",
                "sh_path=/bin/sh",
                "python_command=",
                "ENVRT_LAUNCHER_PROBE_END",
            ]
        )
    )
    sftp = FakeSFTPProvider()
    provider = SSHDetachedExecutionProvider(transport=transport, sftp=sftp)

    handle = await provider.start(
        command=CommandSpec(argv=["echo", "hi"]),
        cwd="/srv/app",
        env={"DEMO": "1"},
        on_output=_ignore_output,
        task_id="task_123",
        endpoint=endpoint,
    )
    await handle.close()

    assert handle.launcher_kind == "sh"
    assert handle.remote_launcher_file == ".environment-runtime/bin/_launcher.sh"
    assert sftp.writes[0][0] == ".environment-runtime/bin/_launcher.sh"
    start_command = transport.connection.commands[1][0]
    assert "nohup sh .environment-runtime/bin/_launcher.sh" in start_command
    assert "--env DEMO=1" in start_command


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_detached_python_probe_requires_python_38_or_newer() -> None:
    endpoint = Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": "known_hosts",
        },
    )
    transport = FakeSSHTransport(
        "\n".join(
            [
                "ENVRT_LAUNCHER_PROBE_BEGIN",
                "nohup_path=/usr/bin/nohup",
                "sh_path=/bin/sh",
                "python_command=",
                "ENVRT_LAUNCHER_PROBE_END",
            ]
        )
    )
    provider = SSHDetachedExecutionProvider(transport=transport, sftp=FakeSFTPProvider())

    await provider.start(
        command=CommandSpec(argv=["echo", "hi"]),
        cwd=None,
        env={},
        on_output=_ignore_output,
        task_id="task_123",
        endpoint=endpoint,
    )

    probe_command = transport.connection.commands[0][0]
    assert "sys.version_info >= (3, 8)" in probe_command


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_detached_prefers_python_launcher_when_available() -> None:
    endpoint = Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": "known_hosts",
        },
    )
    transport = FakeSSHTransport(
        "\n".join(
            [
                "ENVRT_LAUNCHER_PROBE_BEGIN",
                "nohup_path=/usr/bin/nohup",
                "sh_path=/bin/sh",
                "python_command=python3",
                "ENVRT_LAUNCHER_PROBE_END",
            ]
        )
    )
    sftp = FakeSFTPProvider()
    provider = SSHDetachedExecutionProvider(transport=transport, sftp=sftp)

    handle = await provider.start(
        command=CommandSpec(argv=["echo", "hi"]),
        cwd=None,
        env={},
        on_output=_ignore_output,
        task_id="task_123",
        endpoint=endpoint,
    )
    await handle.close()

    assert handle.launcher_kind == "python"
    assert handle.remote_launcher_file == ".environment-runtime/bin/_launcher.py"
    assert sftp.writes[0][0] == ".environment-runtime/bin/_launcher.py"
    start_command = transport.connection.commands[1][0]
    assert "nohup python3 .environment-runtime/bin/_launcher.py" in start_command


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


async def _ignore_output(stream: str, chunk: str) -> None:
    _ = stream, chunk
