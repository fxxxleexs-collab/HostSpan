from __future__ import annotations

import base64
import binascii
import stat as stat_module
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from environment_runtime.broker.schemas import (
    AcquireLeaseParams,
    AddLocalEndpointParams,
    AddSSHEndpointParams,
    AddWorkspaceReplicaParams,
    AddWorkspaceRootParams,
    BindWorkspaceParams,
    CreateEnvironmentParams,
    CreateSessionParams,
    CreateWorkspaceParams,
    EmptyParams,
    EndpointIdParams,
    EnsureLocalEnvironmentParams,
    EnsureSSHEnvironmentParams,
    EnvironmentIdParams,
    FileListParams,
    FileMkdirParams,
    FilePathParams,
    FileReadBytesParams,
    FileReadTextParams,
    FileRemoveParams,
    FileSha256Params,
    FileStatParams,
    FileWriteBytesParams,
    FileWriteTextParams,
    ResizeSessionParams,
    SessionFramesParams,
    SessionIdParams,
    SessionTailParams,
    StartTaskParams,
    TaskIdParams,
    WorkspaceIdParams,
    WorkspaceRevisionParams,
    WorkspaceSyncParams,
    WriteSessionParams,
    parse_params,
)
from environment_runtime.core.errors import ValidationError
from environment_runtime.core.models import BindingMode, Endpoint
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.security import Principal, WriterLeaseService
from environment_runtime.services.session import SessionService
from environment_runtime.services.task import TaskService
from environment_runtime.services.workspace import WorkspaceService

CANONICAL_COMMANDS: dict[str, dict[str, Any]] = {
    "broker.ping": {"group": "broker", "params_schema": "EmptyParams"},
    "broker.status": {"group": "broker", "params_schema": "EmptyParams"},
    "broker.commands": {"group": "broker", "params_schema": "EmptyParams"},
    "broker.shutdown": {"group": "broker", "params_schema": "EmptyParams"},
    "event.subscribe": {
        "group": "broker",
        "params_schema": "EventSubscribeParams",
        "stream": True,
    },
    "endpoint.add_local": {"group": "endpoint", "params_schema": "AddLocalEndpointParams"},
    "endpoint.add_ssh": {"group": "endpoint", "params_schema": "AddSSHEndpointParams"},
    "endpoint.list": {"group": "endpoint", "params_schema": "EmptyParams"},
    "endpoint.health": {"group": "endpoint", "params_schema": "EndpointIdParams"},
    "env.create": {"group": "env", "params_schema": "CreateEnvironmentParams"},
    "env.ensure_local": {"group": "env", "params_schema": "EnsureLocalEnvironmentParams"},
    "env.ensure_ssh": {"group": "env", "params_schema": "EnsureSSHEnvironmentParams"},
    "env.get": {"group": "env", "params_schema": "EnvironmentIdParams"},
    "env.list": {"group": "env", "params_schema": "EmptyParams"},
    "workspace.create": {"group": "workspace", "params_schema": "CreateWorkspaceParams"},
    "workspace.get": {"group": "workspace", "params_schema": "WorkspaceIdParams"},
    "workspace.list": {"group": "workspace", "params_schema": "EmptyParams"},
    "workspace.add_root": {"group": "workspace", "params_schema": "AddWorkspaceRootParams"},
    "workspace.add_replica": {"group": "workspace", "params_schema": "AddWorkspaceReplicaParams"},
    "workspace.bind": {"group": "workspace", "params_schema": "BindWorkspaceParams"},
    "workspace.revision": {"group": "workspace", "params_schema": "WorkspaceRevisionParams"},
    "workspace.sync": {"group": "workspace", "params_schema": "WorkspaceSyncParams"},
    "file.exists": {"group": "file", "params_schema": "FilePathParams"},
    "file.stat": {"group": "file", "params_schema": "FileStatParams"},
    "file.list": {"group": "file", "params_schema": "FileListParams"},
    "file.mkdir": {"group": "file", "params_schema": "FileMkdirParams"},
    "file.remove": {"group": "file", "params_schema": "FileRemoveParams"},
    "file.sha256": {"group": "file", "params_schema": "FileSha256Params"},
    "file.read_text": {"group": "file", "params_schema": "FileReadTextParams"},
    "file.write_text": {"group": "file", "params_schema": "FileWriteTextParams"},
    "file.read_bytes": {"group": "file", "params_schema": "FileReadBytesParams"},
    "file.write_bytes": {"group": "file", "params_schema": "FileWriteBytesParams"},
    "session.create": {"group": "session", "params_schema": "CreateSessionParams"},
    "session.get": {"group": "session", "params_schema": "SessionIdParams"},
    "session.list": {"group": "session", "params_schema": "EmptyParams"},
    "session.acquire_lease": {"group": "session", "params_schema": "AcquireLeaseParams"},
    "session.write": {"group": "session", "params_schema": "WriteSessionParams"},
    "session.resize": {"group": "session", "params_schema": "ResizeSessionParams"},
    "session.terminate": {"group": "session", "params_schema": "SessionIdParams"},
    "session.frames": {"group": "session", "params_schema": "SessionFramesParams"},
    "session.tail": {"group": "session", "params_schema": "SessionTailParams"},
    "session.subscribe_frames": {
        "group": "session",
        "params_schema": "SessionSubscribeFramesParams",
        "stream": True,
    },
    "task.start": {"group": "task", "params_schema": "StartTaskParams"},
    "task.get": {"group": "task", "params_schema": "TaskIdParams"},
    "task.list": {"group": "task", "params_schema": "EmptyParams"},
    "task.logs": {"group": "task", "params_schema": "TaskIdParams"},
    "task.cancel": {"group": "task", "params_schema": "TaskIdParams"},
}


