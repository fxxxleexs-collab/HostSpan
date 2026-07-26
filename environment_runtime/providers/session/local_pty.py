from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from environment_runtime.core.errors import ProviderError
from environment_runtime.core.models import Session
from environment_runtime.providers.session.base import (
    SessionBackendStatus,
    SessionCreateParams,
    SessionOutputCallback,
)


@dataclass
class LocalSessionHandle:
    process: asyncio.subprocess.Process
    backend_name: str = "local_pty"
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def backend_ref(self) -> dict[str, object]:
        return {"pid": self.process.pid}

    async def write(self, data: str) -> None:
        if self.process.stdin is None:
            return
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

    async def resize(self, cols: int, rows: int) -> None:
        # This provider is pipe-based today, not a real PTY, so resizing is a no-op.
        _ = (cols, rows)

    async def detach(self) -> None:
        await self.close()

    async def terminate(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
        await self.close()

    async def wait(self) -> int:
        return await self.process.wait()

    async def close(self) -> None:
        for task in self.reader_tasks:
            if not task.done():
                task.cancel()
        for task in self.reader_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


class LocalSessionProvider:
    backend_name = "local_pty"

    async def create(
        self,
        params: SessionCreateParams,
        on_output: SessionOutputCallback,
    ) -> LocalSessionHandle:
        env = params.env or {}
        cwd = Path(params.cwd) if params.cwd else None
        process = await asyncio.create_subprocess_exec(
            *params.argv,
            cwd=str(cwd) if cwd else None,
            env=None if not env else {**env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )

        async def pump(stream_name: str, stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while chunk := await stream.read(4096):
                await on_output(stream_name, chunk.decode(errors="replace"))

        handle = LocalSessionHandle(process=process)
        handle.reader_tasks.extend(
            [
                asyncio.create_task(pump("stdout", process.stdout)),
                asyncio.create_task(pump("stderr", process.stderr)),
            ]
        )
        return handle

    async def attach(
        self,
        session: Session,
        on_output: SessionOutputCallback,
    ) -> LocalSessionHandle:
        _ = (session, on_output)
        raise ProviderError("local_pty sessions cannot be attached after runtime restart")

    async def status(self, session: Session) -> SessionBackendStatus:
        _ = session
        return SessionBackendStatus(alive=False, detail="local_pty status requires an active handle")
