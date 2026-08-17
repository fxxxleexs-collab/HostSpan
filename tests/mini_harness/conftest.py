from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient
from mini_harness.config import SSHRuntimeConfig


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
        self.task_pid = 12345
        self.task_state = "SUCCEEDED"
        self.task_exit_code = 0
        self.logs = [{"chunk": ".\n1 passed\n"}]
        self.tmux_present = False
        self.tmux_install_requires_password = False
        self.tmux_manual_password_failures = 0
        self.terminal_fallback = False
        self.sessions: dict[str, dict[str, Any]] = {}
        self.session_created_at = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)

    def ensure_local(self, name: str, root: str) -> dict[str, Any]:
        self.requests.append(("ensure_local", {"name": name, "root": root}))
        return {
            "endpoint": {"endpoint_id": "endpoint_1"},
            "environment": {"environment_id": "env_1"},
            "target_id": "target_1",
        }

    def put_secret(self, value: str, purpose: str = "runtime") -> str:
        self.requests.append(("put_secret", {"purpose": purpose, "has_value": bool(value)}))
        return "secret:test"

    def delete_secret(self, secret_ref: str) -> bool:
        self.requests.append(("delete_secret", {"secret_ref": secret_ref}))
        return True

    def ensure_ssh(
        self,
        name: str,
        ssh: SSHRuntimeConfig,
        password_secret_ref: str | None = None,
        trust_host_once: bool = False,
    ) -> dict[str, Any]:
        request = {
            "name": name,
            "hostname": ssh.hostname,
            "auth_method": ssh.auth_method,
            "has_password_secret_ref": password_secret_ref is not None,
        }
        if trust_host_once:
            request["trust_host_once"] = True
        self.requests.append(("ensure_ssh", request))
        return {
            "endpoint": {"endpoint_id": "endpoint_ssh"},
            "environment": {"environment_id": "env_ssh"},
            "target_id": "target_ssh",
        }

    def list_files(self, endpoint_id: str, path: str, recursive: bool = False) -> list[str]:
        self.requests.append(
            ("list_files", {"endpoint_id": endpoint_id, "path": path, "recursive": recursive})
        )
        return sorted(self.files)

    def ensure_dir(self, endpoint_id: str, path: str) -> dict[str, Any]:
        self.requests.append(("ensure_dir", {"endpoint_id": endpoint_id, "path": path}))
        return {"endpoint_id": endpoint_id, "path": path}

    def read_text(self, endpoint_id: str, path: str) -> str:
        self.requests.append(("read_text", {"endpoint_id": endpoint_id, "path": path}))
        return self.files.get(path, self.files[Path(path).name])

    def write_text(self, endpoint_id: str, path: str, text: str) -> dict[str, Any]:
        self.requests.append(("write_text", {"endpoint_id": endpoint_id, "path": path}))
        self.files[Path(path).name] = text
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
        return {"task_id": self.task_id, "state": "RUNNING", "pid": self.task_pid, "persistent": persistent}

    def get_task(self, task_id: str) -> dict[str, Any]:
        self.requests.append(("get_task", {"task_id": task_id}))
        return {
            "task_id": task_id,
            "state": self.task_state,
            "exit_code": self.task_exit_code,
            "pid": self.task_pid,
        }

    def task_logs(self, task_id: str) -> list[dict[str, Any]]:
        self.requests.append(("task_logs", {"task_id": task_id}))
        return self.logs

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        self.requests.append(("cancel_task", {"task_id": task_id}))
        return {"task_id": task_id, "state": "CANCELLED", "pid": self.task_pid}

    def run_command(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
    ) -> dict[str, Any]:
        command_text = " ".join(argv)
        self.requests.append(
            (
                "run_command",
                {
                    "environment_id": environment_id,
                    "target_id": target_id,
                    "argv": argv,
                    "cwd": cwd,
                },
            )
        )
        if "ENVRT_TOOL" in command_text:
            if self.tmux_present:
                self.task_state = "SUCCEEDED"
                self.task_exit_code = 0
                self.logs = [{"chunk": "ENVRT_TOOL_PRESENT tmux\ntmux 3.4\n"}]
            elif "apt-get" in command_text:
                if self.tmux_install_requires_password:
                    self.task_state = "FAILED"
                    self.task_exit_code = 8
                    self.logs = [
                        {
                            "chunk": (
                                "ENVRT_TOOL_MISSING tmux\n"
                                "ENVRT_TOOL_INSTALL_FAILED tmux: sudo password or elevated privileges are required\n"
                            )
                        }
                    ]
                else:
                    self.tmux_present = True
                    self.task_state = "SUCCEEDED"
                    self.task_exit_code = 0
                    self.logs = [
                        {
                            "chunk": (
                                "ENVRT_TOOL_MISSING tmux\nENVRT_TOOL_INSTALLED tmux\ntmux 3.4\n"
                            )
                        }
                    ]
            else:
                self.task_state = "FAILED"
                self.task_exit_code = 7
                self.logs = [{"chunk": "ENVRT_TOOL_MISSING tmux\n"}]
        return {"task_id": self.task_id, "state": "RUNNING", "pid": self.task_pid}

    def observe_task(
        self,
        task_id: str,
        cursor: int,
        max_chars: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        self.requests.append(
            (
                "observe_task",
                {
                    "task_id": task_id,
                    "cursor": cursor,
                    "max_chars": max_chars,
                    "wait_seconds": wait_seconds,
                },
            )
        )
        text = "".join(str(item.get("chunk", "")) for item in self.logs)
        return {
            "task": {
                "task_id": task_id,
                "state": self.task_state,
                "exit_code": self.task_exit_code,
                "pid": self.task_pid,
            },
            "text": text[cursor:],
            "cursor": len(text),
            "truncated": False,
            "state": self.task_state,
            "exit_code": self.task_exit_code,
            "is_terminal": self.task_state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"},
        }

    def open_terminal(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str,
        cols: int,
        rows: int,
    ) -> dict[str, Any]:
        self.requests.append(
            (
                "open_terminal",
                {
                    "environment_id": environment_id,
                    "target_id": target_id,
                    "argv": argv,
                    "cwd": cwd,
                    "cols": cols,
                    "rows": rows,
                },
            )
        )
        is_remote = target_id.endswith("_ssh")
        command_text = " ".join(argv)
        manual_tmux_install = "ENVRT_SUDO_PASSWORD_PROMPT" in command_text
        if self.terminal_fallback and is_remote:
            session = {
                "session_id": "session_1",
                "backend": "ssh_pty",
                "state": "ACTIVE",
                "environment_id": environment_id,
                "target_id": target_id,
                "command": argv,
                "default_cwd": cwd,
                "interaction_state": "AUTOMATION_CONTROLLED",
                "backend_ref": {"backend": "ssh_pty", "endpoint_id": "endpoint_ssh"},
                "fallback_from": "ssh_tmux",
                "fallback_error": "tmux: command not found",
                "lease": {"lease_id": "lease_1"},
                "target_provider": "ssh_process",
                "created_at": self.session_created_at.isoformat(),
                "updated_at": (self.session_created_at + timedelta(minutes=1)).isoformat(),
                "manual_tmux_install": manual_tmux_install,
                "password_sent": False,
            }
            self.sessions["session_1"] = dict(session)
            return session
        session = {
            "session_id": "session_1",
            "backend": "ssh_tmux" if is_remote else "local_pty",
            "state": "ACTIVE",
            "environment_id": environment_id,
            "target_id": target_id,
            "command": argv,
            "default_cwd": cwd,
            "interaction_state": "AUTOMATION_CONTROLLED",
            "backend_ref": {
                "backend": "ssh_tmux" if is_remote else "local_pty",
                "endpoint_id": "endpoint_ssh" if is_remote else "endpoint_1",
                "tmux_session": "envrt_session_1" if is_remote else None,
                "tmux_target": "envrt_session_1:0.0" if is_remote else None,
            },
            "lease": {"lease_id": "lease_1"},
            "target_provider": "ssh_process" if is_remote else "local_process",
            "created_at": self.session_created_at.isoformat(),
            "updated_at": (self.session_created_at + timedelta(minutes=1)).isoformat(),
            "manual_tmux_install": manual_tmux_install,
            "password_sent": False,
        }
        self.sessions["session_1"] = dict(session)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        self.requests.append(("list_sessions", {}))
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.requests.append(("get_session", {"session_id": session_id}))
        return self.sessions.get(
            session_id,
            {
                "session_id": session_id,
                "state": "ACTIVE",
                "backend": "local_pty",
                "environment_id": "env_1",
                "target_id": "target_1",
                "command": ["bash", "-l"],
                "default_cwd": "/project",
                "interaction_state": "AUTOMATION_CONTROLLED",
                "backend_ref": {"backend": "local_pty", "endpoint_id": "endpoint_1"},
                "created_at": self.session_created_at.isoformat(),
                "updated_at": self.session_created_at.isoformat(),
            },
        )

    def observe_terminal(
        self,
        session_id: str,
        after_seq: int | None,
        limit_chars: int,
    ) -> dict[str, Any]:
        self.requests.append(
            (
                "observe_terminal",
                {"session_id": session_id, "after_seq": after_seq, "limit_chars": limit_chars},
            )
        )
        session = self.sessions.get(session_id, {})
        if session.get("manual_tmux_install"):
            failures_left = int(session.get("password_failures_left", 0))
            if session.get("password_sent") and failures_left > 0:
                session["password_failures_left"] = failures_left - 1
                session["password_sent"] = False
                text = "Sorry, try again.\nENVRT_SUDO_PASSWORD_PROMPT\n"
                seq = 2
            elif session.get("password_sent"):
                text = "ENVRT_SUDO_AUTH_OK\nENVRT_TOOL_INSTALLED tmux\ntmux 3.4\n"
                seq = 3
            else:
                text = "ENVRT_TOOL_MISSING tmux\nENVRT_SUDO_PASSWORD_PROMPT\n"
                seq = 1
            frames = [] if after_seq is not None and after_seq >= seq else [{"seq": seq, "data": text}]
            return {
                "session_id": session_id,
                "frames": frames,
                "text": text,
                "cursor": seq,
            }
        frames = [] if after_seq is not None and after_seq >= 1 else [{"seq": 1, "data": "TERMINAL_READY\n"}]
        return {
            "session_id": session_id,
            "frames": frames,
            "text": "TERMINAL_READY\n",
            "cursor": 1,
        }

    def write_terminal(self, session_id: str, data: str) -> dict[str, Any]:
        self.requests.append(("write_terminal", {"session_id": session_id, "data": data}))
        if session_id in self.sessions and self.sessions[session_id].get("manual_tmux_install"):
            self.sessions[session_id]["password_sent"] = True
            self.sessions[session_id].setdefault(
                "password_failures_left", self.tmux_manual_password_failures
            )
            self.tmux_present = True
        return {"session_id": session_id}

    def close_terminal(self, session_id: str) -> dict[str, Any]:
        self.requests.append(("close_terminal", {"session_id": session_id}))
        if session_id in self.sessions:
            self.sessions[session_id]["state"] = "TERMINATED"
            self.sessions[session_id]["interaction_state"] = "NONE"
            self.sessions[session_id]["updated_at"] = (
                self.session_created_at + timedelta(minutes=2)
            ).isoformat()
        return {"session_id": session_id, "state": "TERMINATED"}


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
