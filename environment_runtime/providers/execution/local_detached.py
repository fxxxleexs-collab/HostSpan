"""Detached execution provider for persistent tasks.

A persistent task must survive a runtime restart, keep its output, and have its
exit code recoverable. This provider achieves that by:

1. Spawning the bundled launcher (``_launcher.py``) detached from the runtime
   (its own session on POSIX / new process group on Windows). The launcher is
   the parent of the real command, redirects output to a log file, and writes
   the exit code to a status file on exit.
2. Tailing the log file (not a pipe) so output capture is decoupled from the
   runtime process lifetime.
3. Exposing ``reattach`` so a restarted runtime can resume tailing/polling a
   surviving task and recover its exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from environment_runtime.core.models import Task

OutputCallback = Callable[[str, str], Awaitable[None]]

# Combined stdout+stderr are written to one log file; expose them under the
# "stdout" stream name so the existing LogRepository stays single-stream.
STREAM = "stdout"


def _launcher_path() -> Path:
    """Resolve the on-disk path to the bundled launcher script."""
    files = importlib.resources.files("environment_runtime.providers.execution")
    return Path(str(files.joinpath("_launcher.py")))


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _process_start_time(pid: int) -> float | None:
    """Best-effort process start time (Linux: /proc jiffies; else None).

    Used to mitigate PID reuse on reconnect: a recycled PID will report a
    different start time than the one recorded at launch.
    """
    if _is_windows():
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # Field 22 (1-indexed) is starttime in clock ticks. Comm may contain spaces
    # and is wrapped in (), so split from the last ')' to be safe.
    right = data.rsplit(")", 1)[-1].split()
    # After ')', field indices restart at 1 starting with 'state' (field 3).
    # starttime is field 22 => index 22 - 3 = 19 in this right-side split.
    if len(right) > 19:
        try:
            return float(right[19])
        except ValueError:
            return None
    return None


@dataclass
class LocalDetachedHandle:
    """In-memory handle for a detached task.

    Drives a single background task (``_run``) that tails the log file and
    detects completion in one loop. A single task avoids the race where a
    separate poller resolves ``_completion`` (and the watcher then closes the
    handle) before the tailer has drained the final bytes. When completion is
    detected the loop drains any remaining bytes - seeking before each read,
    since a read at EOF does not surface bytes appended by another process on
    some platforms - and only then resolves ``_completion``.
    """

    pid: int
    log_file: Path
    status_file: Path
    started_at: datetime
    on_output: OutputCallback
    poll_interval: float = 0.5
    pgid: int | None = None
    initial_offset: int = 0
    _pending_exit_code: int | None = None
    _completion: asyncio.Future[int] = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._run()))

    async def wait(self) -> int | None:
        return await self._completion

    async def cancel(self) -> None:
        """Terminate the detached process tree. Completion is finalized via the
        normal status path (the launcher forwards the signal and writes status),
        or, if the launcher is already gone, via liveness detection."""
        with contextlib.suppress(ProcessLookupError):
            if self.pgid is not None and os.name == "posix":
                os.killpg(self.pgid, signal.SIGTERM)  # type: ignore[attr-defined]
            elif os.name == "posix":
                os.kill(self.pid, signal.SIGTERM)
            else:
                # Windows: force-kill the process tree rooted at the launcher.
                # /F is required: without it taskkill cannot terminate console
                # apps ("only force termination can be used").
                subprocess.run(  # noqa: S603, S607 - taskkill is the documented API
                    ["taskkill", "/T", "/F", "/PID", str(self.pid)],
                    check=False,
                    capture_output=True,
                )
        # Give the launcher a moment to write its status file gracefully.
        try:
            await asyncio.wait_for(self._completion, timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                if self.pgid is not None and os.name == "posix":
                    os.killpg(self.pgid, signal.SIGKILL)  # type: ignore[attr-defined]
                elif os.name == "posix":
                    os.kill(self.pid, signal.SIGKILL)  # type: ignore[attr-defined]
            await self.close()

    async def detach(self) -> None:
        """Stop watching but leave the subprocess running (used on shutdown of
        a persistent task)."""
        await self.close()

    async def close(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        """Tail the log file and watch for completion in a single loop.

        On completion (status file present, or the process died), drain any
        remaining bytes - seeking before each read - then resolve
        ``_completion``. Resolving only after the drain guarantees the watcher
        (which awaits ``_completion``) cannot close the handle and truncate the
        final output.
        """
        offset = self.initial_offset
        handle = None
        try:
            while True:
                if handle is None:
                    try:
                        handle = self.log_file.open("rb")
                    except FileNotFoundError:
                        handle = None
                if handle is not None:
                    handle.seek(offset)
                    chunk = handle.read(4096)
                    if chunk:
                        offset += len(chunk)
                        await self.on_output(STREAM, chunk.decode("utf-8", errors="replace"))
                        continue
                # No new bytes (or no log file yet): check for completion.
                if self._check_completion():
                    # Drain bytes written between the last read and the status
                    # file appearing.
                    if handle is not None:
                        handle.seek(offset)
                        while True:
                            tail = handle.read(4096)
                            if not tail:
                                break
                            offset += len(tail)
                            await self.on_output(STREAM, tail.decode("utf-8", errors="replace"))
                    self._resolve(self._pending_exit_code)
                    return
                await asyncio.sleep(self.poll_interval)
        finally:
            if handle is not None:
                handle.close()

    def _check_completion(self) -> bool:
        """Return True if the task is finished (status file present or process
        dead); store the recovered exit code in ``_pending_exit_code``."""
        if self.status_file.exists():
            self._pending_exit_code = self._read_exit_code()
            return True
        if not self._is_ours_and_alive():
            # Process gone with no status file: died uncleanly (e.g. killed).
            self._pending_exit_code = None
            return True
        return False

    def _read_exit_code(self) -> int | None:
        try:
            payload = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        code = payload.get("exit_code")
        return int(code) if isinstance(code, int) else None

    def _is_ours_and_alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # EPERM etc.: process exists but not ours to signal — treat as alive.
            return True
        # PID reuse mitigation (Linux only): compare start times.
        recorded = _process_start_time(self.pid)
        if recorded is None:
            return True
        original = _process_start_time_to_ticks(self.started_at)
        if original is None:
            return True
        return abs(recorded - original) <= 1

    def _resolve(self, exit_code: int | None) -> None:
        if not self._completion.done():
            self._completion.set_result(exit_code)  # type: ignore[arg-type]


def _process_start_time_to_ticks(started_at: datetime) -> float | None:
    """Approximate the recorded start time as /proc starttime ticks.

    The recorded ``started_at`` is wall-clock UTC; /proc starttime is ticks
    since boot. A direct mapping is not exact, so this returns None to disable
    the reuse check rather than risk false negatives. (PID reuse mitigation is
    best-effort; the status file remains the authoritative terminal signal.)
    """
    return None


@dataclass
class ReattachOutcome:
    finished: bool = False
    alive: bool = False
    exit_code: int | None = None
    finished_at: datetime | None = None
    handle: LocalDetachedHandle | None = None


class LocalDetachedExecutionProvider:
    def __init__(self, log_dir: Path, status_dir: Path, poll_interval: float = 0.5) -> None:
        self.log_dir = log_dir
        self.status_dir = status_dir
        self.poll_interval = poll_interval

    def _paths(self, task_id: str) -> tuple[Path, Path]:
        return self.log_dir / f"{task_id}.log", self.status_dir / f"{task_id}.status"

    async def start(
        self,
        command,  # type: ignore[no-untyped-def]
        cwd: Path | None,
        env: dict[str, str],
        on_output: OutputCallback,
        task_id: str,
    ) -> LocalDetachedHandle:
        log_file, status_file = self._paths(task_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.parent.mkdir(parents=True, exist_ok=True)

        launcher = _launcher_path()
        argv: list[str] = [
            sys.executable,
            str(launcher),
            "--log",
            str(log_file),
            "--status",
            str(status_file),
        ]
        if cwd is not None:
            argv += ["--cwd", str(cwd)]
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--", *command.argv]

        # Spawn the launcher fire-and-forget. The launcher does its own file I/O,
        # so we do not need asyncio pipe management here, and a plain Popen is
        # not tied to an asyncio transport whose collection could terminate the
        # child. The launcher is detached so it survives a runtime shutdown.
        if os.name == "posix":
            proc = subprocess.Popen(  # noqa: S603 - argv is runtime-controlled
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            # CREATE_NO_WINDOW runs the launcher (a console-subsystem app) with a
            # hidden console but no window. DETACHED_PROCESS gives no console at
            # all, which makes console apps like python.exe hang on startup.
            proc = subprocess.Popen(  # noqa: S603 - argv is runtime-controlled
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        pgid: int | None = None
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, OSError):
                pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]

        handle = LocalDetachedHandle(
            pid=proc.pid,
            log_file=log_file,
            status_file=status_file,
            started_at=datetime.now(UTC),
            on_output=on_output,
            poll_interval=self.poll_interval,
            pgid=pgid,
            initial_offset=0,
        )
        handle.start()
        return handle

    async def reattach(
        self,
        task: Task,
        on_output: OutputCallback,
        resume_offset: int,
    ) -> ReattachOutcome:
        ref = task.backend_ref or {}
        log_file = Path(ref["log_file"])
        status_file = Path(ref["status_file"])
        pid = int(ref["pid"])
        started_at = datetime.fromisoformat(ref["started_at"])

        # Authoritative: status file present => task finished while runtime down.
        if status_file.exists():
            payload = self._read_status(status_file)
            exit_code = payload.get("exit_code") if payload else None
            finished_at = payload.get("finished_at") if payload else None
            parsed_finished: datetime | None = None
            if isinstance(finished_at, str):
                with contextlib.suppress(ValueError):
                    parsed_finished = datetime.fromisoformat(finished_at)
            return ReattachOutcome(
                finished=True,
                exit_code=int(exit_code) if isinstance(exit_code, int) else None,
                finished_at=parsed_finished,
            )

        # Best-effort liveness (Linux: with start-time check; Windows: weaker).
        handle = LocalDetachedHandle(
            pid=pid,
            log_file=log_file,
            status_file=status_file,
            started_at=started_at,
            on_output=on_output,
            poll_interval=self.poll_interval,
            pgid=ref.get("pgid"),
            initial_offset=resume_offset,
        )
        if not handle._is_ours_and_alive():
            return ReattachOutcome(finished=False, alive=False)
        handle.start()
        return ReattachOutcome(finished=False, alive=True, handle=handle)

    @staticmethod
    def _read_status(status_file: Path) -> dict | None:
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
