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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_lease_force_replaces_existing_session_lease(runtime, tmp_path) -> None:
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

    first = await leases.acquire(session.session_id, "automation", "owner-a")
    second = await leases.acquire(session.session_id, "automation", "owner-b", force=True)

    current = await leases.get_for_session(session.session_id)
    assert current is not None
    assert second.lease_id == first.lease_id
    assert current.owner_id == "owner-b"
    assert current.version == first.version + 1
    await leases.validate(session.session_id, "owner-b")
    with pytest.raises(ConflictError, match="held by another owner"):
        await leases.validate(session.session_id, "owner-a")
