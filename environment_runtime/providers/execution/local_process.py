from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from environment_runtime.core.commands import CommandSpec

OutputCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class LocalProcessHandle:
    process: asyncio.subprocess.Process
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def wait(self) -> int:
        return await self.process.wait()

    async def cancel(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
        await self.close()

    async def close(self) -> None:
        for task in self.reader_tasks:
            if not task.done():
                task.cancel()
        for task in self.reader_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


class LocalProcessExecutionProvider:
    async def start(
        self,
        command: CommandSpec,
        cwd: Path | None,
        env: dict[str, str],
        on_output: OutputCallback,
    ) -> LocalProcessHandle:
        merged_env = None if not env else {**env}
        if command.shell:
            process = await asyncio.create_subprocess_shell(
                " ".join(command.argv),
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )

        async def pump(stream_name: str, stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while chunk := await stream.read(4096):
                await on_output(stream_name, chunk.decode(errors="replace"))

        handle = LocalProcessHandle(process=process)
        handle.reader_tasks.extend(
            [
                asyncio.create_task(pump("stdout", process.stdout)),
                asyncio.create_task(pump("stderr", process.stderr)),
            ]
        )
        return handle
