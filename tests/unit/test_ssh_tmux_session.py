from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from environment_runtime.core.models import (
    Endpoint,
    Environment,
    ExecutionTarget,
    InteractionState,
    Session,
    SessionState,
    TerminalFrameKind,
)
from environment_runtime.providers.session.base import SessionCreateParams, TerminalSize
from environment_runtime.providers.session.ssh_tmux import SSHTmuxSessionProvider
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.recovery import RecoveryService


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool]] = []
        self.tmux_alive = True

    async def run(self, command: str, check: bool = False):
        self.commands.append((command, check))
        if "tmux has-session" in command:
            return SimpleNamespace(exit_status=0 if self.tmux_alive else 1, stdout="")
        return SimpleNamespace(exit_status=0, stdout="")


class FakeTransport:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def connect(self, endpoint: Endpoint) -> FakeConnection:
        _ = endpoint
        return self.connection


class FakeSFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: list[str] = []

    async def ensure_dir(self, endpoint: Endpoint, path: str) -> None:
        _ = endpoint
        self.dirs.append(path)

    async def write_bytes(self, endpoint: Endpoint, path: str, data: bytes) -> None:
        _ = endpoint
        self.files[path] = data

    async def read_bytes(self, endpoint: Endpoint, path: str) -> bytes:
        _ = endpoint
        return self.files[path]

    async def exists(self, endpoint: Endpoint, path: str) -> bool:
        _ = endpoint
        return path in self.files

    async def remove(self, endpoint: Endpoint, path: str) -> None:
        _ = endpoint
        if path not in self.files:
            raise OSError(path)
        self.files.pop(path)


class DelayedStatusSFTP(FakeSFTP):
    def __init__(self, delayed_path: str, delayed_data: bytes) -> None:
        super().__init__()
        self.delayed_path = delayed_path
        self.delayed_data = delayed_data
        self.exists_count = 0

    async def exists(self, endpoint: Endpoint, path: str) -> bool:
        if path == self.delayed_path:
            self.exists_count += 1
            if self.exists_count >= 2:
                self.files[path] = self.delayed_data
        return await super().exists(endpoint, path)


