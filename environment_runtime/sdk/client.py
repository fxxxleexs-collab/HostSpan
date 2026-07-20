from __future__ import annotations

from typing import Any

import httpx


class EnvironmentRuntimeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self._client = httpx.Client(base_url=base_url)

    def close(self) -> None:
        self._client.close()

    def list_tasks(self) -> list[dict[str, Any]]:
        response = self._client.get("/tasks")
        response.raise_for_status()
        return response.json()
