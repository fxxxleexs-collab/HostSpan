from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import (
    AddWorkspaceReplicaRequest,
    AddWorkspaceRootRequest,
    BindWorkspaceRequest,
    CreateWorkspaceRequest,
)
from environment_runtime.core.models import BindingMode
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.workspace import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("")
async def create_workspace(body: CreateWorkspaceRequest, runtime: RuntimeContext = Depends(get_runtime)):
    return await WorkspaceService(runtime).create(body.name)


@router.get("")
async def list_workspaces(runtime: RuntimeContext = Depends(get_runtime)):
    return await WorkspaceService(runtime).list_all()


@router.post("/{workspace_id}/roots")
async def add_root(
    workspace_id: str,
    body: AddWorkspaceRootRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return await WorkspaceService(runtime).add_root(workspace_id, body.logical_path)


@router.post("/{workspace_id}/replicas")
async def add_replica(
    workspace_id: str,
    body: AddWorkspaceReplicaRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return await WorkspaceService(runtime).add_replica(workspace_id, body.endpoint_id, body.physical_root)


@router.post("/{workspace_id}/bindings")
async def bind_workspace(
    workspace_id: str,
    body: BindWorkspaceRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return await WorkspaceService(runtime).bind(
        workspace_id,
        body.source_replica_id,
        body.target_replica_id,
        BindingMode(body.mode),
    )


@router.post("/{workspace_id}/revisions/{replica_id}")
async def create_revision(
    workspace_id: str, replica_id: str, runtime: RuntimeContext = Depends(get_runtime)
):
    return await WorkspaceService(runtime).create_revision(workspace_id, replica_id)


@router.post("/{workspace_id}/sync/{binding_id}")
async def sync_workspace(
    workspace_id: str, binding_id: str, runtime: RuntimeContext = Depends(get_runtime)
):
    return await WorkspaceService(runtime).sync(workspace_id, binding_id)
