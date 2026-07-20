from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from environment_runtime.core.commands import CommandSpec
from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import Task, TaskState
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
