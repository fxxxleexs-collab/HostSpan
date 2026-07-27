from __future__ import annotations

import asyncio
import contextlib
import shlex
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from environment_runtime.core.errors import ProviderError, ValidationError
from environment_runtime.core.models import Session
from environment_runtime.providers.session.base import (
    SessionBackendStatus,
    SessionCreateParams,
    SessionOutputCallback,
)
from environment_runtime.providers.transport.ssh import SSHTransportProvider


@dataclass
class SSHPTYSessionHandle:
    process: Any
    endpoint_id: str
    command: str
    backend_name: str = "ssh_pty"
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _finished: bool = False
    _exit_code: int | None = None

    def backend_ref(self) -> dict[str, object]:
        return {
            "backend": self.backend_name,
            "endpoint_id": self.endpoint_id,
            "command": self.command,
        }

    async def write(self, data: str) -> None:
        writer = getattr(self.process, "stdin", None)
        if writer is None:
            return
        payload = _to_pty_input(data).encode("utf-8")
        writer.write(payload)
        drain = getattr(writer, "drain", None)
        if drain is not None:
            result = drain()
            if asyncio.iscoroutine(result):
                await result

    async def resize(self, cols: int, rows: int) -> None:
        self.process.change_terminal_size(cols, rows)

    async def detach(self) -> None:
        # A plain SSH PTY is tied to this SSH channel. Durable detach is the
        # job of a future tmux/screen-backed provider.
        await self.terminate()

    async def terminate(self) -> None:
        if self._is_finished():
            await self.close()
            return
        self.process.terminate()
        try:
            await self.process.wait(timeout=5)
        except (TimeoutError, asyncssh.Error):
            with contextlib.suppress(asyncssh.Error, OSError):
                self.process.kill()
            with contextlib.suppress(asyncssh.Error, OSError, TimeoutError):
                await self.process.wait(timeout=5)
        await self.close()

    async def wait(self) -> int | None:
        try:
            result = await self.process.wait()
        except (OSError, asyncssh.Error):
            self._finished = True
            return None
        exit_status = getattr(result, "exit_status", None)
        self._finished = True
        self._exit_code = int(exit_status) if isinstance(exit_status, int) else None
        return self._exit_code

    async def close(self) -> None:
        if self._is_finished():
            for task in self.reader_tasks:
                await task
            return
        for task in self.reader_tasks:
            if not task.done():
                task.cancel()
        for task in self.reader_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        close = getattr(self.process, "close", None)
        if close is not None:
            close()

    def _is_finished(self) -> bool:
        return self._finished or getattr(self.process, "returncode", None) is not None


class SSHPTYSessionProvider:
    backend_name = "ssh_pty"

    def __init__(self, transport: SSHTransportProvider) -> None:
        self._transport = transport

    async def create(
        self,
        params: SessionCreateParams,
        on_output: SessionOutputCallback,
    ) -> SSHPTYSessionHandle:
        if params.endpoint.provider_type != "ssh":
            raise ValidationError(f"endpoint {params.endpoint.endpoint_id} is not an SSH endpoint")
        connection = await self._transport.connect(params.endpoint)
        command = _shell_command(params)
        try:
            process = await connection.create_process(
                command,
                request_pty=True,
                term_type=params.term_type,
                term_size=(params.terminal_size.cols, params.terminal_size.rows),
                encoding=None,
            )
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"failed to create SSH PTY session: {exc}") from exc

        handle = SSHPTYSessionHandle(
            process=process,
            endpoint_id=params.endpoint.endpoint_id,
            command=command,
        )
        handle.reader_tasks.extend(_stream_tasks("pty", process.stdout, on_output))
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            handle.reader_tasks.extend(_stream_tasks("stderr", stderr, on_output))
        return handle

    async def attach(
        self,
        session: Session,
        endpoint,
        on_output: SessionOutputCallback,
        initial_output_offset: int = 0,
    ) -> SSHPTYSessionHandle:
        _ = (session, endpoint, on_output, initial_output_offset)
        raise ProviderError("ssh_pty sessions cannot be attached after runtime restart")

    async def status(self, session: Session, endpoint) -> SessionBackendStatus:
        _ = (session, endpoint)
        return SessionBackendStatus(alive=False, detail="ssh_pty status requires an active handle")


def _stream_tasks(
    stream_name: str,
    stream: Any,
    on_output: SessionOutputCallback,
) -> list[asyncio.Task[None]]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    return [
        asyncio.create_task(_read_stream(stream, queue)),
        asyncio.create_task(_dispatch_stream(stream_name, queue, on_output)),
    ]


async def _read_stream(stream: Any, queue: asyncio.Queue[str | None]) -> None:
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            await queue.put(_decode(chunk))
    finally:
        await queue.put(None)


async def _dispatch_stream(
    stream_name: str,
    queue: asyncio.Queue[str | None],
    on_output: SessionOutputCallback,
) -> None:
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        await on_output(stream_name, chunk)


def _shell_command(params: SessionCreateParams) -> str:
    command = " ".join(shlex.quote(item) for item in params.argv)
    env = params.env or {}
    if env:
        assignments = " ".join(shlex.quote(f"{key}={value}") for key, value in env.items())
        command = f"env {assignments} {command}"
    command = f"exec {command}"
    if params.cwd:
        command = f"cd {shlex.quote(params.cwd)} && {command}"
    return command


def _decode(chunk: bytes | bytearray | memoryview | str) -> str:
    if isinstance(chunk, str):
        return chunk
    return bytes(chunk).decode("utf-8", errors="replace")


def _to_pty_input(data: str) -> str:
    # Terminal Enter is carriage return. Keep callers ergonomic by accepting
    # plain "\n" from the REST/interaction APIs and translating it for PTY mode.
    return data.replace("\r\n", "\r").replace("\n", "\r")
