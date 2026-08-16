from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import asyncssh

from environment_runtime.core.errors import ProviderError, ValidationError
from environment_runtime.core.models import Endpoint, Session
from environment_runtime.providers.filesystem.sftp import SFTPFilesystemProvider
from environment_runtime.providers.session.base import (
    SessionBackendStatus,
    SessionCreateParams,
    SessionOutputCallback,
)
from environment_runtime.providers.transport.ssh import SSHTransportProvider

TMUX_STREAM = "pty"


@dataclass(frozen=True)
class TmuxPaths:
    root: str
    log: str
    status: str


@dataclass
class SSHTmuxSessionHandle:
    endpoint: Endpoint
    transport: SSHTransportProvider
    sftp: SFTPFilesystemProvider
    tmux_session: str
    tmux_target: str
    remote_log_file: str
    remote_status_file: str
    command: list[str]
    on_output: SessionOutputCallback
    poll_interval: float = 0.5
    initial_output_offset: int = 0
    backend_name: str = "ssh_tmux"
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _completion: asyncio.Future[int | None] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )
    _pending_exit_code: int | None = None

    def backend_ref(self) -> dict[str, object]:
        return {
            "backend": self.backend_name,
            "endpoint_id": self.endpoint.endpoint_id,
            "tmux_session": self.tmux_session,
            "tmux_target": self.tmux_target,
            "remote_log_file": self.remote_log_file,
            "remote_status_file": self.remote_status_file,
            "command": self.command,
        }

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._run()))

    async def write(self, data: str) -> None:
        buffer_name = f"envrt_{id(self):x}"
        await self._run_remote(
            _argv(
                [
                    "tmux",
                    "set-buffer",
                    "-b",
                    buffer_name,
                    "--",
                    data,
                ]
            ),
            check=True,
        )
        await self._run_remote(
            _argv(
                [
                    "tmux",
                    "paste-buffer",
                    "-b",
                    buffer_name,
                    "-t",
                    self.tmux_target,
                    "-d",
                ]
            ),
            check=True,
        )

    async def resize(self, cols: int, rows: int) -> None:
        await self._run_remote(
            _argv(
                [
                    "tmux",
                    "resize-window",
                    "-t",
                    self.tmux_session,
                    "-x",
                    str(cols),
                    "-y",
                    str(rows),
                ]
            ),
            check=False,
        )

    async def detach(self) -> None:
        await self.close()

    async def terminate(self) -> None:
        await self._run_remote(
            _argv(["tmux", "kill-session", "-t", self.tmux_session]),
            check=False,
        )
        await self.close()
        self._resolve(None)

    async def wait(self) -> int | None:
        return await self._completion

    async def close(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        offset = self.initial_output_offset
        while True:
            offset = await self._drain_log(offset)
            status = await self._status()
            if not status.alive:
                await self._drain_log(offset)
                self._resolve(status.exit_code)
                return
            await asyncio.sleep(self.poll_interval)

    async def _drain_log(self, offset: int) -> int:
        if not await self.sftp.exists(self.endpoint, self.remote_log_file):
            return offset
        data = await self.sftp.read_bytes(self.endpoint, self.remote_log_file)
        if len(data) <= offset:
            return offset
        chunk = data[offset:]
        await self.on_output(TMUX_STREAM, chunk.decode("utf-8", errors="replace"))
        return len(data)

    async def _status(self) -> SessionBackendStatus:
        return await _tmux_status(
            endpoint=self.endpoint,
            transport=self.transport,
            sftp=self.sftp,
            tmux_session=self.tmux_session,
            remote_status_file=self.remote_status_file,
        )

    async def _run_remote(self, command: str, check: bool) -> Any:
        connection = await self.transport.connect(self.endpoint)
        return await connection.run(command, check=check)

    def _resolve(self, exit_code: int | None) -> None:
        if not self._completion.done():
            self._completion.set_result(exit_code)


class SSHTmuxSessionProvider:
    backend_name = "ssh_tmux"

    def __init__(
        self,
        transport: SSHTransportProvider,
        sftp: SFTPFilesystemProvider,
        remote_runtime_dir: str = ".environment-runtime",
        poll_interval: float = 0.5,
    ) -> None:
        self.transport = transport
        self.sftp = sftp
        self.remote_runtime_dir = remote_runtime_dir
        self.poll_interval = poll_interval

    async def create(
        self,
        params: SessionCreateParams,
        on_output: SessionOutputCallback,
    ) -> SSHTmuxSessionHandle:
        if params.endpoint.provider_type != "ssh":
            raise ValidationError(f"endpoint {params.endpoint.endpoint_id} is not an SSH endpoint")
        tmux_session = _tmux_session_name(params.session_id)
        paths = self._paths(params.endpoint, tmux_session)
        await self._prepare_remote_files(params.endpoint, paths)
        await self._ensure_tmux(params.endpoint)
        await self._start_tmux(params, tmux_session, paths)
        target = f"{tmux_session}:0.0"
        await self._pipe_pane(params.endpoint, target, paths.log)
        handle = SSHTmuxSessionHandle(
            endpoint=params.endpoint,
            transport=self.transport,
            sftp=self.sftp,
            tmux_session=tmux_session,
            tmux_target=target,
            remote_log_file=paths.log,
            remote_status_file=paths.status,
            command=params.argv,
            on_output=on_output,
            poll_interval=self.poll_interval,
            initial_output_offset=0,
        )
        handle.start()
        return handle

    async def attach(
        self,
        session: Session,
        endpoint: Endpoint,
        on_output: SessionOutputCallback,
        initial_output_offset: int = 0,
    ) -> SSHTmuxSessionHandle:
        ref = session.backend_ref or {}
        tmux_session = str(ref.get("tmux_session") or _tmux_session_name(session.session_id))
        target = str(ref.get("tmux_target") or f"{tmux_session}:0.0")
        paths = self._paths(endpoint, tmux_session)
        log_file = str(ref.get("remote_log_file") or paths.log)
        status_file = str(ref.get("remote_status_file") or paths.status)
        status = await _tmux_status(
            endpoint=endpoint,
            transport=self.transport,
            sftp=self.sftp,
            tmux_session=tmux_session,
            remote_status_file=status_file,
        )
        if not status.alive:
            raise ProviderError(status.detail or "tmux session is not alive")
        handle = SSHTmuxSessionHandle(
            endpoint=endpoint,
            transport=self.transport,
            sftp=self.sftp,
            tmux_session=tmux_session,
            tmux_target=target,
            remote_log_file=log_file,
            remote_status_file=status_file,
            command=session.command,
            on_output=on_output,
            poll_interval=self.poll_interval,
            initial_output_offset=initial_output_offset,
        )
        handle.start()
        return handle

    async def status(self, session: Session, endpoint: Endpoint) -> SessionBackendStatus:
        ref = session.backend_ref or {}
        tmux_session = str(ref.get("tmux_session") or _tmux_session_name(session.session_id))
        paths = self._paths(endpoint, tmux_session)
        status_file = str(ref.get("remote_status_file") or paths.status)
        return await _tmux_status(
            endpoint=endpoint,
            transport=self.transport,
            sftp=self.sftp,
            tmux_session=tmux_session,
            remote_status_file=status_file,
        )

    async def _prepare_remote_files(self, endpoint: Endpoint, paths: TmuxPaths) -> None:
        await self.sftp.ensure_dir(endpoint, paths.root)
        await self.sftp.write_bytes(endpoint, paths.log, b"")
        with contextlib.suppress(OSError, asyncssh.SFTPError):
            await self.sftp.remove(endpoint, paths.status)

    async def _ensure_tmux(self, endpoint: Endpoint) -> None:
        connection = await self.transport.connect(endpoint)
        try:
            await connection.run("command -v tmux >/dev/null 2>&1", check=True)
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError("tmux is not available on the SSH endpoint") from exc

    async def _start_tmux(
        self,
        params: SessionCreateParams,
        tmux_session: str,
        paths: TmuxPaths,
    ) -> None:
        connection = await self.transport.connect(params.endpoint)
        command = _session_shell(params, paths.status)
        argv = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            tmux_session,
            "-x",
            str(params.terminal_size.cols),
            "-y",
            str(params.terminal_size.rows),
            "--",
            "sh",
            "-lc",
            command,
        ]
        try:
            await connection.run(_argv(argv), check=True)
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"failed to start tmux session: {exc}") from exc

    async def _pipe_pane(self, endpoint: Endpoint, target: str, log_file: str) -> None:
        connection = await self.transport.connect(endpoint)
        pipe_command = f"cat >> {shlex.quote(log_file)}"
        try:
            await connection.run(
                _argv(["tmux", "pipe-pane", "-o", "-t", target, pipe_command]),
                check=True,
            )
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"failed to attach tmux log pipe: {exc}") from exc

    def _paths(self, endpoint: Endpoint, tmux_session: str) -> TmuxPaths:
        base = str(endpoint.config.get("remote_runtime_dir") or self.remote_runtime_dir)
        root = PurePosixPath(base) / "sessions" / tmux_session
        return TmuxPaths(
            root=root.as_posix(),
            log=(root / "terminal.log").as_posix(),
            status=(root / "status.json").as_posix(),
        )


