from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.streams: list[tuple[str, dict[str, Any] | None]] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, params))
        if method == "endpoint.list":
            return []
        if method == "endpoint.add_local":
            return {
                "endpoint_id": "endpoint_1",
                "name": params["name"],
                "provider_type": "local",
                "config": {"root": params["root"]},
            }
        if method == "env.list":
            return []
        if method == "env.create":
            return {
                "environment_id": "env_1",
                "name": params["name"],
                "endpoint_ids": params["endpoint_ids"],
                "default_execution_target_id": "target_1",
            }
        if method == "session.create":
            return {"session_id": "session_1", "backend": params.get("backend") or "local_pty"}
        if method == "session.acquire_lease":
            return {"lease_id": "lease_1", "session_id": params["session_id"]}
        if method == "session.write":
            return {"session_id": params["session_id"]}
        raise AssertionError(f"unexpected method: {method}")

    def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        self.streams.append((method, params))
        yield {"kind": "output", "data": "ready"}

    def close(self) -> None:
        return None


@pytest.mark.unit
def test_agent_sdk_facade_maps_to_canonical_transport_methods(tmp_path: Path) -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(transport)

    bundle = client.environments.ensure_local("agent-env", tmp_path)
    session = client.sessions.create(
        bundle["environment"]["environment_id"],
        bundle["target_id"],
        [sys.executable, "-i"],
        backend="local_pty",
    )
    lease = client.sessions.acquire_lease(session["session_id"], force=True)
    client.sessions.write(session["session_id"], "print('ok')\n")
    frames = list(client.sessions.stream_frames(session["session_id"], max_items=1))

    assert bundle["endpoint"]["endpoint_id"] == "endpoint_1"
    assert session["session_id"] == "session_1"
    assert lease["lease_id"] == "lease_1"
    assert frames == [{"kind": "output", "data": "ready"}]
    assert [method for method, _ in transport.requests] == [
        "endpoint.list",
        "endpoint.add_local",
        "env.list",
        "env.create",
        "session.create",
        "session.acquire_lease",
        "session.write",
    ]
    assert transport.streams == [
        (
            "session.subscribe_frames",
            {
                "session_id": "session_1",
                "heartbeat_seconds": 15.0,
                "max_items": 1,
            },
        )
    ]


@pytest.mark.integration
def test_agent_sdk_runs_session_flow_over_broker_transport(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'agent-sdk.db'}"},
        runtime={"data_dir": tmp_path / "agent-sdk-data"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = AgentRuntimeClient.from_broker(address=address, settings=settings, principal_id="sdk-agent")
    try:
        assert client.broker.ping() == {"status": "ok"}
        bundle = client.environments.ensure_local("sdk-env", tmp_path)
        session = client.sessions.create(
            bundle["environment"]["environment_id"],
            bundle["target_id"],
            [
                sys.executable,
                "-u",
                "-c",
                "print('SDK_READY'); value=input(); print(f'SDK_GOT={value}')",
            ],
        )

        ready_tail = client.sessions.tail_until(session["session_id"], "SDK_READY", timeout_seconds=10)
        client.sessions.acquire_lease(session["session_id"], force=True)
        client.sessions.write(session["session_id"], "from-sdk\n")
        final_tail = client.sessions.tail_until(
            session["session_id"],
            "SDK_GOT=from-sdk",
            timeout_seconds=10,
        )
        frames = list(
            client.sessions.stream_frames(
                session["session_id"],
                after_seq=-1,
                max_items=1,
                timeout_seconds=5,
            )
        )

        assert "SDK_READY" in ready_tail["text"]
        assert "SDK_GOT=from-sdk" in final_tail["text"]
        assert frames
        assert frames[0]["kind"] == "output"
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.broker.shutdown()
        client.close()
        thread.join(timeout=10)


def _test_address(tmp_path: Path) -> BrokerAddress:
    if os.name == "nt":
        return BrokerAddress(rf"\\.\pipe\environment-runtime-agent-sdk-test-{uuid4().hex}", "AF_PIPE")
    return BrokerAddress(str(tmp_path / "broker.sock"), "AF_UNIX")
