from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateLocalEndpointRequest(BaseModel):
    name: str
    root: str


class CreateSSHEndpointRequest(BaseModel):
    name: str
    hostname: str
    username: str
    known_hosts_file: str
    port: int = 22
    auth_method: Literal["auto", "agent", "key", "password"] = "auto"
    identity_file: str | None = None
    password_secret_ref: str | None = None
    use_ssh_agent: bool = True
    proxy_jump: str | None = None
    connect_timeout: float = 300.0
    keepalive_interval: float = 20.0


class CreateEnvironmentRequest(BaseModel):
    name: str
    endpoint_ids: list[str]
    workspace_ids: list[str] = Field(default_factory=list)


class CreateWorkspaceRequest(BaseModel):
    name: str


class AddWorkspaceRootRequest(BaseModel):
    logical_path: str


class AddWorkspaceReplicaRequest(BaseModel):
    endpoint_id: str
    physical_root: str


class BindWorkspaceRequest(BaseModel):
    source_replica_id: str
    target_replica_id: str
    mode: str = "ONE_WAY_MIRROR"


class StartTaskRequest(BaseModel):
    environment_id: str
    target_id: str
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    persistent: bool = False


class CreateSessionRequest(BaseModel):
    environment_id: str
    target_id: str
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    backend: str | None = None
    cols: int = 120
    rows: int = 30
    term_type: str = "xterm-256color"


class WriteSessionRequest(BaseModel):
    owner_id: str
    data: str


class ResizeSessionRequest(BaseModel):
    cols: int
    rows: int


class CreateInputRequestRequest(BaseModel):
    session_id: str
    input_type: str
    prompt: str | None = None
    task_id: str | None = None
    allowed_values: list[str] | None = None


class SubmitInputRequest(BaseModel):
    owner_id: str
    value: str


class AcquireLeaseRequest(BaseModel):
    session_id: str
    owner_type: str
    owner_id: str
    ttl_seconds: int = 300
    force: bool = False


class RegisterArtifactRequest(BaseModel):
    workspace_id: str
    root_id: str
    relative_path: str = ""
    task_id: str | None = None
    media_type: str | None = None


class DownloadArtifactRequest(BaseModel):
    destination: str
