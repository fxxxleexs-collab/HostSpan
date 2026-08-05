from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from mini_harness.sync.config import SyncConfig
from mini_harness.sync.manifest import SyncManifest


class SyncRunMetadata(BaseModel):
    workspace_id: str
    remote_root: str
    last_push_at: str | None = None
    last_upload_count: int = 0
    last_delete_count: int = 0


class SyncState(BaseModel):
    metadata: SyncRunMetadata
    manifest: SyncManifest = Field(default_factory=SyncManifest)


class SyncStateStore:
    def __init__(self, local_root: str | Path, config: SyncConfig | None = None) -> None:
        self.local_root = Path(local_root).resolve()
        self.config = config or SyncConfig()

    def state_path(self, workspace_id: str) -> Path:
        safe_id = _safe_workspace_id(workspace_id)
        return self.local_root / self.config.local_state_dir / f"{safe_id}.json"

    def load(self, workspace_id: str) -> SyncState | None:
        path = self.state_path(workspace_id)
        if not path.exists():
            return None
        return SyncState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, workspace_id: str, state: SyncState) -> Path:
        path = self.state_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def _safe_workspace_id(workspace_id: str) -> str:
    value = workspace_id.strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
