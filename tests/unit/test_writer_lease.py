from __future__ import annotations

import sys

import pytest

from environment_runtime.core.errors import ConflictError
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.security import WriterLeaseService
from environment_runtime.services.session import SessionService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_lease_conflict(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    session = await SessionService(runtime).create(
        environment.environment_id,
        target_id,
        [sys.executable, "-c", "print('ready')"],
    )
    leases = WriterLeaseService(runtime)
    await leases.acquire(session.session_id, "automation", "owner-a")
    with pytest.raises(ConflictError):
        await leases.acquire(session.session_id, "human", "owner-b")
