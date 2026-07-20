from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import CreateEnvironmentRequest
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.runtime import RuntimeContext

router = APIRouter(prefix="/environments", tags=["environments"])


@router.post("")
async def create_environment(
    body: CreateEnvironmentRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await EnvironmentService(runtime).create(body.name, body.endpoint_ids, body.workspace_ids)


@router.get("")
async def list_environments(runtime: RuntimeContext = Depends(get_runtime)):
    return await EnvironmentService(runtime).list_all()


@router.get("/{environment_id}")
async def get_environment(environment_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await EnvironmentService(runtime).get(environment_id)


@router.post("/{environment_id}/reconcile")
async def reconcile_environment(environment_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await EnvironmentService(runtime).reconcile(environment_id)
