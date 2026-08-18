from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from environment_runtime.core.commands import CommandSpec
from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import Endpoint, Environment, ExecutionTarget, Task, TaskState
from environment_runtime.services.runtime import RuntimeContext


class TaskService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def start(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        persistent: bool = False,
    ) -> Task:
        if not argv:
            raise ValidationError("task argv cannot be empty")
        command = CommandSpec(argv=argv, env=env or {})
        task = Task(
            environment_id=environment_id,
            target_id=target_id,
            command=command,
            cwd=cwd,
            persistent=persistent,
            state=TaskState.PREPARING,
            started_at=datetime.now(UTC),
        )
        await self.context.tasks.upsert(task)
        await self._emit("task.preparing", task, task.model_dump(mode="json"))
        offset_counter = {"stdout": 0, "stderr": 0}

        async def on_output(stream: str, chunk: str) -> None:
            offset = offset_counter[stream]
            offset_counter[stream] += len(chunk)
            await self.context.log_repository.append(task.task_id, stream, offset, chunk)
            await self._emit(
                "task.output",
                task,
                {"stream": stream, "offset": offset, "chunk": chunk, "task_id": task.task_id},
            )

        environment, target, endpoint = await self._resolve_target(environment_id, target_id)

        if persistent:
            if endpoint.provider_type == "ssh":
                return await self._start_ssh_detached(task, command, cwd, on_output, endpoint)
            return await self._start_detached(task, command, cwd, on_output)

        if target.provider == "ssh_process":
            raise ValidationError("ssh_process execution is not implemented yet; use persistent=True")

        handle = await self.context.providers.execution["local_process"].start(
            command=command,
            cwd=Path(cwd) if cwd else None,
            env=command.env,
            on_output=on_output,
        )
        self.context.active.task_handles[task.task_id] = handle
        task.backend_ref = {"pid": handle.process.pid}
        task.state = TaskState.RUNNING
        await self.context.tasks.upsert(task)
        await self._emit("task.started", task, {"pid": handle.process.pid})
        watcher = asyncio.create_task(self._watch_task(task.task_id))
        self.context.active.background_tasks.append(watcher)
        return task

    async def _resolve_target(
        self, environment_id: str, target_id: str
    ) -> tuple[Environment, ExecutionTarget, Endpoint]:
        environment = await self.context.environments.get(environment_id)
        if environment is None:
            raise NotFoundError(f"environment {environment_id} was not found")
        target = next(
            (item for item in environment.execution_targets if item.target_id == target_id),
            None,
        )
        if target is None:
            raise NotFoundError(f"target {target_id} was not found in environment {environment_id}")
        endpoint = await self.context.endpoints.get(target.endpoint_id)
        if endpoint is None:
            raise NotFoundError(f"endpoint {target.endpoint_id} was not found")
        return environment, target, endpoint

    async def _start_detached(
        self,
        task: Task,
        command: CommandSpec,
        cwd: str | None,
        on_output,
    ) -> Task:
        provider = self.context.providers.execution["local_detached"]
        handle = await provider.start(
            command=command,
            cwd=Path(cwd) if cwd else None,
            env=command.env,
            on_output=on_output,
            task_id=task.task_id,
        )
        self.context.active.task_handles[task.task_id] = handle
        backend_ref: dict[str, object] = {
            "backend": "local_detached",
            "pid": handle.pid,
            "started_at": handle.started_at.isoformat(),
            "log_file": str(handle.log_file),
            "status_file": str(handle.status_file),
        }
        if handle.pgid is not None:
            backend_ref["pgid"] = handle.pgid
        task.backend_ref = backend_ref
        task.state = TaskState.RUNNING
        await self.context.tasks.upsert(task)
        await self._emit("task.started", task, {"pid": handle.pid, "persistent": True})
        watcher = asyncio.create_task(self._watch_detached(task.task_id))
        self.context.active.background_tasks.append(watcher)
        return task

    async def _start_ssh_detached(
        self,
        task: Task,
        command: CommandSpec,
        cwd: str | None,
        on_output,
        endpoint: Endpoint,
    ) -> Task:
        provider = self.context.providers.execution["ssh_detached"]
        handle = await provider.start(
            command=command,
            cwd=cwd,
            env=command.env,
            on_output=on_output,
            task_id=task.task_id,
            endpoint=endpoint,
        )
        self.context.active.task_handles[task.task_id] = handle
        task.backend_ref = {
            "backend": "ssh_detached",
            "endpoint_id": endpoint.endpoint_id,
            "remote_pid": handle.remote_pid,
            "remote_runtime_dir": endpoint.config.get("remote_runtime_dir", ".environment-runtime"),
            "remote_launcher": getattr(
                handle,
                "remote_launcher_file",
                provider._paths(task.task_id, endpoint)["launcher"],
            ),
            "launcher_kind": getattr(handle, "launcher_kind", "unknown"),
            "remote_log_file": handle.remote_log_file,
            "remote_status_file": handle.remote_status_file,
            "started_at": handle.started_at.isoformat(),
        }
        task.state = TaskState.RUNNING
        await self.context.tasks.upsert(task)
        await self._emit(
            "task.started",
            task,
            {"pid": handle.remote_pid, "persistent": True, "backend": "ssh_detached"},
        )
        watcher = asyncio.create_task(self._watch_detached(task.task_id))
        self.context.active.background_tasks.append(watcher)
        return task

    async def list_all(self) -> list[Task]:
        return await self.context.tasks.list()

    async def get(self, task_id: str) -> Task:
        task = await self.context.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task {task_id} was not found")
        return task

    async def logs(self, task_id: str) -> list[dict]:
        await self.get(task_id)
        return await self.context.log_repository.get_task_logs(task_id)

    async def cancel(self, task_id: str) -> Task:
        task = await self.get(task_id)
        handle = self.context.active.task_handles.get(task_id)
        if handle is None:
            raise NotFoundError(f"task {task_id} is not active")
        task.state = TaskState.CANCELLING
        await self.context.tasks.upsert(task)
        await handle.cancel()
        self.context.active.task_handles.pop(task_id, None)
        task.state = TaskState.CANCELLED
        task.finished_at = datetime.now(UTC)
        await self.context.tasks.upsert(task)
        await self._emit("task.cancelled", task, {"task_id": task.task_id})
        return task

    async def _watch_task(self, task_id: str) -> None:
        handle = self.context.active.task_handles.get(task_id)
        if handle is None:
            return
        returncode = await handle.wait()
        await handle.close()
        task = await self.get(task_id)
        task.exit_code = returncode
        task.finished_at = datetime.now(UTC)
        if task.state == TaskState.CANCELLING:
            task.state = TaskState.CANCELLED
            await self._emit("task.cancelled", task, {"returncode": returncode})
        elif returncode == 0:
            task.state = TaskState.SUCCEEDED
            await self._emit("task.completed", task, {"returncode": returncode})
        else:
            task.state = TaskState.FAILED
            await self._emit("task.failed", task, {"returncode": returncode})
        await self.context.tasks.upsert(task)
        self.context.active.task_handles.pop(task_id, None)

    async def _watch_detached(self, task_id: str) -> None:
        handle = self.context.active.task_handles.get(task_id)
        if handle is None:
            return
        exit_code = await handle.wait()
        await handle.close()
        task = await self.get(task_id)
        task.exit_code = exit_code
        task.finished_at = datetime.now(UTC)
        if task.state == TaskState.CANCELLING:
            task.state = TaskState.CANCELLED
            await self._emit("task.cancelled", task, {"returncode": exit_code})
        elif exit_code is None:
            # Could not determine the real exit code (e.g. PID reuse): mark lost
            # so the DB stays honest rather than guessing.
            task.state = TaskState.LOST
            await self._emit("task.lost", task, {"reason": "exit_code_unknown"})
        elif exit_code == 0:
            task.state = TaskState.SUCCEEDED
            await self._emit("task.completed", task, {"returncode": exit_code})
        else:
            task.state = TaskState.FAILED
            await self._emit("task.failed", task, {"returncode": exit_code})
        await self.context.tasks.upsert(task)
        self.context.active.task_handles.pop(task_id, None)

    async def reattach_on_startup(self, task: Task) -> bool:
        """Try to reclaim a detached task after a runtime restart.

        Returns True if the task was reconciled (either finalized from its
        status file or re-watched as still running), False if it could not be
        reliably reclaimed (caller should mark it LOST).
        """
        ref = task.backend_ref or {}
        backend = ref.get("backend")
        if backend not in {"local_detached", "ssh_detached"}:
            return False
        resume_offset = await self.context.log_repository.resume_offset(task.task_id)
        offset_counter = {"stdout": resume_offset}

        async def on_output(stream: str, chunk: str) -> None:
            offset = offset_counter[stream]
            offset_counter[stream] += len(chunk)
            await self.context.log_repository.append(task.task_id, stream, offset, chunk)
            await self._emit(
                "task.output",
                task,
                {"stream": stream, "offset": offset, "chunk": chunk, "task_id": task.task_id},
            )

        if backend == "ssh_detached":
            endpoint_id = str(ref["endpoint_id"])
            endpoint = await self.context.endpoints.get(endpoint_id)
            if endpoint is None:
                return False
            provider = self.context.providers.execution["ssh_detached"]
            outcome = await provider.reattach(task, endpoint, on_output, resume_offset)
        else:
            provider = self.context.providers.execution["local_detached"]
            outcome = await provider.reattach(task, on_output, resume_offset)
        if outcome.finished:
            task.exit_code = outcome.exit_code
            task.finished_at = outcome.finished_at or datetime.now(UTC)
            task.state = (
                TaskState.SUCCEEDED if outcome.exit_code == 0 else TaskState.FAILED
            )
            await self.context.tasks.upsert(task)
            await self._emit(
                "task.recovered",
                task,
                {"returncode": outcome.exit_code, "task_id": task.task_id},
            )
            return True
        if outcome.alive and outcome.handle is not None:
            # Still running: keep watching, but do NOT register in
            # active.task_handles — a reconnected task is not cancellable in L1.
            task.state = TaskState.RUNNING
            await self.context.tasks.upsert(task)
            await self._emit("task.reconnected", task, {"task_id": task.task_id})
            watcher = asyncio.create_task(self._watch_reconnected(task.task_id, outcome.handle))
            self.context.active.background_tasks.append(watcher)
            return True
        return False

    async def _watch_reconnected(self, task_id: str, handle: object) -> None:
        exit_code = await handle.wait()  # type: ignore[attr-defined]
        await handle.close()  # type: ignore[attr-defined]
        task = await self.get(task_id)
        task.exit_code = exit_code
        task.finished_at = datetime.now(UTC)
        if exit_code is None:
            task.state = TaskState.LOST
            await self._emit("task.lost", task, {"reason": "exit_code_unknown"})
        elif exit_code == 0:
            task.state = TaskState.SUCCEEDED
            await self._emit("task.completed", task, {"returncode": exit_code})
        else:
            task.state = TaskState.FAILED
            await self._emit("task.failed", task, {"returncode": exit_code})
        await self.context.tasks.upsert(task)

    async def _emit(self, event_type: str, task: Task, payload: dict) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="task",
            resource_id=task.task_id,
            environment_id=task.environment_id,
            payload=payload,
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
