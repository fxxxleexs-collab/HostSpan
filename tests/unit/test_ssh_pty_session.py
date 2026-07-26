from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from environment_runtime.core.models import Endpoint, Environment, ExecutionTarget
from environment_runtime.providers.session.base import SessionCreateParams, TerminalSize
from environment_runtime.providers.session.ssh_pty import SSHPTYSessionProvider
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.session import SessionService


class FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream([b"hello from pty\r\n"])
        self.stderr = None
        self.returncode: int | None = None
        self.resize: tuple[int, int] | None = None
        self.terminated = False
        self.killed = False
        self.closed = False

    def change_terminal_size(self, cols: int, rows: int) -> None:
        self.resize = (cols, rows)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137

    def close(self) -> None:
        self.closed = True

    async def wait(self, check: bool = False, timeout: float | None = None):
        _ = (check, timeout)
        if self.returncode is None:
            self.returncode = 0
        return SimpleNamespace(exit_status=self.returncode)


class FakeConnection:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.command: str | None = None
        self.kwargs: dict | None = None

    async def create_process(self, command: str, **kwargs):
        self.command = command
        self.kwargs = kwargs
        return self.process


class FakeTransport:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def connect(self, endpoint: Endpoint) -> FakeConnection:
        _ = endpoint
        return self.connection


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_pty_provider_creates_interactive_process() -> None:
    connection = FakeConnection()
    provider = SSHPTYSessionProvider(FakeTransport(connection))  # type: ignore[arg-type]
    endpoint = Endpoint(
        name="ssh",
        provider_type="ssh",
        config={"hostname": "127.0.0.1"},
    )
    environment = Environment(name="env")
    target = ExecutionTarget(endpoint_id=endpoint.endpoint_id, provider="ssh_process")
    output: list[tuple[str, str]] = []

    async def on_output(stream: str, chunk: str) -> None:
        output.append((stream, chunk))

    handle = await provider.create(
        SessionCreateParams(
            environment=environment,
            target=target,
            endpoint=endpoint,
            argv=["python3", "-i"],
            cwd="/tmp/runtime harness",
            env={"TOKEN": "abc 123"},
            terminal_size=TerminalSize(cols=100, rows=40),
            term_type="xterm-test",
        ),
        on_output=on_output,
    )

    await asyncio.sleep(0)
    await handle.write("print('ok')\n")
    await handle.resize(120, 50)
    exit_code = await handle.wait()
    await handle.close()

    assert connection.command == "cd '/tmp/runtime harness' && exec env 'TOKEN=abc 123' python3 -i"
    assert connection.kwargs == {
        "request_pty": True,
        "term_type": "xterm-test",
        "term_size": (100, 40),
        "encoding": None,
    }
    assert output == [("pty", "hello from pty\r\n")]
    assert connection.process.stdin.data == b"print('ok')\r"
    assert connection.process.resize == (120, 50)
    assert exit_code == 0
    assert handle.backend_ref()["backend"] == "ssh_pty"
    assert handle.backend_ref()["endpoint_id"] == endpoint.endpoint_id


class FakeSessionHandle:
    backend_name = "ssh_pty"

    def backend_ref(self) -> dict[str, object]:
        return {"backend": "ssh_pty", "endpoint_id": "fake-endpoint"}

    async def write(self, data: str) -> None:
        _ = data

    async def resize(self, cols: int, rows: int) -> None:
        _ = (cols, rows)

    async def wait(self) -> int | None:
        return 0

    async def detach(self) -> None:
        return None

    async def terminate(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeSessionProvider:
    backend_name = "ssh_pty"

    def __init__(self) -> None:
        self.params: SessionCreateParams | None = None

    async def create(self, params: SessionCreateParams, on_output):
        self.params = params
        await on_output("pty", "remote-ready\r\n")
        return FakeSessionHandle()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_service_routes_ssh_endpoint_to_ssh_pty(runtime) -> None:
    endpoint = await EndpointService(runtime).add_ssh(
        name="ssh",
        hostname="127.0.0.1",
        username="envrt",
        known_hosts_file="known_hosts",
        port=2222,
        identity_file="key",
        use_ssh_agent=False,
    )
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    provider = FakeSessionProvider()
    runtime.providers.session["ssh_pty"] = provider

    session = await SessionService(runtime).create(
        environment.environment_id,
        target_id,
        ["bash", "-l"],
        cols=90,
        rows=25,
    )
    await asyncio.sleep(0)
    logs = await runtime.event_store.list_events()

    assert session.backend == "ssh_pty"
    assert session.backend_ref["backend"] == "ssh_pty"
    assert provider.params is not None
    assert provider.params.terminal_size.cols == 90
    assert provider.params.terminal_size.rows == 25
    assert any(event.event_type == "session.output" and "remote-ready" in str(event.payload) for event in logs)
