from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import CreateLocalEndpointRequest
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.runtime import RuntimeContext

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


@router.post("")
async def add_local_endpoint(
    body: CreateLocalEndpointRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await EndpointService(runtime).add_local(body.name, body.root)


@router.get("")
async def list_endpoints(runtime: RuntimeContext = Depends(get_runtime)):
    return await EndpointService(runtime).list_all()


@router.get("/{endpoint_id}/health")
async def endpoint_health(endpoint_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await EndpointService(runtime).health(endpoint_id)
