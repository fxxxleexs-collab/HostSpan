from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import InteractionState, SessionState, TaskState

if TYPE_CHECKING:
    from environment_runtime.services.runtime import RuntimeContext

# States that imply a live in-memory handle. On a fresh build_runtime the
# active-handle maps are empty, so any persisted record in one of these states
# is stale (the backing process handle was lost with the previous process).
_TASK_RECOVERABLE_STATES = {TaskState.PREPARING, TaskState.RUNNING, TaskState.CANCELLING}
_SESSION_RECOVERABLE_STATES = {SessionState.CREATING, SessionState.ACTIVE}


class RecoveryService:
    """Honest restart reconciliation.

    Does not attempt to reattach to surviving subprocesses (that is L1/L2
    work). It only makes the persisted state match reality: any task/session
    that claimed to be live but has no handle in this process is marked lost
    or disconnected so the DB no longer lies.
    """

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def reconcile_on_startup(self) -> dict[str, int]:
        tasks_recovered = await self._reconcile_tasks()
        sessions_recovered = await self._reconcile_sessions()
        return {"tasks": tasks_recovered, "sessions": sessions_recovered}

    async def _reconcile_tasks(self) -> int:
        # Imported lazily to avoid a module-load cycle (task -> runtime -> recovery).
        from environment_runtime.services.task import TaskService

        now = datetime.now(UTC)
        count = 0
        for task in await self.context.tasks.list():
            if task.state not in _TASK_RECOVERABLE_STATES:
                continue
            backend = (task.backend_ref or {}).get("backend")
            if backend == "local_detached":
                # Try to reclaim a surviving detached task (finalize from its
                # status file, or re-watch it as still running). Recovery runs on
                # every startup, so a failure to reclaim must not crash the boot.
                try:
                    reclaimed = await TaskService(self.context).reattach_on_startup(task)
                except Exception:
                    reclaimed = False
                if reclaimed:
                    count += 1
                    continue
            # Fallback: cannot reclaim -> mark lost so the DB stays honest.
            task.state = TaskState.LOST
            task.finished_at = now
            await self.context.tasks.upsert(task)
            await self._emit(
                "task.lost",
                resource_type="task",
                resource_id=task.task_id,
                environment_id=task.environment_id,
                payload={"task_id": task.task_id, "reason": "runtime_restart"},
            )
            count += 1
        return count

    async def _reconcile_sessions(self) -> int:
        count = 0
        for session in await self.context.sessions.list():
            if session.state not in _SESSION_RECOVERABLE_STATES:
                continue
            session.state = SessionState.DISCONNECTED
            session.interaction_state = InteractionState.NONE
            await self.context.sessions.upsert(session)
            await self._emit(
                "session.disconnected",
                resource_type="session",
                resource_id=session.session_id,
                environment_id=session.environment_id,
                payload={"session_id": session.session_id, "reason": "runtime_restart"},
            )
            count += 1
        return count

    async def _emit(
        self,
        event_type: str,
        resource_type: str,
        resource_id: str,
        environment_id: str | None,
        payload: dict,
    ) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            environment_id=environment_id,
            payload=payload,
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
