from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EmptyParams(BaseModel):
    pass


class PutSecretParams(BaseModel):
    value: str = Field(min_length=1)
    purpose: str = "runtime"


class SecretRefParams(BaseModel):
    secret_ref: str


class AddLocalEndpointParams(BaseModel):
    name: str
    root: str


class AddSSHEndpointParams(BaseModel):
    name: str
    hostname: str
    username: str
    known_hosts_file: str
    port: int = Field(default=22, ge=1, le=65535)
    auth_method: Literal["auto", "agent", "key", "password"] = "auto"
    identity_file: str | None = None
    password_secret_ref: str | None = None
    use_ssh_agent: bool = True
    proxy_jump: str | None = None
    connect_timeout: float = Field(default=300.0, gt=0)
    keepalive_interval: float = Field(default=20.0, gt=0)


class EndpointIdParams(BaseModel):
    endpoint_id: str


class CreateEnvironmentParams(BaseModel):
    name: str
    endpoint_ids: list[str] = Field(min_length=1)
    workspace_ids: list[str] = Field(default_factory=list)


class EnsureLocalEnvironmentParams(BaseModel):
    name: str
    root: str


class EnsureSSHEnvironmentParams(BaseModel):
    name: str
    hostname: str
    username: str
    known_hosts_file: str
    port: int = Field(default=22, ge=1, le=65535)
    auth_method: Literal["auto", "agent", "key", "password"] = "auto"
    identity_file: str | None = None
    password_secret_ref: str | None = None
    use_ssh_agent: bool = True
    proxy_jump: str | None = None
    connect_timeout: float = Field(default=300.0, gt=0)
    keepalive_interval: float = Field(default=20.0, gt=0)


class EnvironmentIdParams(BaseModel):
    environment_id: str


class CreateSessionParams(BaseModel):
    environment_id: str
    target_id: str
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    backend: str | None = None
    cols: int = Field(default=120, gt=0)
    rows: int = Field(default=30, gt=0)
    term_type: str = "xterm-256color"


class SessionIdParams(BaseModel):
    session_id: str


class AcquireLeaseParams(BaseModel):
    session_id: str
    owner_type: str | None = None
    owner_id: str | None = None
    ttl_seconds: int = Field(default=300, gt=0)
    force: bool = False


class WriteSessionParams(BaseModel):
    session_id: str
    data: str
    owner_id: str | None = None


class ResizeSessionParams(BaseModel):
    session_id: str
    cols: int = Field(gt=0)
    rows: int = Field(gt=0)


class SessionFramesParams(BaseModel):
    session_id: str
    after_seq: int | None = None
    limit: int = Field(default=500, gt=0, le=5000)


class SessionTailParams(BaseModel):
    session_id: str
    limit_chars: int = Field(default=20_000, gt=0, le=1_000_000)


class StartTaskParams(BaseModel):
    environment_id: str
    target_id: str
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    persistent: bool = False


class TaskIdParams(BaseModel):
    task_id: str


class CreateWorkspaceParams(BaseModel):
    name: str


class WorkspaceIdParams(BaseModel):
    workspace_id: str


class AddWorkspaceRootParams(BaseModel):
    workspace_id: str
    logical_path: str


class AddWorkspaceReplicaParams(BaseModel):
    workspace_id: str
    endpoint_id: str
    physical_root: str


class BindWorkspaceParams(BaseModel):
    workspace_id: str
    source_replica_id: str
    target_replica_id: str
    mode: str = "ONE_WAY_MIRROR"


class WorkspaceRevisionParams(BaseModel):
    workspace_id: str
    replica_id: str


class WorkspaceSyncParams(BaseModel):
    workspace_id: str
    binding_id: str


class FilePathParams(BaseModel):
    endpoint_id: str
    path: str


class FileReadTextParams(FilePathParams):
    encoding: str = "utf-8"


class FileWriteTextParams(FilePathParams):
    text: str
    encoding: str = "utf-8"


class FileReadBytesParams(FilePathParams):
    pass


class FileWriteBytesParams(FilePathParams):
    data_base64: str


class FileListParams(FilePathParams):
    recursive: bool = False


class FileStatParams(FilePathParams):
    pass


class FileRemoveParams(FilePathParams):
    pass


class FileMkdirParams(FilePathParams):
    pass


class FileSha256Params(FilePathParams):
    pass


class EventSubscribeParams(BaseModel):
    after_sequence: int = Field(default=0, ge=0)
    resource_type: str | None = None
    resource_id: str | None = None
    event_types: list[str] = Field(default_factory=list)
    max_items: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    heartbeat_seconds: float = Field(default=15.0, gt=0)


class SessionSubscribeFramesParams(BaseModel):
    session_id: str
    after_seq: int | None = None
    max_items: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    heartbeat_seconds: float = Field(default=15.0, gt=0)


class CanonicalCommand(BaseModel):
    method: str
    group: Literal["broker", "endpoint", "env", "workspace", "file", "session", "task"]
    params_schema: str
    aliases: list[str] = Field(default_factory=list)
    stream: bool = False


def parse_params(model: type[BaseModel], params: dict[str, Any]) -> Any:
    return model.model_validate(params)
