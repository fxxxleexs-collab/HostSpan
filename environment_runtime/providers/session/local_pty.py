from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

OutputCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class LocalSessionHandle:
    process: asyncio.subprocess.Process
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def write(self, data: str) -> None:
        if self.process.stdin is None:
            return
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

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
    async def create(
        self,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str],
        on_output: OutputCallback,
    ) -> LocalSessionHandle:
        process = await asyncio.create_subprocess_exec(
            *argv,
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