@pytest.fixture
def ssh_endpoint() -> Endpoint:
    return Endpoint(
        name="ssh",
        provider_type="ssh",
        config={"hostname": "127.0.0.1", "remote_runtime_dir": ".envrt"},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_tmux_provider_creates_tmux_session_and_controls_it(
    ssh_endpoint: Endpoint,
) -> None:
    connection = FakeConnection()
    sftp = FakeSFTP()
    provider = SSHTmuxSessionProvider(  # type: ignore[arg-type]
        FakeTransport(connection),
        sftp,
        poll_interval=0.01,
    )
    output: list[tuple[str, str]] = []

    async def on_output(stream: str, chunk: str) -> None:
        output.append((stream, chunk))

    handle = await provider.create(
        SessionCreateParams(
            session_id="session_abc",
            environment=Environment(name="env"),
            target=ExecutionTarget(endpoint_id=ssh_endpoint.endpoint_id, provider="ssh_process"),
            endpoint=ssh_endpoint,
            argv=["bash", "-l"],
            cwd="/tmp/runtime harness",
            env={"TOKEN": "abc 123"},
            terminal_size=TerminalSize(cols=90, rows=24),
            term_type="xterm-test",
        ),
        on_output=on_output,
    )

    await handle.write("echo ready\n")
    await handle.resize(120, 40)
    await handle.close()

    commands = [command for command, _ in connection.commands]
    assert any("command -v tmux" in command for command in commands)
    assert any("tmux new-session" in command and "-x 90 -y 24" in command for command in commands)
    assert any("tmux pipe-pane" in command and "terminal.log" in command for command in commands)
    assert any("tmux set-buffer" in command and "echo ready" in command for command in commands)
    assert any("tmux paste-buffer" in command for command in commands)
    assert any("tmux resize-window" in command and "-x 120 -y 40" in command for command in commands)
    assert handle.backend_ref()["backend"] == "ssh_tmux"
    assert handle.backend_ref()["tmux_session"] == "envrt_session_abc"
    assert sftp.files[".envrt/sessions/envrt_session_abc/terminal.log"] == b""
    assert output == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_tmux_attach_tails_from_resume_offset(ssh_endpoint: Endpoint) -> None:
    connection = FakeConnection()
    sftp = FakeSFTP()
    provider = SSHTmuxSessionProvider(  # type: ignore[arg-type]
        FakeTransport(connection),
        sftp,
        poll_interval=0.01,
    )
    sftp.files[".envrt/sessions/envrt_session_abc/terminal.log"] = b"hello world"
    session = Session(
        session_id="session_abc",
        environment_id="env",
        target_id="target",
        backend="ssh_tmux",
        command=["bash", "-l"],
        backend_ref={
            "tmux_session": "envrt_session_abc",
            "tmux_target": "envrt_session_abc:0.0",
            "remote_log_file": ".envrt/sessions/envrt_session_abc/terminal.log",
            "remote_status_file": ".envrt/sessions/envrt_session_abc/status.json",
        },
        state=SessionState.ACTIVE,
    )
    output: list[tuple[str, str]] = []

    async def on_output(stream: str, chunk: str) -> None:
        output.append((stream, chunk))

    handle = await provider.attach(session, ssh_endpoint, on_output, initial_output_offset=6)
    await asyncio.sleep(0.03)
    await handle.close()

    assert output == [("pty", "world")]
    assert any("tmux has-session" in command for command, _ in connection.commands)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_tmux_status_restores_exit_code(ssh_endpoint: Endpoint) -> None:
    connection = FakeConnection()
    sftp = FakeSFTP()
    provider = SSHTmuxSessionProvider(  # type: ignore[arg-type]
        FakeTransport(connection),
        sftp,
    )
    sftp.files[".envrt/sessions/envrt_session_done/status.json"] = (
        b'{"exit_code":7,"finished_at":"2026-07-27T00:00:00Z"}'
    )
    session = Session(
        session_id="session_done",
        environment_id="env",
        target_id="target",
        backend="ssh_tmux",
        command=["bash", "-l"],
        backend_ref={
            "tmux_session": "envrt_session_done",
            "remote_status_file": ".envrt/sessions/envrt_session_done/status.json",
        },
        state=SessionState.ACTIVE,
    )

    status = await provider.status(session, ssh_endpoint)

    assert status.alive is False
    assert status.exit_code == 7
    assert status.finished is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_tmux_status_waits_briefly_for_delayed_status_file(
    ssh_endpoint: Endpoint,
) -> None:
    connection = FakeConnection()
    connection.tmux_alive = False
    status_file = ".envrt/sessions/envrt_session_race/status.json"
    sftp = DelayedStatusSFTP(
        status_file,
        b'{"exit_code":0,"finished_at":"2026-07-27T00:00:00Z"}',
    )
    provider = SSHTmuxSessionProvider(  # type: ignore[arg-type]
        FakeTransport(connection),
        sftp,
    )
    session = Session(
        session_id="session_race",
        environment_id="env",
        target_id="target",
        backend="ssh_tmux",
        command=["bash", "-l"],
        backend_ref={
            "tmux_session": "envrt_session_race",
            "remote_status_file": status_file,
        },
        state=SessionState.ACTIVE,
    )

    status = await provider.status(session, ssh_endpoint)

    assert status.alive is False
    assert status.finished is True
    assert status.exit_code == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recovery_marks_unattachable_session_disconnected(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    session = Session(
        environment_id=environment.environment_id,
        target_id=target_id,
        backend="local_pty",
        command=["python", "-i"],
        state=SessionState.ACTIVE,
        interaction_state=InteractionState.AUTOMATION_CONTROLLED,
    )
    await runtime.sessions.upsert(session)

    result = await RecoveryService(runtime).reconcile_on_startup()
    recovered = await runtime.sessions.get(session.session_id)

    assert result["sessions"] == 1
    assert recovered is not None
    assert recovered.state == SessionState.DISCONNECTED
    assert recovered.interaction_state == InteractionState.NONE


class FakeDurableHandle:
    backend_name = "durable_fake"

    def __init__(self) -> None:
        self.waiter: asyncio.Future[int | None] = asyncio.get_event_loop().create_future()

    def backend_ref(self) -> dict[str, object]:
        return {"backend": "durable_fake"}

    async def write(self, data: str) -> None:
        _ = data

    async def resize(self, cols: int, rows: int) -> None:
        _ = (cols, rows)

    async def wait(self) -> int | None:
        return await self.waiter

    async def detach(self) -> None:
        return None

    async def terminate(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeDurableProvider:
    backend_name = "durable_fake"

    def __init__(self) -> None:
        self.initial_output_offset: int | None = None
        self.handle = FakeDurableHandle()

    async def create(self, params, on_output):
        _ = (params, on_output)
        return self.handle

    async def status(self, session: Session, endpoint: Endpoint):
        _ = (session, endpoint)
        return SimpleNamespace(alive=True, exit_code=None)

    async def attach(
        self,
        session: Session,
        endpoint: Endpoint,
        on_output,
        initial_output_offset: int = 0,
    ):
        _ = (session, endpoint, on_output)
        self.initial_output_offset = initial_output_offset
        return self.handle


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recovery_reattaches_durable_session(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    provider = FakeDurableProvider()
    runtime.providers.session["durable_fake"] = provider
    session = Session(
        environment_id=environment.environment_id,
        target_id=target_id,
        backend="durable_fake",
        command=["bash", "-l"],
        state=SessionState.ACTIVE,
    )
    await runtime.sessions.upsert(session)
    await runtime.terminal_frames.append(session.session_id, kind="output", data="already")

    result = await RecoveryService(runtime).reconcile_on_startup()
    recovered = await runtime.sessions.get(session.session_id)

    assert result["sessions"] == 1
    assert recovered is not None
    assert recovered.state == SessionState.ACTIVE
    assert session.session_id in runtime.active.session_handles
    assert provider.initial_output_offset == len("already")


class FakeFinishedProvider:
    backend_name = "finished_fake"

    async def create(self, params, on_output):
        _ = (params, on_output)
        raise AssertionError("create should not be called during recovery")

    async def status(self, session: Session, endpoint: Endpoint):
        _ = (session, endpoint)
        return SimpleNamespace(alive=False, exit_code=None, finished=True)

    async def attach(
        self,
        session: Session,
        endpoint: Endpoint,
        on_output,
        initial_output_offset: int = 0,
    ):
        _ = (session, endpoint, on_output, initial_output_offset)
        raise AssertionError("attach should not be called for finished sessions")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recovery_finalizes_finished_durable_session_without_exit_code(
    runtime,
    tmp_path,
) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    runtime.providers.session["finished_fake"] = FakeFinishedProvider()
    session = Session(
        environment_id=environment.environment_id,
        target_id=target_id,
        backend="finished_fake",
        command=["bash", "-l"],
        state=SessionState.ACTIVE,
        interaction_state=InteractionState.AUTOMATION_CONTROLLED,
    )
    await runtime.sessions.upsert(session)

    result = await RecoveryService(runtime).reconcile_on_startup()
    recovered = await runtime.sessions.get(session.session_id)

    assert result["sessions"] == 1
    assert recovered is not None
    assert recovered.state == SessionState.TERMINATED
    assert recovered.exit_code is None
    assert recovered.interaction_state == InteractionState.NONE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_output_resume_offset_counts_only_output_frames(runtime) -> None:
    session_id = "session_resume"
    await runtime.terminal_frames.append(session_id, TerminalFrameKind.OUTPUT, "abc")
    await runtime.terminal_frames.append(session_id, TerminalFrameKind.REDACTED, "[REDACTED_INPUT]")
    await runtime.terminal_frames.append(session_id, TerminalFrameKind.RESIZE, "120x30")
    await runtime.terminal_frames.append(session_id, TerminalFrameKind.OUTPUT, "defg")

    offset = await runtime.terminal_frames.output_resume_offset(session_id)

    assert offset == len("abcdefg")
