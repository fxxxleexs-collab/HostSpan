from __future__ import annotations

from typing import Any, Protocol

from environment_runtime.sdk import AgentRuntimeClient
from mini_harness.config import SSHRuntimeConfig


class HarnessRuntimeClient(Protocol):
    def ensure_local(self, name: str, root: str) -> dict[str, Any]: ...

    def put_secret(self, value: str, purpose: str = "runtime") -> str: ...

    def delete_secret(self, secret_ref: str) -> bool: ...

    def ensure_ssh(
        self,
        name: str,
        ssh: SSHRuntimeConfig,
        password_secret_ref: str | None = None,
        trust_host_once: bool = False,
    ) -> dict[str, Any]: ...

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]: ...

    def ensure_dir(self, endpoint_id: str, path: str) -> dict[str, Any]: ...

    def read_text(self, endpoint_id: str, path: str) -> str: ...

    def write_text(self, endpoint_id: str, path: str, text: str) -> dict[str, Any]: ...

    def start_task(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        persistent: bool,
    ) -> dict[str, Any]: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def task_logs(self, task_id: str) -> list[dict[str, Any]]: ...

    def cancel_task(self, task_id: str) -> dict[str, Any]: ...

    def run_command(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
    ) -> dict[str, Any]: ...

    def observe_task(
        self,
        task_id: str,
        cursor: int,
        max_chars: int,
        wait_seconds: float,
    ) -> dict[str, Any]: ...

    def open_terminal(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        cols: int,
        rows: int,
    ) -> dict[str, Any]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def observe_terminal(
        self,
        session_id: str,
        after_seq: int | None,
        limit_chars: int,
    ) -> dict[str, Any]: ...

    def write_terminal(self, session_id: str, data: str) -> dict[str, Any]: ...

    def close_terminal(self, session_id: str) -> dict[str, Any]: ...


class SDKRuntimeClient:
    def __init__(self, client: AgentRuntimeClient) -> None:
        self.client = client

    def ensure_local(self, name: str, root: str) -> dict[str, Any]:
        return self.client.environments.ensure_local(name, root)

    def put_secret(self, value: str, purpose: str = "runtime") -> str:
        return self.client.secrets.put(value, purpose=purpose)

    def delete_secret(self, secret_ref: str) -> bool:
        return self.client.secrets.delete(secret_ref)

    def ensure_ssh(
        self,
        name: str,
        ssh: SSHRuntimeConfig,
        password_secret_ref: str | None = None,
        trust_host_once: bool = False,
    ) -> dict[str, Any]:
        if not ssh.hostname or not ssh.username or not ssh.known_hosts_file:
            raise ValueError("ssh runtime requires hostname, username, and known_hosts_file")
        return self.client.environments.ensure_ssh(
            name=name,
            hostname=ssh.hostname,
            username=ssh.username,
            known_hosts_file=ssh.known_hosts_file,
            port=ssh.port,
            auth_method=ssh.auth_method,
            identity_file=ssh.identity_file,
            password_secret_ref=password_secret_ref,
            use_ssh_agent=ssh.use_ssh_agent,
            proxy_jump=ssh.proxy_jump,
            connect_timeout=ssh.connect_timeout,
            keepalive_interval=ssh.keepalive_interval,
            trust_host_once=trust_host_once,
        )

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]:
        return self.client.files.list(endpoint_id, path, recursive)

    def ensure_dir(self, endpoint_id: str, path: str) -> dict[str, Any]:
        return self.client.files.mkdir(endpoint_id, path)

    def read_text(self, endpoint_id: str, path: str) -> str:
        return self.client.files.read_text(endpoint_id, path)

    def write_text(self, endpoint_id: str, path: str, text: str) -> dict[str, Any]:
        return self.client.files.write_text(endpoint_id, path, text)

    def start_task(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        persistent: bool,
    ) -> dict[str, Any]:
        return self.client.tasks.start(
            environment_id,
            target_id,
            argv,
            cwd=cwd,
            persistent=persistent,
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.client.tasks.get(task_id)

    def task_logs(self, task_id: str) -> list[dict[str, Any]]:
        return self.client.tasks.logs(task_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self.client.tasks.cancel(task_id)

    def run_command(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
    ) -> dict[str, Any]:
        return self.client.commands.run(environment_id, target_id, argv, cwd=cwd)

    def observe_task(
        self,
        task_id: str,
        cursor: int,
        max_chars: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        return self.client.tasks.observe(
            task_id,
            cursor=cursor,
            max_chars=max_chars,
            wait_seconds=wait_seconds,
        )

    def open_terminal(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        cols: int,
        rows: int,
    ) -> dict[str, Any]:
        return self.client.terminals.open(
            environment_id,
            target_id,
            argv,
            cwd=cwd,
            cols=cols,
            rows=rows,
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.client.sessions.list()

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.client.sessions.get(session_id)

    def observe_terminal(
        self,
        session_id: str,
        after_seq: int | None,
        limit_chars: int,
    ) -> dict[str, Any]:
        return self.client.terminals.observe(
            session_id,
            after_seq=after_seq,
            limit_chars=limit_chars,
        )

    def write_terminal(self, session_id: str, data: str) -> dict[str, Any]:
        return self.client.terminals.write(session_id, data)

    def close_terminal(self, session_id: str) -> dict[str, Any]:
        return self.client.terminals.close(session_id)
