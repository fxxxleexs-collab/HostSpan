from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient, RuntimePolicy


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.streams: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_tmux_create = False

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, params))
        if method == "env.ensure_local":
            return {
                "endpoint": {
                    "endpoint_id": "endpoint_1",
                    "name": params["name"],
                    "provider_type": "local",
                    "config": {"root": params["root"]},
                },
                "environment": {
                    "environment_id": "env_1",
                    "name": params["name"],
                    "endpoint_ids": ["endpoint_1"],
                    "default_execution_target_id": "target_1",
                },
                "target_id": "target_1",
            }
        if method == "env.get":
            if params["environment_id"] == "env_ssh":
                return {
                    "environment_id": "env_ssh",
                    "name": "ssh-env",
                    "endpoint_ids": ["endpoint_ssh"],
                    "default_execution_target_id": "target_ssh",
                    "execution_targets": [
                        {
                            "target_id": "target_ssh",
                            "endpoint_id": "endpoint_ssh",
                            "provider": "ssh_process",
                        }
                    ],
                }
            return {
                "environment_id": "env_1",
                "name": "local-env",
                "endpoint_ids": ["endpoint_1"],
                "default_execution_target_id": "target_1",
                "execution_targets": [
                    {
                        "target_id": "target_1",
                        "endpoint_id": "endpoint_1",
                        "provider": "local_process",
                    }
                ],
            }
        if method == "session.create":
            if self.fail_tmux_create and params.get("backend") == "ssh_tmux":
                raise RuntimeError("tmux: command not found")
            return {"session_id": "session_1", "backend": params.get("backend") or "local_pty"}
        if method == "session.acquire_lease":
            return {"lease_id": "lease_1", "session_id": params["session_id"]}
        if method == "session.write":
            return {"session_id": params["session_id"]}
        if method == "session.frames":
            return [{"seq": 7, "kind": "output", "data": "READY\n"}]
        if method == "session.tail":
            return {"session_id": params["session_id"], "text": "READY\n", "last_seq": 7}
        if method == "file.write_text":
            return {"size": len(params["text"])}
        if method == "file.read_text":
            return {"text": "hello"}
        if method == "workspace.create":
            return {"workspace_id": "workspace_1", "name": params["name"]}
        if method == "task.start":
            return {
                "task_id": "task_1",
                "state": "RUNNING",
                "persistent": params["persistent"],
                "command": {"argv": params["argv"]},
            }
        if method == "task.get":
            return {"task_id": params["task_id"], "state": "RUNNING", "exit_code": None}
        if method == "task.logs":
            return [
                {"stream": "stdout", "offset": 0, "chunk": "TASK_READY\n"},
                {"stream": "stdout", "offset": 11, "chunk": "TASK_MORE\n"},
            ]
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
    workspace = client.workspaces.create("workspace")
    write_result = client.files.write_text("endpoint_1", "notes.txt", "hello")
    text = client.files.read_text("endpoint_1", "notes.txt")
    task_text = client.tasks.wait_for_log("task_1", "TASK_READY", timeout_seconds=1)
    frames = list(client.sessions.stream_frames(session["session_id"], max_items=1))

    assert bundle["endpoint"]["endpoint_id"] == "endpoint_1"
    assert session["session_id"] == "session_1"
    assert lease["lease_id"] == "lease_1"
    assert workspace["workspace_id"] == "workspace_1"
    assert write_result["size"] == len("hello")
    assert text == "hello"
    assert "TASK_READY" in task_text
    assert frames == [{"kind": "output", "data": "ready"}]
    assert [method for method, _ in transport.requests] == [
        "env.ensure_local",
        "session.create",
        "session.acquire_lease",
        "session.acquire_lease",
        "session.write",
        "workspace.create",
        "file.write_text",
        "file.read_text",
        "task.logs",
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


@pytest.mark.unit
def test_agent_sdk_semantic_command_policy_selects_remote_persistence() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(transport)

    local_task = client.commands.run("env_1", None, ["python", "-m", "pytest"])
    remote_task = client.commands.run("env_ssh", None, ["bash", "-lc", "pytest"])
    observed = client.tasks.observe("task_1", cursor=len("TASK_READY\n"), max_chars=100)

    start_requests = [
        params for method, params in transport.requests if method == "task.start"
    ]
    assert local_task["sdk"]["persistent"] is False
    assert remote_task["sdk"]["persistent"] is True
    assert start_requests[0]["persistent"] is False
    assert start_requests[1]["persistent"] is True
    assert observed["text"] == "TASK_MORE\n"
    assert observed["cursor"] == len("TASK_READY\nTASK_MORE\n")


@pytest.mark.unit
def test_agent_sdk_terminal_policy_defaults_remote_to_tmux_and_acquires_lease() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(
        transport,
        policy=RuntimePolicy(remote_terminal_backend="ssh_tmux", writer_lease_ttl_seconds=60),
    )

    opened = client.terminals.open("env_ssh", None, ["bash", "-l"])
    observed = client.terminals.observe(opened["session_id"], after_seq=0)
    client.terminals.write(opened["session_id"], "echo ok\n")

    create_request = next(params for method, params in transport.requests if method == "session.create")
    lease_requests = [
        params for method, params in transport.requests if method == "session.acquire_lease"
    ]
    assert opened["backend"] == "ssh_tmux"
    assert opened["lease"]["lease_id"] == "lease_1"
    assert create_request["backend"] == "ssh_tmux"
    assert [request["ttl_seconds"] for request in lease_requests] == [60, 60]
    assert lease_requests[1]["force"] is True
    assert observed["cursor"] == 7
    assert observed["text"] == "READY\n"


@pytest.mark.unit
def test_agent_sdk_terminal_write_can_skip_lease_renewal() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(
        transport,
        policy=RuntimePolicy(renew_terminal_lease_on_write=False),
    )

    client.terminals.write("session_1", "echo ok\n")

    assert [method for method, _ in transport.requests] == ["session.write"]


@pytest.mark.unit
def test_agent_sdk_terminal_write_renews_lease_with_owner_override() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(
        transport,
        policy=RuntimePolicy(writer_lease_ttl_seconds=45),
    )

    client.terminals.write("session_1", "echo ok\n", owner_id="agent-owner")

    assert transport.requests == [
        (
            "session.acquire_lease",
            {
                "session_id": "session_1",
                "ttl_seconds": 45,
                "force": True,
                "owner_id": "agent-owner",
            },
        ),
        (
            "session.write",
            {"session_id": "session_1", "data": "echo ok\n", "owner_id": "agent-owner"},
        ),
    ]


@pytest.mark.unit
def test_agent_sdk_session_write_renews_lease_by_default() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(
        transport,
        policy=RuntimePolicy(writer_lease_ttl_seconds=75),
    )

    client.sessions.write("session_1", "echo ok\n")

    assert transport.requests == [
        (
            "session.acquire_lease",
            {"session_id": "session_1", "ttl_seconds": 75, "force": True},
        ),
        ("session.write", {"session_id": "session_1", "data": "echo ok\n"}),
    ]


@pytest.mark.unit
def test_agent_sdk_session_write_can_skip_lease_renewal() -> None:
    transport = FakeTransport()
    client = AgentRuntimeClient(transport)

    client.sessions.write("session_1", "echo ok\n", renew_lease=False)

    assert transport.requests == [
        ("session.write", {"session_id": "session_1", "data": "echo ok\n"}),
    ]


@pytest.mark.unit
def test_agent_sdk_terminal_policy_reports_tmux_fallback_error() -> None:
    transport = FakeTransport()
    transport.fail_tmux_create = True
    client = AgentRuntimeClient(
        transport,
        policy=RuntimePolicy(remote_terminal_backend="ssh_tmux", allow_ssh_pty_fallback=True),
    )

    opened = client.terminals.open("env_ssh", None, ["bash", "-l"])
    create_backends = [
        params["backend"] for method, params in transport.requests if method == "session.create"
    ]

    assert opened["backend"] == "ssh_pty"
    assert opened["fallback_from"] == "ssh_tmux"
    assert opened["fallback_error"] == "tmux: command not found"
    assert create_backends == ["ssh_tmux", "ssh_pty"]


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
        client.files.write_text(bundle["endpoint"]["endpoint_id"], "agent-sdk.txt", "hello sdk")
        assert client.files.read_text(bundle["endpoint"]["endpoint_id"], "agent-sdk.txt") == "hello sdk"
        assert "agent-sdk.txt" in client.files.list(bundle["endpoint"]["endpoint_id"], ".")
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
        client.sessions.write(session["session_id"], "from-sdk\n", renew_lease=False)
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


@pytest.mark.integration
def test_agent_sdk_session_write_renews_expired_lease_after_agent_restart(
    tmp_path: Path,
) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'agent-sdk-renew.db'}"},
        runtime={"data_dir": tmp_path / "agent-sdk-renew-data"},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    agent_a = AgentRuntimeClient.from_broker(
        address=address,
        settings=settings,
        principal_id="sdk-agent-a",
        policy=RuntimePolicy(writer_lease_ttl_seconds=30),
    )
    agent_b = AgentRuntimeClient.from_broker(
        address=address,
        settings=settings,
        principal_id="sdk-agent-b",
        policy=RuntimePolicy(writer_lease_ttl_seconds=30),
    )
    try:
        bundle = agent_a.environments.ensure_local("sdk-renew-env", tmp_path)
        session = agent_a.sessions.create(
            bundle["environment"]["environment_id"],
            bundle["target_id"],
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "print('RENEW_READY', flush=True)\n"
                    "first = input()\n"
                    "print(f'RENEW_GOT={first}', flush=True)\n"
                ),
            ],
        )
        agent_a.sessions.tail_until(session["session_id"], "RENEW_READY", timeout_seconds=10)
        agent_a.sessions.acquire_lease(
            session["session_id"],
            ttl_seconds=1,
            force=True,
        )
        time.sleep(1.2)

        recovered = next(
            item
            for item in agent_b.sessions.list()
            if item["session_id"] == session["session_id"]
        )
        agent_b.sessions.write(recovered["session_id"], "from-agent-b\n")
        tail = agent_b.sessions.tail_until(
            recovered["session_id"],
            "RENEW_GOT=from-agent-b",
            timeout_seconds=10,
        )

        assert "RENEW_GOT=from-agent-b" in tail["text"]
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                agent_a.broker.shutdown()
        agent_a.close()
        agent_b.close()
        thread.join(timeout=10)


