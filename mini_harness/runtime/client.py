from __future__ import annotations

from typing import Any, Protocol

from environment_runtime.sdk import AgentRuntimeClient


class HarnessRuntimeClient(Protocol):
    def ensure_local(self, name: str, root: str) -> dict[str, Any]: ...

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]: ...

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


class SDKRuntimeClient:
    def __init__(self, client: AgentRuntimeClient) -> None:
        self.client = client

    def ensure_local(self, name: str, root: str) -> dict[str, Any]:
        return self.client.environments.ensure_local(name, root)

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]:
        return self.client.files.list(endpoint_id, path, recursive)

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
