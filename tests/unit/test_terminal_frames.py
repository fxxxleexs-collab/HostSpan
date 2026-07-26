from __future__ import annotations

import asyncio
import sys

import pytest

from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.session import SessionService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_frames_are_queryable_by_sequence(runtime, tmp_path) -> None:
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None

    service = SessionService(runtime)
    session = await service.create(
        environment.environment_id,
        target_id,
        [sys.executable, "-u", "-c", "print('one'); print('two')"],
    )
    await asyncio.sleep(1)

    frames = await service.terminal_frames(session.session_id)
    output_frames = [frame for frame in frames if frame.kind == "output"]
    assert output_frames
    assert [frame.seq for frame in frames] == sorted(frame.seq for frame in frames)

    later = await service.terminal_frames(session.session_id, after_seq=frames[0].seq)
    assert all(frame.seq > frames[0].seq for frame in later)
    assert "one" in await _tail_text(service, session.session_id)
    assert "two" in await _tail_text(service, session.session_id)


async def _tail_text(service: SessionService, session_id: str) -> str:
    tail = await service.terminal_tail(session_id)
    return str(tail["text"])
