from __future__ import annotations

from pathlib import Path

from environment_runtime.core.errors import NotFoundError
from environment_runtime.core.models import Artifact
from environment_runtime.core.paths import WorkspacePath
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.workspace import WorkspaceService


class ArtifactService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        self._workspaces = WorkspaceService(context)

    async def register(
        self,
        workspace_path: WorkspacePath,
        task_id: str | None = None,
        media_type: str | None = None,
    ) -> Artifact:
        physical = await self._workspaces.resolve_physical_path(
            workspace_path.workspace_id, workspace_path.root_id, workspace_path.relative_path
        )
        provider = self.context.providers.filesystem["local"]
        data = await provider.read_bytes(physical)
        content_hash = await provider.sha256(physical)
        artifact = Artifact(
            task_id=task_id,
            workspace_path=workspace_path,
            content_hash=content_hash,
            size_bytes=len(data),
            media_type=media_type,
        )
        await self.context.artifacts.upsert(artifact)
        return artifact

    async def list_all(self) -> list[Artifact]:
        return await self.context.artifacts.list()

    async def download(self, artifact_id: str, destination: str) -> str:
        artifact = await self.context.artifacts.get(artifact_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id} was not found")
        physical = await self._workspaces.resolve_physical_path(
            artifact.workspace_path.workspace_id,
            artifact.workspace_path.root_id,
            artifact.workspace_path.relative_path,
        )
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(physical.read_bytes())
        return str(target)