@pytest.mark.integration
def test_agent_sdk_tracks_local_persistent_task_over_broker_transport(tmp_path: Path) -> None:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'agent-sdk-task.db'}"},
        runtime={"data_dir": tmp_path / "agent-sdk-task-data", "detached_poll_interval_seconds": 0.1},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = AgentRuntimeClient.from_broker(address=address, settings=settings, principal_id="sdk-agent")
    try:
        bundle = client.environments.ensure_local("sdk-task-env", tmp_path)
        task = client.tasks.start(
            bundle["environment"]["environment_id"],
            bundle["target_id"],
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import time\n"
                    "for i in range(4):\n"
                    "    print(f'SDK_TASK_TICK={i}', flush=True)\n"
                    "    time.sleep(0.25)\n"
                ),
            ],
            persistent=True,
        )

        mid_logs = client.tasks.wait_for_log(task["task_id"], "SDK_TASK_TICK=1", timeout_seconds=10)
        mid_state = client.tasks.get(task["task_id"])
        final = client.tasks.wait(task["task_id"], timeout_seconds=10)
        final_logs = client.tasks.logs_text(task["task_id"])

        assert "SDK_TASK_TICK=1" in mid_logs
        assert mid_state["state"] in {"RUNNING", "SUCCEEDED"}
        assert final["state"] == "SUCCEEDED"
        assert final["exit_code"] == 0
        assert "SDK_TASK_TICK=3" in final_logs
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
