from __future__ import annotations

import base64
import builtins
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from environment_runtime.broker import BrokerAddress
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk.transport import BrokerTransport, RuntimeTransport


@dataclass(frozen=True)
class RuntimePolicy:
    """Default backend choices for semantic SDK helpers.

    Low-level namespaces such as ``tasks`` and ``sessions`` remain explicit.
    This policy is only used by higher-level helpers such as ``commands`` and
    ``terminals``.
    """

    local_command_persistent: bool = False
    remote_command_persistent: bool = True
    local_terminal_backend: str = "local_pty"
    remote_terminal_backend: str = "ssh_tmux"
    allow_ssh_pty_fallback: bool = True
    writer_lease_ttl_seconds: int = 300
    renew_terminal_lease_on_write: bool = True


class AgentRuntimeClient:
    """Agent-facing facade over the runtime command surface."""

    def __init__(self, transport: RuntimeTransport, policy: RuntimePolicy | None = None) -> None:
        self.transport = transport
        self.policy = policy or RuntimePolicy()
        self.broker = BrokerNamespace(transport)
        self.endpoints = EndpointNamespace(transport)
        self.environments = EnvironmentNamespace(transport)
        self.workspaces = WorkspaceNamespace(transport)
        self.files = FileNamespace(transport)
        self.tasks = TaskNamespace(transport)
        self.sessions = SessionNamespace(transport, self.policy)
        self.commands = CommandNamespace(transport, self.policy)
        self.terminals = TerminalNamespace(transport, self.policy)

    @classmethod
    def from_broker(
        cls,
        address: BrokerAddress | None = None,
        settings: RuntimeSettings | None = None,
        token: str | None = None,
        principal_id: str = "agent",
        principal_type: str = "agent",
        scope_id: str = "default",
        policy: RuntimePolicy | None = None,
    ) -> AgentRuntimeClient:
        return cls(
            BrokerTransport(
                address=address,
                settings=settings,
                token=token,
                principal_id=principal_id,
                principal_type=principal_type,
                scope_id=scope_id,
            ),
            policy=policy,
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

    def commands(self) -> list[dict[str, Any]]:
        return self._transport.request("broker.commands")

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
        return self._transport.request("env.ensure_local", {"name": name, "root": str(root)})

    def ensure_ssh(
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
        return self._transport.request(
            "env.ensure_ssh",
            {
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
            },
        )


class WorkspaceNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def create(self, name: str) -> dict[str, Any]:
        return self._transport.request("workspace.create", {"name": name})

    def get(self, workspace_id: str) -> dict[str, Any]:
        return self._transport.request("workspace.get", {"workspace_id": workspace_id})

    def list(self) -> list[dict[str, Any]]:
        return self._transport.request("workspace.list")

    def add_root(self, workspace_id: str, logical_path: str) -> dict[str, Any]:
        return self._transport.request(
            "workspace.add_root",
            {"workspace_id": workspace_id, "logical_path": logical_path},
        )

    def add_replica(
        self,
        workspace_id: str,
        endpoint_id: str,
        physical_root: str | Path,
    ) -> dict[str, Any]:
        return self._transport.request(
            "workspace.add_replica",
            {
                "workspace_id": workspace_id,
                "endpoint_id": endpoint_id,
                "physical_root": str(physical_root),
            },
        )

    def bind(
        self,
        workspace_id: str,
        source_replica_id: str,
        target_replica_id: str,
        mode: str = "ONE_WAY_MIRROR",
    ) -> dict[str, Any]:
        return self._transport.request(
            "workspace.bind",
            {
                "workspace_id": workspace_id,
                "source_replica_id": source_replica_id,
                "target_replica_id": target_replica_id,
                "mode": mode,
            },
        )

    def revision(self, workspace_id: str, replica_id: str) -> dict[str, Any]:
        return self._transport.request(
            "workspace.revision",
            {"workspace_id": workspace_id, "replica_id": replica_id},
        )

    def sync(self, workspace_id: str, binding_id: str) -> dict[str, Any]:
        return self._transport.request(
            "workspace.sync",
            {"workspace_id": workspace_id, "binding_id": binding_id},
        )


class FileNamespace:
    def __init__(self, transport: RuntimeTransport) -> None:
        self._transport = transport

    def exists(self, endpoint_id: str, path: str | Path) -> bool:
        result = self._transport.request(
            "file.exists",
            {"endpoint_id": endpoint_id, "path": str(path)},
        )
        return bool(result["exists"])

    def stat(self, endpoint_id: str, path: str | Path) -> dict[str, Any]:
        return self._transport.request("file.stat", {"endpoint_id": endpoint_id, "path": str(path)})

    def list(
        self,
        endpoint_id: str,
        path: str | Path,
        recursive: bool = False,
    ) -> list[str]:
        result = self._transport.request(
            "file.list",
            {"endpoint_id": endpoint_id, "path": str(path), "recursive": recursive},
        )
        return result["entries"]

    def mkdir(self, endpoint_id: str, path: str | Path) -> dict[str, Any]:
        return self._transport.request("file.mkdir", {"endpoint_id": endpoint_id, "path": str(path)})

    def remove(self, endpoint_id: str, path: str | Path) -> dict[str, Any]:
        return self._transport.request("file.remove", {"endpoint_id": endpoint_id, "path": str(path)})

    def sha256(self, endpoint_id: str, path: str | Path) -> str:
        result = self._transport.request(
            "file.sha256",
            {"endpoint_id": endpoint_id, "path": str(path)},
        )
        return str(result["sha256"])

    def read_text(
        self,
        endpoint_id: str,
        path: str | Path,
        encoding: str = "utf-8",
    ) -> str:
        result = self._transport.request(
            "file.read_text",
            {"endpoint_id": endpoint_id, "path": str(path), "encoding": encoding},
        )
        return str(result["text"])

    def write_text(
        self,
        endpoint_id: str,
        path: str | Path,
        text: str,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        return self._transport.request(
            "file.write_text",
            {
                "endpoint_id": endpoint_id,
                "path": str(path),
                "text": text,
                "encoding": encoding,
            },
        )

    def read_bytes(self, endpoint_id: str, path: str | Path) -> bytes:
        result = self._transport.request(
            "file.read_bytes",
            {"endpoint_id": endpoint_id, "path": str(path)},
        )
        return base64.b64decode(str(result["data_base64"]))

    def write_bytes(self, endpoint_id: str, path: str | Path, data: bytes) -> dict[str, Any]:
        return self._transport.request(
            "file.write_bytes",
            {
                "endpoint_id": endpoint_id,
                "path": str(path),
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )


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

    def logs_text(self, task_id: str) -> str:
        return "".join(str(item.get("chunk", "")) for item in self.logs(task_id))

    def observe(
        self,
        task_id: str,
        cursor: int = 0,
        max_chars: int = 12_000,
        wait_seconds: float = 0.0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if cursor < 0:
            raise ValueError("cursor must be greater than or equal to 0")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        deadline = time.monotonic() + wait_seconds
        task = self.get(task_id)
        text = self.logs_text(task_id)
        while wait_seconds > 0 and len(text) <= cursor and not _is_terminal_task(task):
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_seconds)
            task = self.get(task_id)
            text = self.logs_text(task_id)
        next_cursor = len(text)
        start = min(cursor, len(text))
        chunk = text[start:]
        truncated = len(chunk) > max_chars
        if truncated:
            chunk = chunk[-max_chars:]
        is_terminal = _is_terminal_task(task)
        return {
            "task": task,
            "task_id": task_id,
            "text": chunk,
            "cursor": next_cursor,
            "truncated": truncated,
            "state": task.get("state"),
            "exit_code": task.get("exit_code"),
            "is_terminal": is_terminal,
            "terminal": is_terminal,
        }

    def wait_for_log(
        self,
        task_id: str,
        marker: str,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        text = self.logs_text(task_id)
        while time.monotonic() < deadline:
            if marker in text:
                return text
            time.sleep(poll_interval_seconds)
            text = self.logs_text(task_id)
        raise TimeoutError(f"marker {marker!r} was not observed in task {task_id}")

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
    def __init__(self, transport: RuntimeTransport, policy: RuntimePolicy | None = None) -> None:
        self._transport = transport
        self._policy = policy

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

    def write(
        self,
        session_id: str,
        data: str,
        owner_id: str | None = None,
        renew_lease: bool | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        should_renew = bool(
            renew_lease
            if renew_lease is not None
            else self._policy and self._policy.renew_terminal_lease_on_write
        )
        if should_renew:
            self.acquire_lease(
                session_id,
                ttl_seconds=ttl_seconds
                or (
                    self._policy.writer_lease_ttl_seconds
                    if self._policy is not None
                    else 300
                ),
                force=True,
                owner_id=owner_id,
            )
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


class CommandNamespace:
    def __init__(self, transport: RuntimeTransport, policy: RuntimePolicy) -> None:
        self._transport = transport
        self._policy = policy
        self._tasks = TaskNamespace(transport)

    def run(
        self,
        environment_id: str,
        target_id: str | None,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        persistent: bool | None = None,
    ) -> dict[str, Any]:
        target = _resolve_target(self._transport, environment_id, target_id)
        resolved_persistent = persistent
        if resolved_persistent is None:
            resolved_persistent = (
                self._policy.remote_command_persistent
                if _is_remote_target(target)
                else self._policy.local_command_persistent
            )
        task = self._tasks.start(
            environment_id,
            str(target["target_id"]),
            argv,
            cwd=cwd,
            env=env,
            persistent=resolved_persistent,
        )
        task["sdk"] = {
            "operation": "commands.run",
            "target_provider": target.get("provider"),
            "persistent": resolved_persistent,
        }
        return task

    def observe(
        self,
        task_id: str,
        cursor: int = 0,
        max_chars: int = 12_000,
        wait_seconds: float = 0.0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        return self._tasks.observe(
            task_id,
            cursor=cursor,
            max_chars=max_chars,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


class TerminalNamespace:
    def __init__(self, transport: RuntimeTransport, policy: RuntimePolicy) -> None:
        self._transport = transport
        self._policy = policy
        self._sessions = SessionNamespace(transport, policy)

    def open(
        self,
        environment_id: str,
        target_id: str | None,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        backend: str | None = None,
        cols: int = 120,
        rows: int = 30,
        term_type: str = "xterm-256color",
        acquire_lease: bool = True,
        force_lease: bool = True,
    ) -> dict[str, Any]:
        target = _resolve_target(self._transport, environment_id, target_id)
        selected_backend = backend or (
            self._policy.remote_terminal_backend
            if _is_remote_target(target)
            else self._policy.local_terminal_backend
        )
        fallback_from: str | None = None
        fallback_error: str | None = None
        try:
            session = self._sessions.create(
                environment_id,
                str(target["target_id"]),
                argv,
                cwd=cwd,
                env=env,
                backend=selected_backend,
                cols=cols,
                rows=rows,
                term_type=term_type,
            )
        except Exception as exc:
            if not (
                _is_remote_target(target)
                and selected_backend == "ssh_tmux"
                and self._policy.allow_ssh_pty_fallback
            ):
                raise
            fallback_from = selected_backend
            fallback_error = str(exc)
            selected_backend = "ssh_pty"
            session = self._sessions.create(
                environment_id,
                str(target["target_id"]),
                argv,
                cwd=cwd,
                env=env,
                backend=selected_backend,
                cols=cols,
                rows=rows,
                term_type=term_type,
            )

        lease = None
        if acquire_lease:
            lease = self._sessions.acquire_lease(
                str(session["session_id"]),
                ttl_seconds=self._policy.writer_lease_ttl_seconds,
                force=force_lease,
            )
        return {
            "session": session,
            "lease": lease,
            "session_id": session["session_id"],
            "backend": session.get("backend", selected_backend),
            "fallback_from": fallback_from,
            "fallback_error": fallback_error,
            "target_provider": target.get("provider"),
        }

    def observe(
        self,
        session_id: str,
        after_seq: int | None = None,
        limit: int = 500,
        limit_chars: int = 20_000,
    ) -> dict[str, Any]:
        frames = self._sessions.frames(session_id, after_seq=after_seq, limit=limit)
        tail = self._sessions.tail(session_id, limit_chars=limit_chars)
        last_seq = tail.get("last_seq")
        if frames:
            last_seq = frames[-1].get("seq", last_seq)
        return {
            "session_id": session_id,
            "frames": frames,
            "text": tail.get("text", ""),
            "cursor": last_seq,
            "last_seq": last_seq,
        }

    def write(
        self,
        session_id: str,
        data: str,
        owner_id: str | None = None,
        renew_lease: bool | None = None,
    ) -> dict[str, Any]:
        should_renew = (
            self._policy.renew_terminal_lease_on_write
            if renew_lease is None
            else renew_lease
        )
        return self._sessions.write(
            session_id,
            data,
            owner_id=owner_id,
            renew_lease=should_renew,
        )

    def resize(self, session_id: str, cols: int, rows: int) -> dict[str, Any]:
        return self._sessions.resize(session_id, cols, rows)

    def close(self, session_id: str) -> dict[str, Any]:
        return self._sessions.terminate(session_id)

    def stream(
        self,
        session_id: str,
        after_seq: int | None = None,
        max_items: int | None = None,
        timeout_seconds: float | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> Iterator[dict[str, Any]]:
        yield from self._sessions.stream_frames(
            session_id,
            after_seq=after_seq,
            max_items=max_items,
            timeout_seconds=timeout_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )


def _resolve_target(
    transport: RuntimeTransport,
    environment_id: str,
    target_id: str | None,
) -> dict[str, Any]:
    environment = transport.request("env.get", {"environment_id": environment_id})
    resolved_target_id = target_id or environment.get("default_execution_target_id")
    for target in environment.get("execution_targets", []):
        if target.get("target_id") == resolved_target_id:
            return target
    raise ValueError(f"target {resolved_target_id!r} was not found in environment {environment_id}")


def _is_remote_target(target: dict[str, Any]) -> bool:
    return str(target.get("provider", "")).startswith("ssh")


def _is_terminal_task(task: dict[str, Any]) -> bool:
    return str(task.get("state")) in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}


def _set_optional(params: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        params[key] = value
