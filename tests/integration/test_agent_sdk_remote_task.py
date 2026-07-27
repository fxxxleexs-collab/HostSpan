from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient

pytestmark = pytest.mark.integration


def test_agent_sdk_tracks_remote_persistent_task_across_broker_restart(tmp_path: Path) -> None:
    if os.environ.get("ENVRT_TEST_SSH_DOCKER") != "1":
        pytest.skip("set ENVRT_TEST_SSH_DOCKER=1 to run the real SSH Docker task test")
    key = Path(os.environ.get("ENVRT_TEST_SSH_KEY", "manual_ssh_test/envrt_test_key")).resolve()
    known_hosts = Path(
        os.environ.get("ENVRT_TEST_SSH_KNOWN_HOSTS", "manual_ssh_test/known_hosts")
    ).resolve()
    if not key.exists() or not known_hosts.exists():
        pytest.skip("manual SSH key/known_hosts files are not available")

    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'agent-sdk-remote-task.db'}"},
        runtime={
            "data_dir": tmp_path / "agent-sdk-remote-task-data",
            "detached_poll_interval_seconds": 0.2,
        },
    )
    marker = uuid4().hex[:8]
    remote_probe = f".environment-runtime/sdk-remote-task-{marker}/probe.txt"
    server, thread = _start_server(settings, address)
    client = AgentRuntimeClient.from_broker(address=address, settings=settings, principal_id="sdk-agent")
    task_id = ""
    try:
        bundle = client.environments.ensure_ssh(
            name=f"sdk-ssh-task-{marker}",
            hostname=os.environ.get("ENVRT_TEST_SSH_HOST", "127.0.0.1"),
            username=os.environ.get("ENVRT_TEST_SSH_USER", "envrt"),
            known_hosts_file=known_hosts,
            port=int(os.environ.get("ENVRT_TEST_SSH_PORT", "2222")),
            identity_file=key,
            use_ssh_agent=False,
        )
        endpoint_id = bundle["endpoint"]["endpoint_id"]
        client.files.write_text(endpoint_id, remote_probe, f"probe-{marker}")
        assert client.files.read_text(endpoint_id, remote_probe) == f"probe-{marker}"

        script = (
            "i=0; "
            "while [ $i -lt 8 ]; do "
            "echo SDK_REMOTE_TASK_TICK=$i; "
            "i=$((i+1)); "
            "sleep 1; "
            "done"
        )
        task = client.tasks.start(
            bundle["environment"]["environment_id"],
            bundle["target_id"],
            ["bash", "-lc", script],
            persistent=True,
        )
        task_id = task["task_id"]

        before_restart_logs = client.tasks.wait_for_log(
            task_id,
            "SDK_REMOTE_TASK_TICK=1",
            timeout_seconds=20,
        )
        before_restart = client.tasks.get(task_id)
        assert "SDK_REMOTE_TASK_TICK=1" in before_restart_logs
        assert before_restart["state"] in {"RUNNING", "SUCCEEDED"}
    finally:
        with contextlib.suppress(Exception):
            client.broker.shutdown()
        client.close()
        thread.join(timeout=10)

    time.sleep(2.0)

    restart_address = _test_address(tmp_path)
    server, thread = _start_server(settings, restart_address)
    client = AgentRuntimeClient.from_broker(
        address=restart_address,
        settings=settings,
        principal_id="sdk-agent",
    )
    try:
        recovered = client.tasks.get(task_id)
        assert recovered["state"] in {"RUNNING", "SUCCEEDED"}
        after_restart_logs = client.tasks.wait_for_log(
            task_id,
            "SDK_REMOTE_TASK_TICK=5",
            timeout_seconds=20,
        )
        final = client.tasks.wait(task_id, timeout_seconds=30)
        final_logs = client.tasks.logs_text(task_id)

        assert "SDK_REMOTE_TASK_TICK=5" in after_restart_logs
        assert final["state"] == "SUCCEEDED"
        assert final["exit_code"] == 0
        assert "SDK_REMOTE_TASK_TICK=7" in final_logs
    finally:
        with contextlib.suppress(Exception):
            client.broker.shutdown()
        client.close()
        thread.join(timeout=10)
        _ = server


def _start_server(
    settings: RuntimeSettings,
    address: BrokerAddress,
) -> tuple[LocalBrokerServer, threading.Thread]:
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    return server, thread


def _test_address(tmp_path: Path) -> BrokerAddress:
    if os.name == "nt":
        return BrokerAddress(rf"\\.\pipe\environment-runtime-agent-remote-test-{uuid4().hex}", "AF_PIPE")
    return BrokerAddress(str(tmp_path / "broker.sock"), "AF_UNIX")
