from __future__ import annotations

from pathlib import Path

import pytest

from environment_runtime.config import RuntimeSettings
from environment_runtime.services.runtime import build_runtime, shutdown_runtime


@pytest.fixture
async def runtime(tmp_path: Path):
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"},
        security={"allowed_local_roots": [tmp_path]},
    )
    runtime = await build_runtime(settings)
    try:
        yield runtime
    finally:
        await shutdown_runtime(runtime)