async def _tmux_status(
    endpoint: Endpoint,
    transport: SSHTransportProvider,
    sftp: SFTPFilesystemProvider,
    tmux_session: str,
    remote_status_file: str,
) -> SessionBackendStatus:
    if await sftp.exists(endpoint, remote_status_file):
        payload = await _read_status(endpoint, sftp, remote_status_file)
        return SessionBackendStatus(
            alive=False,
            exit_code=_payload_exit_code(payload),
            detail="tmux session finished",
            finished=True,
        )
    connection = await transport.connect(endpoint)
    try:
        result = await connection.run(_argv(["tmux", "has-session", "-t", tmux_session]), check=False)
    except (OSError, asyncssh.Error) as exc:
        return SessionBackendStatus(
            alive=False,
            detail=f"tmux status unavailable: {exc}",
            checked=False,
        )
    if getattr(result, "exit_status", 1) == 0:
        return SessionBackendStatus(alive=True, detail="tmux session is alive")
    delayed_payload = await _read_status_when_available(endpoint, sftp, remote_status_file)
    if delayed_payload is not None:
        return SessionBackendStatus(
            alive=False,
            exit_code=_payload_exit_code(delayed_payload),
            detail="tmux session finished",
            finished=True,
        )
    return SessionBackendStatus(alive=False, detail="tmux session is not alive")


