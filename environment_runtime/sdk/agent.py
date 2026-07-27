from __future__ import annotations

import builtins
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from environment_runtime.broker import BrokerAddress
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk.transport import BrokerTransport, RuntimeTransport


class AgentRuntimeClient:
    """Agent-facing facade over the runtime command surface."""

    def __init__(self, transport: RuntimeTransport) -> None:
        self.transport = transport
        self.broker = BrokerNamespace(transport)
        self.endpoints = EndpointNamespace(transport)
        self.environments = EnvironmentNamespace(transport)
        self.tasks = TaskNamespace(transport)
        self.sessions = SessionNamespace(transport)

    @classmethod
    def from_broker(
        cls,
        address: BrokerAddress | None = None,
        settings: RuntimeSettings | None = None,
        token: str | None = None,
        principal_id: str = "agent",
        principal_type: str = "agent",
        scope_id: str = "default",
    ) -> AgentRuntimeClient:
        return cls(
            BrokerTransport(
                address=address,
                settings=settings,
                token=token,
                principal_id=principal_id,
                principal_type=principal_type,
                scope_id=scope_id,
            )
        )

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> AgentRuntimeClient:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class BrokerNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def ping(self) -> dict[str, Any]:
        return self._transport.request("broker.ping")

    def status(self) -> dict[str, Any]:
        return self._transport.request("broker.status")

    def shutdown(self) -> dict[str, Any]:
        return self._transport.request("broker.shutdown")

    def events(
        self,
        after_sequence: int = 0,
        resource_type: str | None = None,
        resource_id: str | None = None,
        event_types: list[str] | None = None,
        max_items: int | None = None,
        timeout_seconds: float | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "after_sequence": after_sequence,
            "heartbeat_seconds": heartbeat_seconds,
        }
        _set_optional(params, "resource_type", resource_type)
        _set_optional(params, "resource_id", resource_id)
        _set_optional(params, "event_types", event_types)
        _set_optional(params, "max_items", max_items)
        _set_optional(params, "timeout_seconds", timeout_seconds)
        yield from self._transport.stream("event.subscribe", params)


class EndpointNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def add_local(self, name: str, root: str | Path) -> dict[str, Any]:
        return self._transport.request("endpoint.add_local", {"name": name, "root": str(root)})

    def add_ssh(
        self,
        name: str,
        hostname: str,
        username: str,
        known_hosts_file: str | Path,
        port: int = 22,
        identity_file: str | Path | None = None,
        use_ssh_agent: bool = True,
        proxy_jump: str | None = None,
        connect_timeout: float = 15.0,
        keepalive_interval: float = 20.0,
    ) -> dict[str, Any]:
        params = {
            "name": name,
            "hostname": hostname,
            "username": username,
            "known_hosts_file": str(known_hosts_file),
            "port": port,
            "identity_file": str(identity_file) if identity_file is not None else None,
            "use_ssh_agent": use_ssh_agent,
            "proxy_jump": proxy_jump,
            "connect_timeout": connect_timeout,
            "keepalive_interval": keepalive_interval,
        }
        return self._transport.request("endpoint.add_ssh", params)

    def list(self) -> list[dict[str, Any]]:
        return self._transport.request("endpoint.list")

    def health(self, endpoint_id: str) -> dict[str, Any]:
        return self._transport.request("endpoint.health", {"endpoint_id": endpoint_id})


class EnvironmentNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def create(
        self,
        name: str,
        endpoint_ids: list[str],
        workspace_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._transport.request(
            "env.create",
            {
                "name": name,
                "endpoint_ids": endpoint_ids,
                "workspace_ids": workspace_ids or [],
            },
        )

    def get(self, environment_id: str) -> dict[str, Any]:
        return self._transport.request("env.get", {"environment_id": environment_id})

    def list(self) -> list[dict[str, Any]]:
        return self._transport.request("env.list")

    def ensure_local(self, name: str, root: str | Path) -> dict[str, Any]:
        root_text = str(root)
        endpoint = self._find_endpoint(name, "local", {"root": root_text})
        if endpoint is None:
            endpoint = self._transport.request("endpoint.add_local", {"name": name, "root": root_text})
        environment = self._find_environment(name, endpoint["endpoint_id"])
        if environment is None:
            environment = self.create(name, [endpoint["endpoint_id"]])
        return {
            "endpoint": endpoint,
            "environment": environment,
            "target_id": environment["default_execution_target_id"],
        }

    def _find_endpoint(
        self,
        name: str,
        provider_type: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        for endpoint in self._transport.request("endpoint.list"):
            if endpoint.get("name") != name or endpoint.get("provider_type") != provider_type:
                continue
            endpoint_config = dict(endpoint.get("config", {}))
            if all(endpoint_config.get(key) == value for key, value in config.items()):
                return endpoint
        return None

    def _find_environment(self, name: str, endpoint_id: str) -> dict[str, Any] | None:
        for environment in self.list():
            if environment.get("name") == name and endpoint_id in environment.get("endpoint_ids", []):
                return environment
        return None


class TaskNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def start(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        persistent: bool = False,
    ) -> dict[str, Any]:
        return self._transport.request(
            "task.start",
            {
                "environment_id": environment_id,
                "target_id": target_id,
                "argv": argv,
                "cwd": cwd,
                "env": env or {},
                "persistent": persistent,
            },
        )

    def get(self, task_id: str) -> dict[str, Any]:
        return self._transport.request("task.get", {"task_id": task_id})

    def list(self) -> list[dict[str, Any]]:
        return self._transport.request("task.list")

    def logs(self, task_id: str) -> builtins.list[dict[str, Any]]:
        return self._transport.request("task.logs", {"task_id": task_id})

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._transport.request("task.cancel", {"task_id": task_id})

    def wait(
        self,
        task_id: str,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        terminal_states = {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}
        last = self.get(task_id)
        while time.monotonic() < deadline:
            if last.get("state") in terminal_states:
                return last
            time.sleep(poll_interval_seconds)
            last = self.get(task_id)
        raise TimeoutError(f"task {task_id} did not finish within {timeout_seconds} seconds")


class SessionNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        backend: str | None = None,
        cols: int = 120,
        rows: int = 30,
        term_type: str = "xterm-256color",
    ) -> dict[str, Any]:
        return self._transport.request(
            "session.create",
            {
                "environment_id": environment_id,
                "target_id": target_id,
                "argv": argv,
                "cwd": cwd,
                "env": env or {},
                "backend": backend,
                "cols": cols,
                "rows": rows,
                "term_type": term_type,
            },
        )

    def get(self, session_id: str) -> dict[str, Any]:
        return self._transport.request("session.get", {"session_id": session_id})

    def list(self) -> list[dict[str, Any]]:
        return self._transport.request("session.list")

    def acquire_lease(
        self,
        session_id: str,
        ttl_seconds: int = 300,
        force: bool = False,
        owner_id: str | None = None,
        owner_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "session_id": session_id,
            "ttl_seconds": ttl_seconds,
            "force": force,
        }
        _set_optional(params, "owner_id", owner_id)
        _set_optional(params, "owner_type", owner_type)
        return self._transport.request("session.acquire_lease", params)

    def write(self, session_id: str, data: str, owner_id: str | None = None) -> dict[str, Any]:
        params = {"session_id": session_id, "data": data}
        _set_optional(params, "owner_id", owner_id)
        return self._transport.request("session.write", params)

    def resize(self, session_id: str, cols: int, rows: int) -> dict[str, Any]:
        return self._transport.request(
            "session.resize",
            {"session_id": session_id, "cols": cols, "rows": rows},
        )

    def terminate(self, session_id: str) -> dict[str, Any]:
        return self._transport.request("session.terminate", {"session_id": session_id})

    def frames(
        self,
        session_id: str,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> builtins.list[dict[str, Any]]:
        params = {"session_id": session_id, "limit": limit}
        _set_optional(params, "after_seq", after_seq)
        return self._transport.request("session.frames", params)

    def tail(self, session_id: str, limit_chars: int = 20_000) -> dict[str, Any]:
        return self._transport.request(
            "session.tail",
            {"session_id": session_id, "limit_chars": limit_chars},
        )

    def stream_frames(
        self,
        session_id: str,
        after_seq: int | None = None,
        max_items: int | None = None,
        timeout_seconds: float | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "session_id": session_id,
            "heartbeat_seconds": heartbeat_seconds,
        }
        _set_optional(params, "after_seq", after_seq)
        _set_optional(params, "max_items", max_items)
        _set_optional(params, "timeout_seconds", timeout_seconds)
        yield from self._transport.stream("session.subscribe_frames", params)

    def tail_until(
        self,
        session_id: str,
        marker: str,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
        limit_chars: int = 50_000,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last = self.tail(session_id, limit_chars)
        while time.monotonic() < deadline:
            if marker in str(last.get("text", "")):
                return last
            time.sleep(poll_interval_seconds)
            last = self.tail(session_id, limit_chars)
        raise TimeoutError(f"marker {marker!r} was not observed in session {session_id}")


def _set_optional(params: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        params[key] = value
