from __future__ import annotations

from pathlib import Path

from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.ids import new_id
from environment_runtime.core.models import (
    BindingMode,
    ReplicaState,
    Workspace,
    WorkspaceBinding,
    WorkspaceReplica,
    WorkspaceRoot,
)
from environment_runtime.services.runtime import RuntimeContext


class WorkspaceService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def create(self, name: str) -> Workspace:
        workspace = Workspace(name=name)
        await self.context.workspaces.upsert(workspace)
        return workspace

    async def get(self, workspace_id: str) -> Workspace:
        workspace = await self.context.workspaces.get(workspace_id)
        if workspace is None:
            raise NotFoundError(f"workspace {workspace_id} was not found")
        return workspace

    async def list_all(self) -> list[Workspace]:
        return await self.context.workspaces.list()

    async def add_root(self, workspace_id: str, logical_path: str) -> Workspace:
        workspace = await self.get(workspace_id)
        workspace.roots.append(WorkspaceRoot(logical_path=logical_path))
        return await self.context.workspaces.upsert(workspace)

    async def add_replica(self, workspace_id: str, endpoint_id: str, physical_root: str) -> Workspace:
        workspace = await self.get(workspace_id)
        workspace.replicas.append(
            WorkspaceReplica(
                workspace_id=workspace_id,
                endpoint_id=endpoint_id,
                physical_root=physical_root,
                state=ReplicaState.SYNCED,
            )
        )
        return await self.context.workspaces.upsert(workspace)

    async def bind(
        self,
        workspace_id: str,
        source_replica_id: str,
        target_replica_id: str,
        mode: BindingMode = BindingMode.ONE_WAY_MIRROR,
    ) -> Workspace:
        workspace = await self.get(workspace_id)
        workspace.bindings.append(
            WorkspaceBinding(
                binding_id=new_id("binding"),
                source_replica_id=source_replica_id,
                target_replica_id=target_replica_id,
                mode=mode,
            )
        )
        return await self.context.workspaces.upsert(workspace)

    async def create_revision(self, workspace_id: str, replica_id: str) -> Workspace:
        workspace = await self.get(workspace_id)
        replica = next((item for item in workspace.replicas if item.replica_id == replica_id), None)
        if replica is None:
            raise ValidationError(f"replica {replica_id} is not part of workspace {workspace_id}")
        revision = await self.context.providers.sync["snapshot"].compute_revision(Path(replica.physical_root))
        workspace.current_revision = revision
        replica.revision = revision
        replica.state = ReplicaState.SYNCED
        await self.context.workspaces.upsert(workspace)
        await self._emit("workspace.revision.created", workspace, {"revision": revision})
        return workspace

    async def sync(self, workspace_id: str, binding_id: str) -> Workspace:
        workspace = await self.get(workspace_id)
        binding = next((item for item in workspace.bindings if item.binding_id == binding_id), None)
        if binding is None:
            raise ValidationError(f"binding {binding_id} is not part of workspace {workspace_id}")
        source = next(item for item in workspace.replicas if item.replica_id == binding.source_replica_id)
        target = next(item for item in workspace.replicas if item.replica_id == binding.target_replica_id)
        await self._emit("workspace.sync.started", workspace, {"binding_id": binding_id})
        await self.context.providers.sync["snapshot"].mirror(
            Path(source.physical_root), Path(target.physical_root)
        )
        revision = await self.context.providers.sync["snapshot"].compute_revision(Path(source.physical_root))
        workspace.current_revision = revision
        source.revision = revision
        target.revision = revision
        source.state = ReplicaState.SYNCED
        target.state = ReplicaState.SYNCED
        await self.context.workspaces.upsert(workspace)
        await self._emit("workspace.sync.completed", workspace, {"binding_id": binding_id, "revision": revision})
        return workspace

    async def resolve_physical_path(
        self, workspace_id: str, root_id: str, relative_path: str = ""
    ) -> Path:
        workspace = await self.get(workspace_id)
        if not workspace.replicas:
            raise ValidationError("workspace has no replicas")
        root = next((item for item in workspace.roots if item.root_id == root_id), None)
        if root is None:
            raise ValidationError(f"workspace root {root_id} was not found")
        replica = workspace.replicas[0]
        root_path = Path(replica.physical_root) / root.logical_path
        return root_path / relative_path

    async def _emit(self, event_type: str, workspace: Workspace, payload: dict) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="workspace",
            resource_id=workspace.workspace_id,
            payload=payload | {"workspace_id": workspace.workspace_id},
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
