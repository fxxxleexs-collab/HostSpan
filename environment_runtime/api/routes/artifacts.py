from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import DownloadArtifactRequest, RegisterArtifactRequest
from environment_runtime.core.paths import WorkspacePath
from environment_runtime.services.artifact import ArtifactService
from environment_runtime.services.runtime import RuntimeContext

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.post("")
async def register_artifact(
    body: RegisterArtifactRequest, runtime: RuntimeContext = Depends(get_runtime)
):
    return await ArtifactService(runtime).register(
        WorkspacePath(
            workspace_id=body.workspace_id, root_id=body.root_id, relative_path=body.relative_path
        ),
        task_id=body.task_id,
        media_type=body.media_type,
    )


@router.get("")
async def list_artifacts(runtime: RuntimeContext = Depends(get_runtime)):
    return await ArtifactService(runtime).list_all()


@router.post("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    body: DownloadArtifactRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return {"path": await ArtifactService(runtime).download(artifact_id, body.destination)}
