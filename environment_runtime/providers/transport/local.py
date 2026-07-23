from __future__ import annotations

from pathlib import Path

from environment_runtime.core.models import Endpoint


class LocalTransportProvider:
    async def healthcheck(self, endpoint: Endpoint | None = None) -> dict:
        return {"status": "ok", "cwd": str(Path.cwd())}
