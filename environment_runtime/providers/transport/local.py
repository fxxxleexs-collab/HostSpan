from __future__ import annotations

from pathlib import Path


class LocalTransportProvider:
    async def healthcheck(self) -> dict:
        return {"status": "ok", "cwd": str(Path.cwd())}
