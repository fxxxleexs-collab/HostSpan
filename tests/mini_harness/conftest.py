from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    _ = config
    return "sample_project" in collection_path.parts


class FakeHarnessRuntime:
    def __init__(self) -> None:
        self.files = {
            "calculator.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
            "test_calculator.py": "from calculator import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n",
        }
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.task_id = "task_1"
        self.task_state = "SUCCEEDED"
        self.task_exit_code = 0
        self.logs = [{"chunk": ".\n1 passed\n"}]

    def ensure_local(self, name: str, root: str) -> dict[str, Any]:
        self.requests.append(("ensure_local", {"name": name, "root": root}))
        return {
            "endpoint": {"endpoint_id": "endpoint_1"},
            "environment": {"environment_id": "env_1"},
            "target_id": "target_1",
        }

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]:
        self.requests.append(
            ("list_files", {"endpoint_id": endpoint_id, "path": path, "recursive": recursive})
        )
        return sorted(self.files)

    def read_text(self, endpoint_id: str, path: str) -> str:
        self.requests.append(("read_text", {"endpoint_id": endpoint_id, "path": path}))
        return self.files[path]

    def write_text(self, endpoint_id: str, path: str, text: str) -> dict[str, Any]:
        self.requests.append(("write_text", {"endpoint_id": endpoint_id, "path": path}))
        self.files[path] = text
        return {"size": len(text.encode("utf-8"))}

    def start_task(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        persistent: bool,
    ) -> dict[str, Any]:
        self.requests.append(
            (
                "start_task",
                {
                    "environment_id": environment_id,
                    "target_id": target_id,
                    "argv": argv,
                    "cwd": cwd,
                    "persistent": persistent,
                },
            )
        )
        return {"task_id": self.task_id, "state": "RUNNING"}

    def get_task(self, task_id: str) -> dict[str, Any]:
        self.requests.append(("get_task", {"task_id": task_id}))
        return {"task_id": task_id, "state": self.task_state, "exit_code": self.task_exit_code}

    def task_logs(self, task_id: str) -> list[dict[str, Any]]:
        self.requests.append(("task_logs", {"task_id": task_id}))
        return self.logs

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        self.requests.append(("cancel_task", {"task_id": task_id}))
        return {"task_id": task_id, "state": "CANCELLED"}


@pytest.fixture
def fake_runtime() -> FakeHarnessRuntime:
    return FakeHarnessRuntime()


@pytest.fixture
def sample_project_copy(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "sample_project"
    destination = tmp_path / "sample_project"
    shutil.copytree(source, destination)
    return destination


@pytest.fixture
def broker_client(
    tmp_path: Path,
) -> Iterator[tuple[AgentRuntimeClient, BrokerAddress, RuntimeSettings]]:
    address = _test_address(tmp_path)
    settings = RuntimeSettings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'mini-harness.db'}"},
        runtime={"data_dir": tmp_path / "mini-harness-data", "detached_poll_interval_seconds": 0.1},
        security={"allowed_local_roots": [tmp_path]},
    )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    assert server.ready.wait(timeout=10)
    client = AgentRuntimeClient.from_broker(
        address=address, settings=settings, principal_id="mini-test"
    )
    try:
        yield client, address, settings
    finally:
        if thread.is_alive():
            with contextlib.suppress(Exception):
                client.broker.shutdown()
        client.close()
        thread.join(timeout=10)


def _test_address(tmp_path: Path) -> BrokerAddress:
    if os.name == "nt":
        return BrokerAddress(rf"\\.\pipe\mini-harness-test-{uuid4().hex}", "AF_PIPE")
    return BrokerAddress(str(tmp_path / "broker.sock"), "AF_UNIX")