async def _read_status_when_available(
    endpoint: Endpoint,
    sftp: SFTPFilesystemProvider,
    remote_status_file: str,
    timeout: float = 1.0,
    interval: float = 0.1,
) -> dict[str, object] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await sftp.exists(endpoint, remote_status_file):
            return await _read_status(endpoint, sftp, remote_status_file)
        await asyncio.sleep(interval)
    return None


async def _read_status(
    endpoint: Endpoint,
    sftp: SFTPFilesystemProvider,
    remote_status_file: str,
) -> dict[str, object] | None:
    try:
        data = await sftp.read_bytes(endpoint, remote_status_file)
        payload = json.loads(data.decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_exit_code(payload: dict[str, object] | None) -> int | None:
    if payload is None:
        return None
    code = payload.get("exit_code")
    return int(code) if isinstance(code, int) else None


def _session_shell(params: SessionCreateParams, status_file: str) -> str:
    user_command = _user_command(params)
    status_target = shlex.quote(status_file)
    return "\n".join(
        [
            user_command,
            "code=$?",
            (
                "printf '{\"exit_code\":%s,\"finished_at\":\"%s\"}\\n' "
                '"$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
                f"> {status_target}"
            ),
            'exit "$code"',
        ]
    )


def _user_command(params: SessionCreateParams) -> str:
    command = " ".join(shlex.quote(item) for item in params.argv)
    env = params.env or {}
    if env:
        assignments = " ".join(shlex.quote(f"{key}={value}") for key, value in env.items())
        command = f"env {assignments} {command}"
    if params.cwd:
        command = f"cd {shlex.quote(params.cwd)} && {command}"
    return command


def _tmux_session_name(session_id: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in session_id)
    return f"envrt_{safe}"


def _argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv)
