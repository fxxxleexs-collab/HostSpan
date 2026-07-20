from __future__ import annotations

from typing import Any

import httpx


class AsyncEnvironmentRuntimeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self._client = httpx.AsyncClient(base_url=base_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def add_local_endpoint(self, name: str, root: str) -> dict[str, Any]:
        response = await self._client.post("/endpoints", json={"name": name, "root": root})
        response.raise_for_status()
        return response.json()

    async def create_environment(
        self, name: str, endpoint_ids: list[str], workspace_ids: list[str] | None = None
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/environments",
            json={"name": name, "endpoint_ids": endpoint_ids, "workspace_ids": workspace_ids or []},
        )
        response.raise_for_status()
        return response.json()

    async def create_workspace(self, name: str) -> dict[str, Any]:
        response = await self._client.post("/workspaces", json={"name": name})
        response.raise_for_status()
        return response.json()

    async def start_task(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str | None = None,
        persistent: bool = False,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/tasks",
            json={
                "environment_id": environment_id,
                "target_id": target_id,
                "argv": argv,
                "cwd": cwd,
                "persistent": persistent,
            },
        )
        response.raise_for_status()
        return response.json()
