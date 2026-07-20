from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import (
    AcquireLeaseRequest,
    CreateInputRequestRequest,
    SubmitInputRequest,
)
from environment_runtime.core.models import InputType
from environment_runtime.services.interaction import InteractionService
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.security import WriterLeaseService

router = APIRouter(prefix="/interactions", tags=["interactions"])


@router.post("/requests")
async def create_input_request(
    body: CreateInputRequestRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await InteractionService(runtime).create_request(
        body.session_id,
        InputType(body.input_type),
        prompt=body.prompt,
        task_id=body.task_id,
        allowed_values=body.allowed_values,
    )


@router.post("/requests/{request_id}/submit")
async def submit_input(
    request_id: str,
    body: SubmitInputRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return await InteractionService(runtime).submit_input(request_id, body.owner_id, body.value)


@router.post("/leases")
async def acquire_lease(body: AcquireLeaseRequest, runtime: RuntimeContext = Depends(get_runtime)):
    return await WriterLeaseService(runtime).acquire(
        body.session_id,
        body.owner_type,
        body.owner_id,
        ttl_seconds=body.ttl_seconds,
        force=body.force,
    )
