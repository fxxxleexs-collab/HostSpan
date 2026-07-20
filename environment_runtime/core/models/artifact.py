from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..ids import new_id
from ..paths import WorkspacePath


class Artifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: new_id("artifact"))
    task_id: str | None = None
    workspace_path: WorkspacePath
    content_hash: str
    size_bytes: int
    media_type: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)
