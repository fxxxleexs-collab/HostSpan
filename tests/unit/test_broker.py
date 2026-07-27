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
from environment_runtime.core.errors import ProviderError


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broker_command_handler_exposes_runtime_status(runtime) -> None:
    result = await RuntimeCommandHandler(runtime).handle("broker.status", {})

    assert result == {"status": "ok", "active_tasks": 0, "active_sessions": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broker_command_handler_exposes_canonical_command_metadata(runtime) -> None:
    result = await RuntimeCommandHandler(runtime).handle("broker.commands", {})

    methods = {item["method"] for item in result}
    assert "env.ensure_local" in methods
    assert "event.subscribe" in methods
    assert "workspace.create" in methods
    assert "file.write_text" in methods
    assert "session.subscribe_frames" in methods


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
    client = BrokerClient(address, settings=settings, principal_id="agent-a")
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
        client.call("session.acquire_lease", {"session_id": session["session_id"], "force": True})
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
    client = BrokerClient(address, settings=settings)
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
    client = BrokerClient(address, settings=settings)
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


@pytest.mark.integration
def test_broker_rejects_client_without_auth_token(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-auth.db'}"},
        runtime={"data_dir": tmp_path / "data-auth"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    trusted_client = BrokerClient(address, settings=settings)
    untrusted_client = BrokerClient(address)
    try:
        with pytest.raises(ProviderError, match="broker auth token is required"):
            untrusted_client.call("broker.ping")
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                trusted_client.call("broker.shutdown")
        thread.join(timeout=10)


@pytest.mark.integration
def test_broker_writer_lease_is_enforced_by_principal(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-lease.db'}"},
        runtime={"data_dir": tmp_path / "data-lease"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    agent_a = BrokerClient(address, settings=settings, principal_id="agent-a")
    agent_b = BrokerClient(address, settings=settings, principal_id="agent-b")
    try:
        endpoint = agent_a.call("endpoint.add_local", {"name": "local", "root": str(tmp_path)})
        environment = agent_a.call(
            "env.create",
            {"name": "env", "endpoint_ids": [endpoint["endpoint_id"]]},
        )
        session = agent_a.call(
            "session.create",
            {
                "environment_id": environment["environment_id"],
                "target_id": environment["default_execution_target_id"],
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('LEASE_READY'); value=input(); print(f'LEASE_GOT={value}')",
                ],
            },
        )
        _wait_for_tail(agent_a, session["session_id"], "LEASE_READY")
        agent_a.call("session.acquire_lease", {"session_id": session["session_id"], "force": True})

        with pytest.raises(ProviderError, match="writer lease is held by another owner"):
            agent_b.call(
                "session.write",
                {"session_id": session["session_id"], "data": "from-agent-b\n"},
            )

        agent_a.call("session.write", {"session_id": session["session_id"], "data": "from-agent-a\n"})
        tail = _wait_for_tail(agent_a, session["session_id"], "LEASE_GOT=from-agent-a")
        assert "LEASE_GOT=from-agent-a" in tail["text"]
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                agent_a.call("broker.shutdown")
        thread.join(timeout=10)


@pytest.mark.integration
def test_broker_workspace_and_local_file_commands(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-files.db'}"},
        runtime={"data_dir": tmp_path / "data-files"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = BrokerClient(address, settings=settings)
    try:
        bundle = client.call("env.ensure_local", {"name": "files-env", "root": str(tmp_path)})
        endpoint_id = bundle["endpoint"]["endpoint_id"]
        workspace = client.call("workspace.create", {"name": "workspace"})
        with_root = client.call(
            "workspace.add_root",
            {"workspace_id": workspace["workspace_id"], "logical_path": "."},
        )
        client.call(
            "workspace.add_replica",
            {
                "workspace_id": workspace["workspace_id"],
                "endpoint_id": endpoint_id,
                "physical_root": str(tmp_path),
            },
        )
        write_result = client.call(
            "file.write_text",
            {"endpoint_id": endpoint_id, "path": "nested/hello.txt", "text": "hello broker"},
        )
        read_result = client.call(
            "file.read_text",
            {"endpoint_id": endpoint_id, "path": "nested/hello.txt"},
        )
        entries = client.call("file.list", {"endpoint_id": endpoint_id, "path": "nested"})
        digest = client.call("file.sha256", {"endpoint_id": endpoint_id, "path": "nested/hello.txt"})

        assert with_root["roots"][0]["logical_path"] == "."
        assert write_result["size"] == len("hello broker")
        assert read_result["text"] == "hello broker"
        assert entries["entries"] == ["hello.txt"]
        assert len(digest["sha256"]) == 64

        with pytest.raises(ProviderError, match="local file path must stay within the endpoint root"):
            client.call("file.read_text", {"endpoint_id": endpoint_id, "path": "../outside.txt"})
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.call("broker.shutdown")
        thread.join(timeout=10)


@pytest.mark.integration
def test_broker_command_params_are_validated(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'broker-validation.db'}"},
        runtime={"data_dir": tmp_path / "data-validation"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = BrokerClient(address, settings=settings)
    try:
        with pytest.raises(ProviderError, match="argv"):
            client.call(
                "task.start",
                {"environment_id": "env", "target_id": "target", "argv": []},
            )
        with pytest.raises(ProviderError, match="endpoint_id"):
            client.call("file.read_text", {"path": "missing-endpoint.txt"})
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
