from __future__ import annotations

import sys

import pytest

from environment_runtime.core.errors import ValidationError
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.session import SessionService


@pytest.fixture
async def local_environment(runtime, tmp_path):
    endpoint = await EndpointService(runtime).add_local("local", str(tmp_path))
    environment = await EnvironmentService(runtime).create("env", [endpoint.endpoint_id])
    target_id = environment.default_execution_target_id
    assert target_id is not None
    return environment, target_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_create_uses_backend_contract(runtime, local_environment, tmp_path) -> None:
    environment, target_id = local_environment
    session = await SessionService(runtime).create(
        environment.environment_id,
        target_id,
        [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
        cwd=str(tmp_path),
        env={"ENVRT_SESSION_TEST": "ok"},
        backend="local_pty",
        cols=101,
        rows=31,
        term_type="xterm-test",
    )

    try:
        assert session.backend == "local_pty"
        assert session.environment_variables == {"ENVRT_SESSION_TEST": "ok"}
        assert session.terminal_cols == 101
        assert session.terminal_rows == 31
        assert session.term_type == "xterm-test"
        assert "pid" in session.backend_ref
    finally:
        await SessionService(runtime).terminate(session.session_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_resize_updates_metadata_and_emits_event(
    runtime, local_environment
) -> None:
    environment, target_id = local_environment
    service = SessionService(runtime)
    session = await service.create(
        environment.environment_id,
        target_id,
        [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
    )

    try:
        resized = await service.resize(session.session_id, 132, 44)
        events = await runtime.event_store.list_events()
        frames = await service.terminal_frames(session.session_id)

        assert resized.terminal_cols == 132
        assert resized.terminal_rows == 44
        assert any(event.event_type == "session.resized" for event in events)
        assert any(frame.kind == "resize" and frame.data == "132x44" for frame in frames)
    finally:
        await service.terminate(session.session_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_session_backend_is_rejected(runtime, local_environment) -> None:
    environment, target_id = local_environment

    with pytest.raises(ValidationError):
        await SessionService(runtime).create(
            environment.environment_id,
            target_id,
            [sys.executable, "-c", "print('unused')"],
            backend="missing_backend",
        )