class RuntimeCommandHandler:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def handle(
        self,
        method: str,
        params: dict[str, Any],
        principal: Principal | None = None,
    ) -> Any:
        principal = principal or Principal(principal_id="trusted-local", principal_type="system")
        if method == "broker.ping":
            parse_params(EmptyParams, params)
            return {"status": "ok"}
        if method == "broker.status":
            parse_params(EmptyParams, params)
            return await self._status()
        if method == "broker.commands":
            parse_params(EmptyParams, params)
            return self._commands()
        if method.startswith("endpoint."):
            return await self._endpoint(method, params)
        if method.startswith("env."):
            return await self._environment(method, params)
        if method.startswith("workspace."):
            return await self._workspace(method, params)
        if method.startswith("file."):
            return await self._file(method, params)
        if method.startswith("session."):
            return await self._session(method, params, principal)
        if method.startswith("task."):
            return await self._task(method, params)
        raise ValidationError(f"unknown broker method: {method}")

    async def _status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "active_tasks": len(self.context.active.task_handles),
            "active_sessions": len(self.context.active.session_handles),
        }

    def _commands(self) -> list[dict[str, Any]]:
        return [
            {"method": method, **metadata}
            for method, metadata in sorted(CANONICAL_COMMANDS.items())
        ]

    async def _endpoint(self, method: str, params: dict[str, Any]) -> Any:
        service = EndpointService(self.context)
        if method == "endpoint.add_local":
            data = parse_params(AddLocalEndpointParams, params)
            return _json(await service.add_local(data.name, data.root))
        if method == "endpoint.add_ssh":
            data = parse_params(AddSSHEndpointParams, params)
            return _json(
                await service.add_ssh(
                    name=data.name,
                    hostname=data.hostname,
                    username=data.username,
                    known_hosts_file=data.known_hosts_file,
                    port=data.port,
                    identity_file=data.identity_file,
                    use_ssh_agent=data.use_ssh_agent,
                    proxy_jump=data.proxy_jump,
                    connect_timeout=data.connect_timeout,
                    keepalive_interval=data.keepalive_interval,
                )
            )
        if method == "endpoint.list":
            parse_params(EmptyParams, params)
            return _json(await service.list_all())
        if method == "endpoint.health":
            data = parse_params(EndpointIdParams, params)
            return await service.health(data.endpoint_id)
        raise ValidationError(f"unknown broker method: {method}")

    async def _environment(self, method: str, params: dict[str, Any]) -> Any:
        service = EnvironmentService(self.context)
        if method == "env.create":
            data = parse_params(CreateEnvironmentParams, params)
            return _json(
                await service.create(
                    data.name,
                    data.endpoint_ids,
                    data.workspace_ids,
                )
            )
        if method == "env.ensure_local":
            data = parse_params(EnsureLocalEnvironmentParams, params)
            endpoint = await self._ensure_local_endpoint(data.name, data.root)
            environment = await self._ensure_environment(data.name, endpoint.endpoint_id)
            return _json(
                {
                    "endpoint": endpoint,
                    "environment": environment,
                    "target_id": environment.default_execution_target_id,
                }
            )
        if method == "env.ensure_ssh":
            data = parse_params(EnsureSSHEnvironmentParams, params)
            endpoint = await self._ensure_ssh_endpoint(data)
            environment = await self._ensure_environment(data.name, endpoint.endpoint_id)
            return _json(
                {
                    "endpoint": endpoint,
                    "environment": environment,
                    "target_id": environment.default_execution_target_id,
                }
            )
        if method == "env.get":
            data = parse_params(EnvironmentIdParams, params)
            return _json(await service.get(data.environment_id))
        if method == "env.list":
            parse_params(EmptyParams, params)
            return _json(await service.list_all())
        raise ValidationError(f"unknown broker method: {method}")

    async def _session(
        self,
        method: str,
        params: dict[str, Any],
        principal: Principal,
    ) -> Any:
        service = SessionService(self.context)
        if method == "session.create":
            data = parse_params(CreateSessionParams, params)
            return _json(
                await service.create(
                    environment_id=data.environment_id,
                    target_id=data.target_id,
                    argv=data.argv,
                    cwd=data.cwd,
                    env=data.env,
                    backend=data.backend,
                    cols=data.cols,
                    rows=data.rows,
                    term_type=data.term_type,
                )
            )
        if method == "session.get":
            data = parse_params(SessionIdParams, params)
            return _json(await service.get(data.session_id))
        if method == "session.list":
            parse_params(EmptyParams, params)
            return _json(await service.list_all())
        if method == "session.acquire_lease":
            data = parse_params(AcquireLeaseParams, params)
            lease = await WriterLeaseService(self.context).acquire(
                data.session_id,
                data.owner_type or principal.principal_type,
                data.owner_id or principal.principal_id,
                ttl_seconds=data.ttl_seconds,
                force=data.force,
            )
            return _json(lease)
        if method == "session.write":
            data = parse_params(WriteSessionParams, params)
            owner_id = data.owner_id or principal.principal_id
            await WriterLeaseService(self.context).validate(data.session_id, owner_id)
            return _json(await service.write(data.session_id, data.data))
        if method == "session.resize":
            data = parse_params(ResizeSessionParams, params)
            return _json(await service.resize(data.session_id, data.cols, data.rows))
        if method == "session.terminate":
            data = parse_params(SessionIdParams, params)
            return _json(await service.terminate(data.session_id))
        if method == "session.frames":
            data = parse_params(SessionFramesParams, params)
            return _json(
                await service.terminal_frames(
                    data.session_id,
                    data.after_seq,
                    data.limit,
                )
            )
        if method == "session.tail":
            data = parse_params(SessionTailParams, params)
            return await service.terminal_tail(data.session_id, data.limit_chars)
        raise ValidationError(f"unknown broker method: {method}")

    async def _task(self, method: str, params: dict[str, Any]) -> Any:
        service = TaskService(self.context)
        if method == "task.start":
            data = parse_params(StartTaskParams, params)
            return _json(
                await service.start(
                    environment_id=data.environment_id,
                    target_id=data.target_id,
                    argv=data.argv,
                    cwd=data.cwd,
                    env=data.env,
                    persistent=data.persistent,
                )
            )
        if method == "task.get":
            data = parse_params(TaskIdParams, params)
            return _json(await service.get(data.task_id))
        if method == "task.list":
            parse_params(EmptyParams, params)
            return _json(await service.list_all())
        if method == "task.logs":
            data = parse_params(TaskIdParams, params)
            return await service.logs(data.task_id)
        if method == "task.cancel":
            data = parse_params(TaskIdParams, params)
            return _json(await service.cancel(data.task_id))
        raise ValidationError(f"unknown broker method: {method}")

    async def _workspace(self, method: str, params: dict[str, Any]) -> Any:
        service = WorkspaceService(self.context)
        if method == "workspace.create":
            data = parse_params(CreateWorkspaceParams, params)
            return _json(await service.create(data.name))
        if method == "workspace.get":
            data = parse_params(WorkspaceIdParams, params)
            return _json(await service.get(data.workspace_id))
        if method == "workspace.list":
            parse_params(EmptyParams, params)
            return _json(await service.list_all())
        if method == "workspace.add_root":
            data = parse_params(AddWorkspaceRootParams, params)
            return _json(await service.add_root(data.workspace_id, data.logical_path))
        if method == "workspace.add_replica":
            data = parse_params(AddWorkspaceReplicaParams, params)
            return _json(
                await service.add_replica(data.workspace_id, data.endpoint_id, data.physical_root)
            )
        if method == "workspace.bind":
            data = parse_params(BindWorkspaceParams, params)
            return _json(
                await service.bind(
                    data.workspace_id,
                    data.source_replica_id,
                    data.target_replica_id,
                    BindingMode(data.mode),
                )
            )
        if method == "workspace.revision":
            data = parse_params(WorkspaceRevisionParams, params)
            return _json(await service.create_revision(data.workspace_id, data.replica_id))
        if method == "workspace.sync":
            data = parse_params(WorkspaceSyncParams, params)
            return _json(await service.sync(data.workspace_id, data.binding_id))
        raise ValidationError(f"unknown broker method: {method}")

    async def _file(self, method: str, params: dict[str, Any]) -> Any:
        if method == "file.exists":
            data = parse_params(FilePathParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            return {"exists": await self._file_exists(endpoint, data.path)}
        if method == "file.stat":
            data = parse_params(FileStatParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            return await self._file_stat(endpoint, data.path)
        if method == "file.list":
            data = parse_params(FileListParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            return {"entries": await self._file_list(endpoint, data.path, data.recursive)}
        if method == "file.mkdir":
            data = parse_params(FileMkdirParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            await self._file_mkdir(endpoint, data.path)
            return {"endpoint_id": endpoint.endpoint_id, "path": data.path}
        if method == "file.remove":
            data = parse_params(FileRemoveParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            await self._file_remove(endpoint, data.path)
            return {"endpoint_id": endpoint.endpoint_id, "path": data.path}
        if method == "file.sha256":
            data = parse_params(FileSha256Params, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            return {"sha256": await self._file_sha256(endpoint, data.path)}
        if method == "file.read_text":
            data = parse_params(FileReadTextParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            content = await self._file_read_bytes(endpoint, data.path)
            return {
                "text": content.decode(data.encoding),
                "encoding": data.encoding,
                "size": len(content),
            }
        if method == "file.write_text":
            data = parse_params(FileWriteTextParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            payload = data.text.encode(data.encoding)
            await self._file_write_bytes(endpoint, data.path, payload)
            return {"endpoint_id": endpoint.endpoint_id, "path": data.path, "size": len(payload)}
        if method == "file.read_bytes":
            data = parse_params(FileReadBytesParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            content = await self._file_read_bytes(endpoint, data.path)
            return {"data_base64": base64.b64encode(content).decode("ascii"), "size": len(content)}
        if method == "file.write_bytes":
            data = parse_params(FileWriteBytesParams, params)
            endpoint = await self._get_endpoint(data.endpoint_id)
            try:
                payload = base64.b64decode(data.data_base64, validate=True)
            except binascii.Error as exc:
                raise ValidationError("data_base64 is not valid base64") from exc
            await self._file_write_bytes(endpoint, data.path, payload)
            return {"endpoint_id": endpoint.endpoint_id, "path": data.path, "size": len(payload)}
        raise ValidationError(f"unknown broker method: {method}")

    async def _get_endpoint(self, endpoint_id: str) -> Endpoint:
        endpoint = await self.context.endpoints.get(endpoint_id)
        if endpoint is None:
            raise ValidationError(f"endpoint {endpoint_id} was not found")
        return endpoint

    async def _ensure_local_endpoint(self, name: str, root: str) -> Endpoint:
        for endpoint in await self.context.endpoints.list():
            if (
                endpoint.name == name
                and endpoint.provider_type == "local"
                and endpoint.config.get("root") == root
            ):
                return endpoint
        return await EndpointService(self.context).add_local(name, root)

    async def _ensure_ssh_endpoint(self, data: EnsureSSHEnvironmentParams) -> Endpoint:
        expected = {
            "hostname": data.hostname,
            "port": data.port,
            "username": data.username,
            "known_hosts_file": data.known_hosts_file,
            "identity_file": data.identity_file,
            "use_ssh_agent": data.use_ssh_agent,
            "proxy_jump": data.proxy_jump,
            "connect_timeout": data.connect_timeout,
            "keepalive_interval": data.keepalive_interval,
        }
        for endpoint in await self.context.endpoints.list():
            if endpoint.name != data.name or endpoint.provider_type != "ssh":
                continue
            if all(endpoint.config.get(key) == value for key, value in expected.items()):
                return endpoint
        return await EndpointService(self.context).add_ssh(
            name=data.name,
            hostname=data.hostname,
            username=data.username,
            known_hosts_file=data.known_hosts_file,
            port=data.port,
            identity_file=data.identity_file,
            use_ssh_agent=data.use_ssh_agent,
            proxy_jump=data.proxy_jump,
            connect_timeout=data.connect_timeout,
            keepalive_interval=data.keepalive_interval,
        )

    async def _ensure_environment(self, name: str, endpoint_id: str):
        for environment in await self.context.environments.list():
            if environment.name == name and endpoint_id in environment.endpoint_ids:
                return environment
        return await EnvironmentService(self.context).create(name, [endpoint_id])

    async def _file_exists(self, endpoint: Endpoint, path: str) -> bool:
        if endpoint.provider_type == "local":
            return await self.context.providers.filesystem["local"].exists(
                _resolve_local_path(endpoint, path)
            )
        if endpoint.provider_type == "ssh":
            return await self.context.providers.filesystem["sftp"].exists(endpoint, path)
        raise ValidationError(f"unsupported endpoint type for file.exists: {endpoint.provider_type}")

    async def _file_stat(self, endpoint: Endpoint, path: str) -> dict[str, Any]:
        if endpoint.provider_type == "local":
            resolved = _resolve_local_path(endpoint, path)
            stat_result = resolved.stat()
            return {
                "size": stat_result.st_size,
                "permissions": stat_result.st_mode,
                "mtime": stat_result.st_mtime,
                "is_dir": stat_module.S_ISDIR(stat_result.st_mode),
            }
        if endpoint.provider_type == "ssh":
            return await self.context.providers.filesystem["sftp"].stat(endpoint, path)
        raise ValidationError(f"unsupported endpoint type for file.stat: {endpoint.provider_type}")

    async def _file_list(self, endpoint: Endpoint, path: str, recursive: bool) -> list[str]:
        if endpoint.provider_type == "local":
            resolved = _resolve_local_path(endpoint, path)
            if recursive:
                files = await self.context.providers.filesystem["local"].walk_files(resolved)
                return [file.relative_to(resolved).as_posix() for file in files]
            return sorted(child.name for child in resolved.iterdir())
        if endpoint.provider_type == "ssh":
            if recursive:
                return await self.context.providers.filesystem["sftp"].walk_files(endpoint, path)
            return await self.context.providers.filesystem["sftp"].list(endpoint, path)
        raise ValidationError(f"unsupported endpoint type for file.list: {endpoint.provider_type}")

    async def _file_mkdir(self, endpoint: Endpoint, path: str) -> None:
        if endpoint.provider_type == "local":
            await self.context.providers.filesystem["local"].ensure_dir(
                _resolve_local_path(endpoint, path)
            )
            return
        if endpoint.provider_type == "ssh":
            await self.context.providers.filesystem["sftp"].ensure_dir(endpoint, path)
            return
        raise ValidationError(f"unsupported endpoint type for file.mkdir: {endpoint.provider_type}")

    async def _file_remove(self, endpoint: Endpoint, path: str) -> None:
        if endpoint.provider_type == "local":
            resolved = _resolve_local_path(endpoint, path)
            if resolved.is_dir():
                resolved.rmdir()
            else:
                resolved.unlink()
            return
        if endpoint.provider_type == "ssh":
            await self.context.providers.filesystem["sftp"].remove(endpoint, path)
            return
        raise ValidationError(f"unsupported endpoint type for file.remove: {endpoint.provider_type}")

    async def _file_sha256(self, endpoint: Endpoint, path: str) -> str:
        if endpoint.provider_type == "local":
            return await self.context.providers.filesystem["local"].sha256(
                _resolve_local_path(endpoint, path)
            )
        if endpoint.provider_type == "ssh":
            return await self.context.providers.filesystem["sftp"].sha256(endpoint, path)
        raise ValidationError(f"unsupported endpoint type for file.sha256: {endpoint.provider_type}")

    async def _file_read_bytes(self, endpoint: Endpoint, path: str) -> bytes:
        if endpoint.provider_type == "local":
            return await self.context.providers.filesystem["local"].read_bytes(
                _resolve_local_path(endpoint, path)
            )
        if endpoint.provider_type == "ssh":
            return await self.context.providers.filesystem["sftp"].read_bytes(endpoint, path)
        raise ValidationError(f"unsupported endpoint type for file.read: {endpoint.provider_type}")

    async def _file_write_bytes(self, endpoint: Endpoint, path: str, data: bytes) -> None:
        if endpoint.provider_type == "local":
            await self.context.providers.filesystem["local"].write_bytes(
                _resolve_local_path(endpoint, path),
                data,
            )
            return
        if endpoint.provider_type == "ssh":
            await self.context.providers.filesystem["sftp"].write_bytes(endpoint, path, data)
            return
        raise ValidationError(f"unsupported endpoint type for file.write: {endpoint.provider_type}")


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    return value


def _resolve_local_path(endpoint: Endpoint, path: str) -> Path:
    root_value = endpoint.config.get("root")
    if not isinstance(root_value, str) or not root_value:
        raise ValidationError(f"local endpoint {endpoint.endpoint_id} has no root")
    root = Path(root_value).resolve()
    requested = Path(path)
    resolved = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    if not resolved.is_relative_to(root):
        raise ValidationError("local file path must stay within the endpoint root")
    return resolved
