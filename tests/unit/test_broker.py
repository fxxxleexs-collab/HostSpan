from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, BrokerClient, LocalBrokerServer
from environment_runtime.broker.commands import RuntimeCommandHandler
from environment_runtime.config import RuntimeSettings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broker_command_handler_exposes_runtime_status(runtime) -> None:
    result = await RuntimeCommandHandler(runtime).handle("broker.status", {})

    assert result == {"status": "ok", "active_tasks": 0, "active_sessions": 0}


@pytest.mark.integration
def test_local_broker_shares_active_session_across_client_calls(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker.db'}"},
        runtime={"data_dir": tmp_path / "data"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = BrokerClient(address)
    try:
        assert client.call("broker.ping") == {"status": "ok"}
        endpoint = client.call("endpoint.add_local", {"name": "local", "root": str(tmp_path)})
        environment = client.call(
            "env.create",
            {"name": "env", "endpoint_ids": [endpoint["endpoint_id"]]},
        )
        session = client.call(
            "session.create",
            {
                "environment_id": environment["environment_id"],
                "target_id": environment["default_execution_target_id"],
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('BROKER_READY'); value=input(); print(f'BROKER_GOT={value}')",
                ],
            },
        )

        _wait_for_tail(client, session["session_id"], "BROKER_READY")
        client.call(
            "session.write",
            {"session_id": session["session_id"], "data": "from-client-two\n"},
        )
        tail = _wait_for_tail(client, session["session_id"], "BROKER_GOT=from-client-two")
        frames = client.call("session.frames", {"session_id": session["session_id"]})
        status = client.call("broker.status")

        assert "BROKER_GOT=from-client-two" in tail["text"]
        assert any(frame["kind"] == "redacted" for frame in frames)
        assert status["active_sessions"] in {0, 1}
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.call("broker.shutdown")
        thread.join(timeout=10)


@pytest.mark.integration
def test_broker_event_subscription_replays_matching_events(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-events.db'}"},
        runtime={"data_dir": tmp_path / "data-events"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = BrokerClient(address)
    try:
        endpoint = client.call("endpoint.add_local", {"name": "local", "root": str(tmp_path)})

        events = list(
            client.stream(
                "event.subscribe",
                {
                    "resource_type": "endpoint",
                    "resource_id": endpoint["endpoint_id"],
                    "max_items": 1,
                    "timeout_seconds": 5,
                },
            )
        )

        assert len(events) == 1
        assert events[0]["event_type"] == "endpoint.connected"
        assert events[0]["resource_id"] == endpoint["endpoint_id"]
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.call("broker.shutdown")
        thread.join(timeout=10)


@pytest.mark.integration
def test_broker_session_frame_subscription_replays_terminal_frames(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-frames.db'}"},
        runtime={"data_dir": tmp_path / "data-frames"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = BrokerClient(address)
    try:
        endpoint = client.call("endpoint.add_local", {"name": "local", "root": str(tmp_path)})
        environment = client.call(
            "env.create",
            {"name": "env", "endpoint_ids": [endpoint["endpoint_id"]]},
        )
        session = client.call(
            "session.create",
            {
                "environment_id": environment["environment_id"],
                "target_id": environment["default_execution_target_id"],
                "argv": [sys.executable, "-u", "-c", "print('FRAME_STREAM_READY')"],
            },
        )
        _wait_for_tail(client, session["session_id"], "FRAME_STREAM_READY")

        frames = list(
            client.stream(
                "session.subscribe_frames",
                {
                    "session_id": session["session_id"],
                    "after_seq": -1,
                    "max_items": 1,
                    "timeout_seconds": 5,
                },
            )
        )

        assert len(frames) == 1
        assert frames[0]["kind"] == "output"
        assert "FRAME_STREAM_READY" in frames[0]["data"]
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.call("broker.shutdown")
        thread.join(timeout=10)


def _wait_for_tail(
    client: BrokerClient,
    session_id: str,
    needle: str,
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tail = client.call("session.tail", {"session_id": session_id, "limit_chars": 50_000})
        if needle in str(tail["text"]):
            return tail
        threading.Event().wait(0.1)
    return client.call("session.tail", {"session_id": session_id, "limit_chars": 50_000})


def _test_address(tmp_path: Path) -> BrokerAddress:
    if os.name == "nt":
        return BrokerAddress(rf"\\.\pipe\environment-runtime-test-{uuid4().hex}", "AF_PIPE")
    return BrokerAddress(str(tmp_path / "broker.sock"), "AF_UNIX")
