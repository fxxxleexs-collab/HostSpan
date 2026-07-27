from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from environment_runtime.core.errors import ValidationError
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.security import Principal, WriterLeaseService
from environment_runtime.services.session import SessionService
from environment_runtime.services.task import TaskService


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
            return {"status": "ok"}
        if method == "broker.status":
            return await self._status()
        if method.startswith("endpoint."):
            return await self._endpoint(method, params)
        if method.startswith("env."):
            return await self._environment(method, params)
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

    async def _endpoint(self, method: str, params: dict[str, Any]) -> Any:
        service = EndpointService(self.context)
        if method == "endpoint.add_local":
            return _json(await service.add_local(str(params["name"]), str(params["root"])))
        if method == "endpoint.add_ssh":
            return _json(
                await service.add_ssh(
                    name=str(params["name"]),
                    hostname=str(params["hostname"]),
                    username=str(params["username"]),
                    known_hosts_file=str(params["known_hosts_file"]),
                    port=int(params.get("port", 22)),
                    identity_file=params.get("identity_file"),
                    use_ssh_agent=bool(params.get("use_ssh_agent", True)),
                    proxy_jump=params.get("proxy_jump"),
                    connect_timeout=float(params.get("connect_timeout", 15.0)),
                    keepalive_interval=float(params.get("keepalive_interval", 20.0)),
                )
            )
        if method == "endpoint.list":
            return _json(await service.list_all())
        if method == "endpoint.health":
            return await service.health(str(params["endpoint_id"]))
        raise ValidationError(f"unknown broker method: {method}")

    async def _environment(self, method: str, params: dict[str, Any]) -> Any:
        service = EnvironmentService(self.context)
        if method == "env.create":
            return _json(
                await service.create(
                    str(params["name"]),
                    [str(item) for item in params["endpoint_ids"]],
                    [str(item) for item in params.get("workspace_ids", [])],
                )
            )
        if method == "env.get":
            return _json(await service.get(str(params["environment_id"])))
        if method == "env.list":
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
            return _json(
                await service.create(
                    environment_id=str(params["environment_id"]),
                    target_id=str(params["target_id"]),
                    argv=[str(item) for item in params["argv"]],
                    cwd=params.get("cwd"),
                    env={str(k): str(v) for k, v in dict(params.get("env", {})).items()},
                    backend=params.get("backend"),
                    cols=int(params.get("cols", 120)),
                    rows=int(params.get("rows", 30)),
                    term_type=str(params.get("term_type", "xterm-256color")),
                )
            )
        if method == "session.get":
            return _json(await service.get(str(params["session_id"])))
        if method == "session.list":
            return _json(await service.list_all())
        if method == "session.acquire_lease":
            lease = await WriterLeaseService(self.context).acquire(
                str(params["session_id"]),
                str(params.get("owner_type", principal.principal_type)),
                str(params.get("owner_id", principal.principal_id)),
                ttl_seconds=int(params.get("ttl_seconds", 300)),
                force=bool(params.get("force", False)),
            )
            return _json(lease)
        if method == "session.write":
            owner_id = str(params.get("owner_id", principal.principal_id))
            await WriterLeaseService(self.context).validate(str(params["session_id"]), owner_id)
            return _json(await service.write(str(params["session_id"]), str(params["data"])))
        if method == "session.resize":
            return _json(
                await service.resize(
                    str(params["session_id"]),
                    int(params["cols"]),
                    int(params["rows"]),
                )
            )
        if method == "session.terminate":
            return _json(await service.terminate(str(params["session_id"])))
        if method == "session.frames":
            return _json(
                await service.terminal_frames(
                    str(params["session_id"]),
                    params.get("after_seq"),
                    int(params.get("limit", 500)),
                )
            )
        if method == "session.tail":
            return await service.terminal_tail(
                str(params["session_id"]),
                int(params.get("limit_chars", 20_000)),
            )
        raise ValidationError(f"unknown broker method: {method}")

    async def _task(self, method: str, params: dict[str, Any]) -> Any:
        service = TaskService(self.context)
        if method == "task.start":
            return _json(
                await service.start(
                    environment_id=str(params["environment_id"]),
                    target_id=str(params["target_id"]),
                    argv=[str(item) for item in params["argv"]],
                    cwd=params.get("cwd"),
                    env={str(k): str(v) for k, v in dict(params.get("env", {})).items()},
                    persistent=bool(params.get("persistent", False)),
                )
            )
        if method == "task.get":
            return _json(await service.get(str(params["task_id"])))
        if method == "task.list":
            return _json(await service.list_all())
        if method == "task.logs":
            return await service.logs(str(params["task_id"]))
        if method == "task.cancel":
            return _json(await service.cancel(str(params["task_id"])))
        raise ValidationError(f"unknown broker method: {method}")


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    return value
