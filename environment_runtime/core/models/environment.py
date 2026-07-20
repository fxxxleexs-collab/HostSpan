from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from ..capabilities import Capability
from ..ids import new_id


class EnvironmentState(StrEnum):
    DECLARED = "DECLARED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    FAILED = "FAILED"


class ExecutionTarget(BaseModel):
    target_id: str = Field(default_factory=lambda: new_id("target"))
    endpoint_id: str
    provider: str
    default_cwd: str | None = None
    environment_variables: dict[str, str] = Field(default_factory=dict)
    capabilities: set[Capability] = Field(default_factory=set)


class Environment(BaseModel):
    environment_id: str = Field(default_factory=lambda: new_id("environment"))
    name: str
    endpoint_ids: list[str] = Field(default_factory=list)
    workspace_ids: list[str] = Field(default_factory=list)
    execution_targets: list[ExecutionTarget] = Field(default_factory=list)
    default_execution_target_id: str | None = None
    default_session_backend: str | None = None
    required_capabilities: set[Capability] = Field(default_factory=set)
    status: EnvironmentState = EnvironmentState.DECLARED
