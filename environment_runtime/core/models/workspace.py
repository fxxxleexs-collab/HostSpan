from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from ..events import ExposurePolicy
from ..ids import new_id


class WriteAuthority(StrEnum):
    READ_ONLY = "READ_ONLY"
    RUNTIME = "RUNTIME"
    USER = "USER"


class ReplicaState(StrEnum):
    UNKNOWN = "UNKNOWN"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    DIRTY = "DIRTY"
    CONFLICTED = "CONFLICTED"
    UNAVAILABLE = "UNAVAILABLE"


class BindingMode(StrEnum):
    REMOTE_NATIVE = "REMOTE_NATIVE"
    ONE_WAY_MIRROR = "ONE_WAY_MIRROR"
    SNAPSHOT_UPLOAD = "SNAPSHOT_UPLOAD"
    ARTIFACT_EXPORT = "ARTIFACT_EXPORT"


class WorkspaceRoot(BaseModel):
    root_id: str = Field(default_factory=lambda: new_id("root"))
    logical_path: str
    authority: WriteAuthority = WriteAuthority.USER
    exposure: ExposurePolicy = ExposurePolicy.USER


class WorkspaceReplica(BaseModel):
    replica_id: str = Field(default_factory=lambda: new_id("replica"))
    workspace_id: str
    endpoint_id: str
    physical_root: str
    revision: str | None = None
    state: ReplicaState = ReplicaState.UNKNOWN


class WorkspaceBinding(BaseModel):
    binding_id: str = Field(default_factory=lambda: new_id("binding"))
    source_replica_id: str
    target_replica_id: str
    mode: BindingMode
    authority: WriteAuthority = WriteAuthority.RUNTIME
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class Workspace(BaseModel):
    workspace_id: str = Field(default_factory=lambda: new_id("workspace"))
    name: str
    roots: list[WorkspaceRoot] = Field(default_factory=list)
    replicas: list[WorkspaceReplica] = Field(default_factory=list)
    bindings: list[WorkspaceBinding] = Field(default_factory=list)
    current_revision: str | None = None
