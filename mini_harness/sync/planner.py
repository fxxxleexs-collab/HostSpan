from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from mini_harness.sync.config import SyncConfig
from mini_harness.sync.manifest import SkippedFile, SyncManifest

SyncActionKind = Literal["upload", "delete_remote"]


class SyncAction(BaseModel):
    kind: SyncActionKind
    path: str
    sha256: str | None = None
    size: int | None = None


class SyncConflict(BaseModel):
    path: str
    reason: str


class SyncPlan(BaseModel):
    mode: str = "push"
    uploads: list[SyncAction] = Field(default_factory=list)
    deletes: list[SyncAction] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    skipped: list[SkippedFile] = Field(default_factory=list)
    conflicts: list[SyncConflict] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.uploads or self.deletes)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def plan_push(
    *,
    local: SyncManifest,
    last_pushed: SyncManifest | None = None,
    remote: SyncManifest | None = None,
    skipped: list[SkippedFile] | None = None,
    config: SyncConfig | None = None,
) -> SyncPlan:
    sync_config = config or SyncConfig()
    plan = SyncPlan(skipped=skipped or [])
    previous = last_pushed or SyncManifest()

    _detect_remote_conflicts(plan, previous, remote)
    if plan.conflicts:
        return plan

    for path, record in sorted(local.files.items()):
        old = previous.files.get(path)
        if old is not None and old.sha256 == record.sha256:
            plan.unchanged.append(path)
            continue
        plan.uploads.append(
            SyncAction(kind="upload", path=path, sha256=record.sha256, size=record.size)
        )

    if sync_config.delete_remote:
        for path in sorted(set(previous.files) - set(local.files)):
            plan.deletes.append(SyncAction(kind="delete_remote", path=path))
    return plan


def _detect_remote_conflicts(
    plan: SyncPlan,
    last_pushed: SyncManifest,
    remote: SyncManifest | None,
) -> None:
    if remote is None:
        return
    all_paths = set(last_pushed.files) | set(remote.files)
    for path in sorted(all_paths):
        old = last_pushed.files.get(path)
        current = remote.files.get(path)
        if old is None and current is not None:
            plan.conflicts.append(
                SyncConflict(path=path, reason="remote file is not in last pushed manifest")
            )
            continue
        if old is not None and current is None:
            plan.conflicts.append(SyncConflict(path=path, reason="remote file was deleted"))
            continue
        if old is not None and current is not None and old.sha256 != current.sha256:
            plan.conflicts.append(SyncConflict(path=path, reason="remote file changed"))
