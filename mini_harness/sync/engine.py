from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.sync.config import SyncConfig
from mini_harness.sync.errors import SyncConflictError
from mini_harness.sync.manifest import ManifestScanResult, SyncManifest, scan_local_manifest
from mini_harness.sync.planner import SyncPlan, plan_push
from mini_harness.sync.state import SyncRunMetadata, SyncState, SyncStateStore


class SyncPushResult(BaseModel):
    ok: bool
    workspace_id: str
    plan: SyncPlan
    manifest: SyncManifest
    uploaded: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    local_state_path: str | None = None
    remote_manifest_path: str | None = None
    requested_path: str | None = None
    skipped_reason: str | None = None


class SyncEngine:
    def __init__(
        self,
        *,
        runtime: HarnessRuntimeClient,
        endpoint_id: str,
        local_root: str | Path,
        remote_root: str,
        workspace_id: str = "default",
        config: SyncConfig | None = None,
        state_store: SyncStateStore | None = None,
    ) -> None:
        self.runtime = runtime
        self.endpoint_id = endpoint_id
        self.local_root = Path(local_root).resolve()
        self.remote_root = remote_root.replace("\\", "/").rstrip("/") or "."
        self.workspace_id = workspace_id
        self.config = config or SyncConfig()
        self.state_store = state_store or SyncStateStore(self.local_root, self.config)

    def status(self) -> SyncPushResult:
        scan = self.scan()
        previous = self._last_manifest()
        plan = plan_push(
            local=scan.manifest,
            last_pushed=previous,
            skipped=scan.skipped,
            config=self.config,
        )
        return SyncPushResult(
            ok=plan.ok,
            workspace_id=self.workspace_id,
            plan=plan,
            manifest=scan.manifest,
        )

    def scan(self) -> ManifestScanResult:
        return scan_local_manifest(self.local_root, self.config)

    def push(self) -> SyncPushResult:
        scan = self.scan()
        previous = self._last_manifest()
        plan = plan_push(
            local=scan.manifest,
            last_pushed=previous,
            skipped=scan.skipped,
            config=self.config,
        )
        if plan.conflicts:
            raise SyncConflictError(
                "; ".join(f"{item.path}: {item.reason}" for item in plan.conflicts)
            )

        uploaded: list[str] = []
        for action in plan.uploads:
            path = action.path
            local_path = self.local_root / Path(path)
            remote_path = self.remote_path(path)
            self._ensure_remote_parent(remote_path)
            self.runtime.write_text(
                self.endpoint_id,
                remote_path,
                local_path.read_text(encoding="utf-8"),
            )
            uploaded.append(path)

        remote_manifest_path = self.remote_path(self.config.remote_manifest_path)
        self._ensure_remote_parent(remote_manifest_path)
        self.runtime.write_text(
            self.endpoint_id,
            remote_manifest_path,
            json.dumps(scan.manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        )

        state = SyncState(
            metadata=SyncRunMetadata(
                workspace_id=self.workspace_id,
                remote_root=self.remote_root,
                last_push_at=datetime.now(UTC).isoformat(timespec="seconds"),
                last_upload_count=len(uploaded),
                last_delete_count=len(plan.deletes),
            ),
            manifest=scan.manifest,
        )
        local_state_path = self.state_store.save(self.workspace_id, state)
        return SyncPushResult(
            ok=True,
            workspace_id=self.workspace_id,
            plan=plan,
            manifest=scan.manifest,
            uploaded=uploaded,
            deleted=[],
            local_state_path=str(local_state_path),
            remote_manifest_path=remote_manifest_path,
        )

    def push_file(self, relative_path: str) -> SyncPushResult:
        requested_path = _normalize_relative_sync_path(relative_path)
        scan = self.scan()
        previous = self._last_manifest()
        plan = plan_push(
            local=scan.manifest,
            last_pushed=previous,
            skipped=scan.skipped,
            config=self.config,
        )
        if plan.conflicts:
            raise SyncConflictError(
                "; ".join(f"{item.path}: {item.reason}" for item in plan.conflicts)
            )
        skipped = next((item for item in scan.skipped if item.path == requested_path), None)
        if skipped is not None:
            return SyncPushResult(
                ok=False,
                workspace_id=self.workspace_id,
                plan=plan,
                manifest=scan.manifest,
                requested_path=requested_path,
                skipped_reason=skipped.reason,
            )
        if requested_path not in scan.manifest.files:
            return SyncPushResult(
                ok=False,
                workspace_id=self.workspace_id,
                plan=plan,
                manifest=scan.manifest,
                requested_path=requested_path,
                skipped_reason="not_in_manifest",
            )

        uploaded: list[str] = []
        upload_paths = {action.path for action in plan.uploads}
        if requested_path in upload_paths:
            local_path = self.local_root / Path(requested_path)
            remote_path = self.remote_path(requested_path)
            self._ensure_remote_parent(remote_path)
            self.runtime.write_text(
                self.endpoint_id,
                remote_path,
                local_path.read_text(encoding="utf-8"),
            )
            uploaded.append(requested_path)

        remote_manifest_path = self.remote_path(self.config.remote_manifest_path)
        self._ensure_remote_parent(remote_manifest_path)
        self.runtime.write_text(
            self.endpoint_id,
            remote_manifest_path,
            json.dumps(scan.manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        )

        state = SyncState(
            metadata=SyncRunMetadata(
                workspace_id=self.workspace_id,
                remote_root=self.remote_root,
                last_push_at=datetime.now(UTC).isoformat(timespec="seconds"),
                last_upload_count=len(uploaded),
                last_delete_count=0,
            ),
            manifest=scan.manifest,
        )
        local_state_path = self.state_store.save(self.workspace_id, state)
        return SyncPushResult(
            ok=True,
            workspace_id=self.workspace_id,
            plan=plan,
            manifest=scan.manifest,
            uploaded=uploaded,
            deleted=[],
            local_state_path=str(local_state_path),
            remote_manifest_path=remote_manifest_path,
            requested_path=requested_path,
        )

    def remote_path(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").strip("/")
        if not normalized:
            return self.remote_root
        return f"{self.remote_root}/{normalized}" if self.remote_root != "." else normalized

    def _last_manifest(self) -> SyncManifest | None:
        state = self.state_store.load(self.workspace_id)
        return state.manifest if state is not None else None

    def _ensure_remote_parent(self, remote_path: str) -> None:
        parent = str(Path(remote_path.replace("\\", "/")).parent).replace("\\", "/")
        if parent and parent != ".":
            self.runtime.ensure_dir(self.endpoint_id, parent)


def _normalize_relative_sync_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized or ".." in [part for part in normalized.split("/") if part]:
        raise ValueError("sync path must be relative and stay inside the workspace")
    return normalized
