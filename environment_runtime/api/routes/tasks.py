from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import StartTaskRequest
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.task import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("")
async def start_task(body: StartTaskRequest, runtime: RuntimeContext = Depends(get_runtime)):
    return await TaskService(runtime).start(
        body.environment_id,
        body.target_id,
        body.argv,
        cwd=body.cwd,
        env=body.env,
        persistent=body.persistent,
    )


@router.get("")
async def list_tasks(runtime: RuntimeContext = Depends(get_runtime)):
    return await TaskService(runtime).list_all()


@router.get("/{task_id}")
async def get_task(task_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await TaskService(runtime).get(task_id)


@router.get("/{task_id}/logs")
async def get_task_logs(task_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await TaskService(runtime).logs(task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await TaskService(runtime).cancel(task_id)
