from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import CreateLocalEndpointRequest, CreateSSHEndpointRequest
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.runtime import RuntimeContext

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


@router.post("")
async def add_local_endpoint(
    body: CreateLocalEndpointRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await EndpointService(runtime).add_local(body.name, body.root)


@router.post("/ssh")
async def add_ssh_endpoint(
    body: CreateSSHEndpointRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await EndpointService(runtime).add_ssh(
        name=body.name,
        hostname=body.hostname,
        username=body.username,
        known_hosts_file=body.known_hosts_file,
        port=body.port,
        auth_method=body.auth_method,
        identity_file=body.identity_file,
        password_secret_ref=body.password_secret_ref,
        use_ssh_agent=body.use_ssh_agent,
        proxy_jump=body.proxy_jump,
        connect_timeout=body.connect_timeout,
        keepalive_interval=body.keepalive_interval,
    )


@router.get("")
async def list_endpoints(runtime: RuntimeContext = Depends(get_runtime)):
    return await EndpointService(runtime).list_all()


@router.get("/{endpoint_id}/health")
async def endpoint_health(endpoint_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await EndpointService(runtime).health(endpoint_id)
