"""Remote detached execution provider for SSH endpoints.

This mirrors ``local_detached`` but stores log/status files on the remote host
and tails them over SFTP. The first implementation targets POSIX-like SSH
servers with ``sh``, ``nohup``, and ``python3`` available.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import asyncssh

from environment_runtime.core.errors import ProviderError
from environment_runtime.core.models import Endpoint
from environment_runtime.providers.filesystem.sftp import SFTPFilesystemProvider
from environment_runtime.providers.transport.ssh import SSHTransportProvider

if TYPE_CHECKING:
    from environment_runtime.core.commands import CommandSpec
    from environment_runtime.core.models import Task

OutputCallback = Callable[[str, str], Awaitable[None]]
STREAM = "stdout"


def _launcher_bytes() -> bytes:
    path = Path(__file__).with_name("_launcher.py")
    if path.exists():
        return path.read_bytes()
    return importlib.resources.read_binary(
        "environment_runtime.providers.execution",
        "_launcher.py",
    )


@dataclass
class SSHDetachedHandle:
    endpoint: Endpoint
    transport: SSHTransportProvider
    sftp: SFTPFilesystemProvider
    remote_pid: int
    remote_log_file: str
    remote_status_file: str
    started_at: datetime
    on_output: OutputCallback
    poll_interval: float = 0.5
    initial_offset: int = 0
    _pending_exit_code: int | None = None
    _pending_finished_at: datetime | None = None
    _completion: asyncio.Future[int | None] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._run()))

    async def wait(self) -> int | None:
        return await self._completion

    async def detach(self) -> None:
        await self.close()

    async def cancel(self) -> None:
        connection = await self.transport.connect(self.endpoint)
        with contextlib.suppress(OSError, asyncssh.Error):
            await connection.run(f"kill -TERM {self.remote_pid}", check=False)
        try:
            await asyncio.wait_for(self._completion, timeout=5)
        except TimeoutError:
            with contextlib.suppress(OSError, asyncssh.Error):
                await connection.run(f"kill -KILL {self.remote_pid}", check=False)
            await self.close()

    async def close(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        offset = self.initial_offset
        while True:
            offset = await self._drain_log(offset)
            if await self._check_completion():
                await self._drain_log(offset)
                self._resolve(self._pending_exit_code)
                return
            await asyncio.sleep(self.poll_interval)

    async def _drain_log(self, offset: int) -> int:
        if not await self.sftp.exists(self.endpoint, self.remote_log_file):
            return offset
        data = await self.sftp.read_bytes(self.endpoint, self.remote_log_file)
        if len(data) <= offset:
            return offset
        chunk = data[offset:]
        await self.on_output(STREAM, chunk.decode("utf-8", errors="replace"))
        return len(data)

    async def _check_completion(self) -> bool:
        if await self.sftp.exists(self.endpoint, self.remote_status_file):
            payload = await self._read_status()
            self._pending_exit_code = _payload_exit_code(payload)
            self._pending_finished_at = _payload_finished_at(payload)
            return True
        if not await self._is_alive():
            self._pending_exit_code = None
            return True
        return False

    async def _is_alive(self) -> bool:
        connection = await self.transport.connect(self.endpoint)
        try:
            result = await connection.run(f"kill -0 {self.remote_pid}", check=False)
        except (OSError, asyncssh.Error):
            return False
        return result.exit_status == 0

    async def _read_status(self) -> dict | None:
        try:
            data = await self.sftp.read_bytes(self.endpoint, self.remote_status_file)
            payload = json.loads(data.decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _resolve(self, exit_code: int | None) -> None:
        if not self._completion.done():
            self._completion.set_result(exit_code)


@dataclass
class SSHReattachOutcome:
    finished: bool = False
    alive: bool = False
    exit_code: int | None = None
    finished_at: datetime | None = None
    handle: SSHDetachedHandle | None = None


class SSHDetachedExecutionProvider:
    def __init__(
        self,
        transport: SSHTransportProvider,
        sftp: SFTPFilesystemProvider,
        remote_runtime_dir: str = ".environment-runtime",
        remote_python: str = "python3",
        poll_interval: float = 0.5,
    ) -> None:
        self.transport = transport
        self.sftp = sftp
        self.remote_runtime_dir = remote_runtime_dir
        self.remote_python = remote_python
        self.poll_interval = poll_interval

    async def start(
        self,
        command: CommandSpec,
        cwd: str | None,
        env: dict[str, str],
        on_output: OutputCallback,
        task_id: str,
        endpoint: Endpoint,
    ) -> SSHDetachedHandle:
        paths = self._paths(task_id, endpoint)
        await self._upload_launcher(endpoint, paths["launcher"])

        argv = self._launcher_argv(paths, command, cwd, env, endpoint)
        shell = self._detached_shell(argv)
        connection = await self.transport.connect(endpoint)
        try:
            result = await connection.run(shell, check=True)
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"failed to start remote detached task: {exc}") from exc
        remote_pid = _parse_pid(_stdout_text(result.stdout))
        handle = SSHDetachedHandle(
            endpoint=endpoint,
            transport=self.transport,
            sftp=self.sftp,
            remote_pid=remote_pid,
            remote_log_file=paths["log"],
            remote_status_file=paths["status"],
            started_at=datetime.now(UTC),
            on_output=on_output,
            poll_interval=self.poll_interval,
            initial_offset=0,
        )
        handle.start()
        return handle

    async def reattach(
        self,
        task: Task,
        endpoint: Endpoint,
        on_output: OutputCallback,
        resume_offset: int,
    ) -> SSHReattachOutcome:
        ref = task.backend_ref or {}
        status_file = str(ref["remote_status_file"])
        log_file = str(ref["remote_log_file"])
        remote_pid = int(ref["remote_pid"])
        started_at = datetime.fromisoformat(str(ref["started_at"]))

        if await self.sftp.exists(endpoint, status_file):
            payload = await self._read_status(endpoint, status_file)
            return SSHReattachOutcome(
                finished=True,
                exit_code=_payload_exit_code(payload),
                finished_at=_payload_finished_at(payload),
            )

        handle = SSHDetachedHandle(
            endpoint=endpoint,
            transport=self.transport,
            sftp=self.sftp,
            remote_pid=remote_pid,
            remote_log_file=log_file,
            remote_status_file=status_file,
            started_at=started_at,
            on_output=on_output,
            poll_interval=self.poll_interval,
            initial_offset=resume_offset,
        )
        if not await handle._is_alive():
            return SSHReattachOutcome(finished=False, alive=False)
        handle.start()
        return SSHReattachOutcome(finished=False, alive=True, handle=handle)

    async def _upload_launcher(self, endpoint: Endpoint, remote_launcher: str) -> None:
        await self.sftp.write_bytes(endpoint, remote_launcher, _launcher_bytes())

    async def _read_status(self, endpoint: Endpoint, status_file: str) -> dict | None:
        try:
            data = await self.sftp.read_bytes(endpoint, status_file)
            payload = json.loads(data.decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _paths(self, task_id: str, endpoint: Endpoint) -> dict[str, str]:
        base = str(endpoint.config.get("remote_runtime_dir") or self.remote_runtime_dir)
        root = PurePosixPath(base)
        return {
            "runtime_dir": root.as_posix(),
            "launcher": (root / "bin" / "_launcher.py").as_posix(),
            "log": (root / "logs" / f"{task_id}.log").as_posix(),
            "status": (root / "status" / f"{task_id}.status").as_posix(),
        }

    def _launcher_argv(
        self,
        paths: dict[str, str],
        command: CommandSpec,
        cwd: str | None,
        env: dict[str, str],
        endpoint: Endpoint,
    ) -> list[str]:
        remote_python = str(endpoint.config.get("remote_python") or self.remote_python)
        argv = [
            remote_python,
            paths["launcher"],
            "--log",
            paths["log"],
            "--status",
            paths["status"],
        ]
        if cwd:
            argv += ["--cwd", cwd]
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--", *command.argv]
        return argv

    def _detached_shell(self, argv: list[str]) -> str:
        quoted = " ".join(shlex.quote(item) for item in argv)
        return f"nohup {quoted} </dev/null >/dev/null 2>&1 & echo $!"


def _parse_pid(stdout: str) -> int:
    try:
        return int(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ProviderError(f"remote launcher did not return a pid: {stdout!r}") from exc


def _stdout_text(stdout: bytes | str | None) -> str:
    if stdout is None:
        return ""
    if isinstance(stdout, bytes):
        return stdout.decode("utf-8", errors="replace")
    return stdout


def _payload_exit_code(payload: dict | None) -> int | None:
    if payload is None:
        return None
    code = payload.get("exit_code")
    return int(code) if isinstance(code, int) else None


def _payload_finished_at(payload: dict | None) -> datetime | None:
    if payload is None:
        return None
    finished_at = payload.get("finished_at")
    if not isinstance(finished_at, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(finished_at)
    return None
