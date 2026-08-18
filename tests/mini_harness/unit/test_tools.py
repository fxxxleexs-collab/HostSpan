from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mini_harness.agent.controller import AgentController
from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.state import AgentState
from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig, RuntimeConfig, SSHRuntimeConfig
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.permissions import PermissionsConfig
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.config import SyncConfig
from mini_harness.tools.adapter import build_runtime_tools as build_facade_runtime_tools
from mini_harness.tools.registry import ToolRegistry
from mini_harness.tools.runtime.builder import build_internal_runtime_tools as build_runtime_tools
from mini_harness.workspace import SandboxConfig, SandboxTargetConfig


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
        sandbox_config=SandboxConfig(
            local=SandboxTargetConfig(allow_root_shell=True, allow_package_install=True)
        ),
    )


def _facade_registry(fake_runtime) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_facade_runtime_tools(fake_runtime):
        registry.register(tool)
    return registry


class FakeInteractiveApprovalHandler:
    def __init__(self, *, approved: bool = True, secret: str | None = "test-password") -> None:
        self.approved = approved
        self.secret = secret
        self.requests: list[ToolApprovalRequest] = []
        self.secret_prompts: list[str] = []

    async def approve(self, request: ToolApprovalRequest) -> bool:
        self.requests.append(request)
        return self.approved

    async def prompt_secret(self, prompt: str) -> str | None:
        self.secret_prompts.append(prompt)
        return self.secret

    async def prompt_ssh_connection(
        self,
        *,
        reason: str,
        default_name: str,
    ) -> RuntimeConfig | None:
        self.secret_prompts.append(f"ssh_connection:{reason}:{default_name}")
        return RuntimeConfig(
            mode="ssh",
            name=default_name,
            ssh=SSHRuntimeConfig(
                hostname="example.test",
                username="envrt",
                known_hosts_file="known_hosts",
                auth_method="password",
                use_ssh_agent=False,
                remote_root="/srv/app",
            ),
        )


@pytest.mark.asyncio
async def test_runtime_tools_map_to_runtime_client(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    listed = await registry.execute("list_files", {"path": "."}, context)
    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    write = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        context,
    )
    run = await registry.execute(
        "run_command",
        {"argv": ["python", "-m", "pytest", "-q"], "cwd": "."},
        context,
    )

    assert listed.ok
    assert "calculator.py" in str(listed.content)
    assert read.ok
    assert "return a - b" in str(read.content)
    calculator_bytes = b"def add(a: int, b: int) -> int:\n    return a - b\n"
    expected_hash = hashlib.sha256(calculator_bytes).hexdigest()
    assert read.metadata["sha256"] == expected_hash
    assert read.metadata["size"] == len(calculator_bytes)
    assert read.metadata["line_count"] == 2
    assert read.metadata["selected_line_count"] == 2
    assert read.metadata["newline"] == "lf"
    assert read.metadata["encoding"] == "utf-8"
    assert context.file_snapshot("calculator.py") is not None
    assert write.ok
    assert write.metadata["hash_guarded"] is True
    assert write.metadata["expected_source"] == "recent_read_snapshot"
    assert write.metadata["before_sha256"] == expected_hash
    assert write.metadata["after_sha256"] != expected_hash
    assert write.metadata["diff"]["changed"] is True
    assert "-def add(a: int, b: int) -> int:" in str(write.content)
    assert "+def add(a, b):" in str(write.content)
    assert context.file_snapshot("calculator.py").sha256 == write.metadata["after_sha256"]
    assert run.resource_ref == "task:task_1"
    assert run.metadata["pid"] == 12345
    assert run.metadata["persistent"] is False
    assert run.metadata["exit_code"] == 0
    assert run.metadata["timed_out"] is False
    assert run.metadata["log_tail"] == ".\n1 passed\n"
    assert run.cursor == len(".\n1 passed\n")
    assert run.content == ".\n1 passed\n"
    assert context.active_task_id is None
    assert [name for name, _ in fake_runtime.requests] == [
        "list_files",
        "read_text",
        "read_text",
        "write_text",
        "run_command",
        "observe_task",
    ]


@pytest.mark.asyncio
async def test_facade_tools_are_default_runtime_tools(fake_runtime) -> None:
    registry = _facade_registry(fake_runtime)
    context = _context()

    read = await registry.execute(
        "file",
        {"action": "read", "path": "calculator.py"},
        context,
    )
    command = await registry.execute(
        "command",
        {"action": "run", "argv": ["python", "-m", "pytest", "-q"]},
        context,
    )

    assert read.ok
    assert read.metadata["inner_tool"] == "read_file"
    assert command.ok
    assert command.metadata["inner_tool"] == "run_command"
    assert command.metadata["exit_code"] == 0
    assert command.metadata["timed_out"] is False
    assert ".\n1 passed\n" in str(command.content)


@pytest.mark.asyncio
async def test_terminal_facade_opens_sends_and_observes(fake_runtime) -> None:
    registry = _facade_registry(fake_runtime)
    context = _context()

    opened = await registry.execute("terminal", {"action": "open"}, context)
    sent = await registry.execute("terminal", {"action": "command", "data": "echo ok"}, context)
    observed = await registry.execute("terminal", {"action": "observe"}, context)

    assert opened.ok
    assert opened.metadata["inner_tool"] == "open_terminal"
    assert sent.ok
    assert sent.metadata["inner_tool"] == "terminal_command"
    assert observed.ok
    assert observed.metadata["inner_tool"] == "observe_terminal"
    assert (
        "write_terminal",
        {"session_id": "session_1", "data": "echo ok\n"},
    ) in fake_runtime.requests


@pytest.mark.asyncio
async def test_file_facade_preserves_diff_preview_tool_name(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(approved=True)
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_facade_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "file",
        {"action": "write", "path": "calculator.py", "content": "print('ok')\n"},
        context,
    )

    assert result.ok
    assert approval.requests[0].tool_name == "file"
    assert approval.requests[0].arguments["action"] == "write"
    assert approval.requests[0].preview_kind == "diff"


@pytest.mark.asyncio
async def test_file_facade_read_then_edit_uses_recent_snapshot(fake_runtime) -> None:
    registry = _facade_registry(fake_runtime)
    context = _context()

    read = await registry.execute("file", {"action": "read", "path": "calculator.py"}, context)
    edited = await registry.execute(
        "file",
        {
            "action": "edit",
            "path": "calculator.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
        },
        context,
    )

    assert read.ok
    assert edited.ok
    assert edited.metadata["expected_source"] == "recent_read_snapshot"
    assert edited.metadata["expected_sha256"] == read.metadata["sha256"]
    assert "return a + b" in fake_runtime.files["calculator.py"]


@pytest.mark.asyncio
async def test_start_task_records_managed_long_running_task(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    fake_runtime.task_state = "RUNNING"
    fake_runtime.task_exit_code = None
    fake_runtime.logs = [{"chunk": " * Running on http://127.0.0.1:5000\n"}]

    started = await registry.execute(
        "start_task",
        {
            "argv": ["python", "app.py"],
            "cwd": ".",
            "wait_seconds": 0.1,
            "brief": "Flask test server is starting and should be observed for readiness",
        },
        context,
    )
    listed = await registry.execute("list_tasks", {"state_filter": "active"}, context)
    cancelled = await registry.execute("cancel_task", {"task_ref": "task:task_1"}, context)

    assert started.ok
    assert started.resource_ref == "task:task_1"
    assert started.metadata["pid"] == 12345
    assert started.metadata["persistent"] is True
    assert "Running on" in str(started.content)
    assert listed.ok
    assert listed.metadata["task_count"] == 1
    assert listed.metadata["tasks"][0]["pid"] == 12345
    assert listed.metadata["tasks"][0]["persistent"] is True
    assert (
        listed.metadata["tasks"][0]["brief"]
        == "Flask test server is starting and should be observed for readiness"
    )
    assert "python app.py" in str(listed.content)
    assert "brief=Flask test server is starting" in str(listed.content)
    assert cancelled.ok
    assert cancelled.metadata["pid"] == 12345
    transitions = context.recent_runtime_transitions()
    assert transitions[0]["kind"] == "task"
    assert transitions[0]["action"] == "cancel"
    assert transitions[0]["ref"] == "task:task_1"
    assert transitions[0]["active_after"] == "none"
    assert any(
        transition["kind"] == "task" and transition["action"] == "start"
        for transition in transitions
    )
    assert [name for name, _ in fake_runtime.requests] == [
        "start_task",
        "observe_task",
        "cancel_task",
    ]


@pytest.mark.asyncio
async def test_runtime_tools_map_remote_paths_and_terminal_tools(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    listed = await registry.execute("list_files", {"path": "."}, context)
    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    run = await registry.execute(
        "run_command",
        {"argv": ["bash", "-lc", "pytest -q"], "cwd": "."},
        context,
    )
    opened = await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    observed = await registry.execute("observe_terminal", {}, context)
    sent = await registry.execute("terminal_command", {"data": "echo ok\n"}, context)
    closed = await registry.execute("close_terminal", {}, context)

    assert listed.ok
    assert read.ok
    assert run.metadata["cwd"] == "/srv/app"
    assert opened.resource_ref == "session:session_1"
    assert opened.metadata["target"] == "remote"
    assert opened.metadata["backend"] == "ssh_tmux"
    assert "TERMINAL_READY" in str(observed.content)
    assert sent.ok
    assert closed.state == "TERMINATED"
    assert context.active_session_id is None
    assert context.session_brief("session_1") is not None
    assert context.session_brief("session_1").runtime_state == "TERMINATED"
    assert context.session_brief("session_1").pending is False
    assert context.session_brief("session_1").history_only is True
    transitions = context.recent_runtime_transitions()
    assert transitions[0]["kind"] == "terminal"
    assert transitions[0]["action"] == "close"
    assert transitions[0]["ref"] == "session:session_1"
    assert transitions[0]["active_after"] == "none"
    assert any(
        transition["kind"] == "terminal" and transition["action"] == "open"
        for transition in transitions
    )
    assert fake_runtime.requests[0][1]["path"] == "/srv/app"
    assert fake_runtime.requests[1][1]["path"] == "/srv/app/calculator.py"
    assert fake_runtime.requests[2][1]["cwd"] == "/srv/app"


@pytest.mark.asyncio
async def test_request_human_terminal_input_submits_hidden_input_with_enter(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(secret="s3cr3t")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    context.approval_handler = approval

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "request_human_terminal_input",
        {"prompt": "Password for sudo"},
        context,
    )

    assert result.ok
    assert approval.secret_prompts == ["Password for sudo"]
    assert result.metadata["hidden_input"] is True
    assert result.metadata["submitted"] is True
    assert "s3cr3t" not in result.summary
    assert "s3cr3t" not in str(result.metadata)
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "s3cr3t\n"},
    )
    assert context.last_terminal_input == "s3cr3t\n"
    assert context.terminal_input_pending is True


@pytest.mark.asyncio
async def test_read_file_metadata_keeps_full_snapshot_for_line_window(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "read_file",
        {"path": "test_calculator.py", "start_line": 4, "end_line": 5},
        context,
    )

    assert result.ok
    assert "4 | def test_add()" in str(result.content)
    assert result.metadata["line_count"] == 5
    assert result.metadata["selected_line_count"] == 2
    snapshot = context.file_snapshot("test_calculator.py")
    assert snapshot is not None
    assert snapshot.line_count == 5
    assert snapshot.sha256 == result.metadata["sha256"]


@pytest.mark.asyncio
async def test_read_file_supports_line_blocks_with_next_start(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    first = await registry.execute(
        "read_file",
        {"path": "test_calculator.py", "start_line": 1, "max_lines": 2},
        context,
    )
    second = await registry.execute(
        "read_file",
        {
            "path": "test_calculator.py",
            "start_line": first.metadata["next_start_line"],
            "max_lines": 10,
        },
        context,
    )

    assert first.ok
    assert "1 | from calculator import add" in str(first.content)
    assert first.metadata["start_line"] == 1
    assert first.metadata["end_line"] == 2
    assert first.metadata["selected_line_count"] == 2
    assert first.metadata["has_more"] is True
    assert first.metadata["next_start_line"] == 3
    assert second.ok
    assert second.metadata["start_line"] == 3
    assert second.metadata["end_line"] == 5
    assert second.metadata["has_more"] is False
    assert second.metadata["next_start_line"] is None


@pytest.mark.asyncio
async def test_read_file_rejects_end_line_with_max_lines(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "read_file",
        {"path": "calculator.py", "start_line": 1, "end_line": 2, "max_lines": 2},
        context,
    )

    assert not result.ok
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert "cannot be used together" in result.summary


@pytest.mark.asyncio
async def test_read_file_can_target_local_in_ssh_runtime(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
    )

    result = await registry.execute(
        "read_file",
        {"path": "calculator.py", "target": "local"},
        context,
    )

    assert result.ok
    assert result.metadata["target"] == "local"
    assert result.metadata["file_location"]["endpoint_id"] == "endpoint_1"
    assert fake_runtime.requests[-1] == (
        "read_text",
        {"endpoint_id": "endpoint_1", "path": "calculator.py"},
    )


@pytest.mark.asyncio
async def test_file_read_sync_reads_local_and_reports_mirror_status(
    fake_runtime,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('local')\n", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute(
        "read_file",
        {"target": "sync", "path": "src/app.py"},
        context,
    )

    assert result.ok
    assert "print('local')" in str(result.content)
    assert result.metadata["target"] == "sync"
    assert result.metadata["file_location"]["backend"] == "local-disk"
    assert result.metadata["sync"]["state"] == "LOCAL_AHEAD"
    assert result.metadata["sync"]["path_status"]["state"] == "LOCAL_AHEAD"
    assert result.metadata["sync"]["recommended_action"].startswith('sync action="push"')


@pytest.mark.asyncio
async def test_list_files_sync_lists_local_and_reports_mirror_status(
    fake_runtime,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('local')\n", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute(
        "list_files",
        {"target": "sync", "path": ".", "recursive": True},
        context,
    )

    assert result.ok
    assert "src/app.py" in str(result.content)
    assert result.metadata["target"] == "sync"
    assert result.metadata["sync"]["state"] == "LOCAL_AHEAD"
    assert result.metadata["sync"]["diff"]["uploads"] == ["src/app.py"]


@pytest.mark.asyncio
async def test_list_files_can_target_local_in_ssh_runtime(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
    )

    result = await registry.execute(
        "list_files",
        {"path": ".", "target": "local"},
        context,
    )

    assert result.ok
    assert result.metadata["target"] == "local"
    assert fake_runtime.requests[-1] == (
        "list_files",
        {"endpoint_id": "endpoint_1", "path": ".", "recursive": False},
    )


@pytest.mark.asyncio
async def test_write_file_allows_unguarded_write_without_recent_snapshot(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        context,
    )

    assert result.ok
    assert result.metadata["hash_guarded"] is False
    assert result.metadata["unguarded_write"] is True
    assert result.metadata["expected_sha256"] is None
    assert result.metadata["diff"]["changed"] is True
    assert "calculator.py:" in str(result.content)
    assert [name for name, _ in fake_runtime.requests] == ["read_text", "write_text"]


@pytest.mark.asyncio
async def test_write_file_uses_explicit_expected_sha256(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    before = hashlib.sha256(
        b"def add(a: int, b: int) -> int:\n    return a - b\n"
    ).hexdigest()

    result = await registry.execute(
        "write_file",
        {
            "path": "calculator.py",
            "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "expected_sha256": before,
        },
        context,
    )

    assert result.ok
    assert result.metadata["hash_guarded"] is True
    assert result.metadata["expected_source"] == "argument"
    assert result.metadata["expected_sha256"] == before
    assert result.metadata["before_sha256"] == before


@pytest.mark.asyncio
async def test_write_file_rejects_when_expected_sha256_is_stale(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "write_file",
        {
            "path": "calculator.py",
            "content": "def add(a, b):\n    return a + b\n",
            "expected_sha256": "0" * 64,
        },
        context,
    )

    assert not result.ok
    assert result.error_code == "FILE_CHANGED"
    assert result.metadata["expected_sha256"] == "0" * 64
    assert result.metadata["actual_sha256"] != "0" * 64
    assert result.metadata["recommended_action"] == 'file action="read"'
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_write_file_falls_back_to_recent_snapshot_when_argument_hash_is_wrong(
    fake_runtime,
) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    result = await registry.execute(
        "write_file",
        {
            "path": "calculator.py",
            "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "expected_sha256": "0" * 64,
        },
        context,
    )

    assert read.ok
    assert result.ok
    assert result.metadata["expected_source"] == "recent_read_snapshot"
    assert result.metadata["expected_sha256"] == read.metadata["sha256"]
    assert result.metadata["ignored_expected_sha256"] == "0" * 64


@pytest.mark.asyncio
async def test_write_file_rejects_when_recent_read_snapshot_is_stale(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    assert read.ok
    fake_runtime.files["calculator.py"] = "def add(a, b):\n    return 999\n"
    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        context,
    )

    assert not result.ok
    assert result.error_code == "FILE_CHANGED"
    assert result.metadata["expected_source"] == "recent_read_snapshot"
    assert result.metadata["expected_sha256"] == read.metadata["sha256"]
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_write_file_creates_new_file_with_diff_preview(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "write_file",
        {"path": "new_module.py", "content": "VALUE = 42\n"},
        context,
    )

    assert result.ok
    assert result.metadata["existed_before"] is False
    assert result.metadata["hash_guarded"] is False
    assert result.metadata["diff"]["added_lines"] == 1
    assert result.metadata["diff"]["removed_lines"] == 0
    assert "+VALUE = 42" in str(result.content)
    assert fake_runtime.files["new_module.py"] == "VALUE = 42\n"


@pytest.mark.asyncio
async def test_write_file_ensures_parent_directory(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "write_file",
        {"path": "src/new_module.py", "content": "VALUE = 42\n"},
        _context(),
    )

    assert result.ok
    assert result.metadata["parent_directory"] == "src"
    assert result.metadata["parent_directory_ensured"] is True
    assert fake_runtime.requests[-2:] == [
        ("ensure_dir", {"endpoint_id": "endpoint_1", "path": "src"}),
        ("write_text", {"endpoint_id": "endpoint_1", "path": "src/new_module.py"}),
    ]
    assert fake_runtime.files["new_module.py"] == "VALUE = 42\n"


@pytest.mark.asyncio
async def test_write_file_ensures_remote_parent_directory(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    result = await registry.execute(
        "write_file",
        {"path": "src/new_module.py", "content": "VALUE = 42\n"},
        context,
    )

    assert result.ok
    assert result.metadata["parent_directory"] == "src"
    assert fake_runtime.requests[-2:] == [
        ("ensure_dir", {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src"}),
        ("write_text", {"endpoint_id": "endpoint_ssh", "path": "/srv/app/src/new_module.py"}),
    ]


@pytest.mark.asyncio
async def test_write_file_preflight_approval_shows_diff_before_write(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(approved=True)
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        context,
    )

    assert result.ok
    assert result.metadata["preview_approved"] is True
    assert len(approval.requests) == 1
    request = approval.requests[0]
    assert request.tool_name == "write_file"
    assert request.preview_kind == "diff"
    assert request.preview_title == "Diff preview for calculator.py"
    assert "-def add(a: int, b: int) -> int:" in str(request.preview_body)
    assert "+def add(a, b):" in str(request.preview_body)
    assert [name for name, _ in fake_runtime.requests] == [
        "read_text",
        "read_text",
        "write_text",
    ]


@pytest.mark.asyncio
async def test_write_file_preflight_denial_prevents_write(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(approved=False)
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert approval.requests[0].preview_kind == "diff"
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_write_file_can_disallow_unguarded_write(fake_runtime) -> None:
    registry = ToolRegistry(config=AgentConfig(allow_unguarded_write=False))
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["unguarded_write"] is True
    assert result.metadata["recommended_action"] == 'file action="read"'
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_edit_file_replaces_exact_single_context(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    before = hashlib.sha256(
        b"def add(a: int, b: int) -> int:\n    return a - b\n"
    ).hexdigest()

    result = await registry.execute(
        "edit_file",
        {
            "path": "calculator.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
            "expected_sha256": before,
        },
        _context(),
    )

    assert result.ok
    assert result.metadata["hash_guarded"] is True
    assert result.metadata["expected_source"] == "argument"
    assert result.metadata["diff"]["changed"] is True
    assert "-    return a - b" in str(result.content)
    assert "+    return a + b" in str(result.content)
    assert "return a + b" in fake_runtime.files["calculator.py"]


@pytest.mark.asyncio
async def test_edit_file_rejects_ambiguous_context(fake_runtime) -> None:
    fake_runtime.files["repeat.py"] = "VALUE = 1\nVALUE = 1\n"
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "edit_file",
        {"path": "repeat.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "EDIT_CONTEXT_AMBIGUOUS"
    assert result.metadata["match_count"] == 2
    assert fake_runtime.files["repeat.py"] == "VALUE = 1\nVALUE = 1\n"


@pytest.mark.asyncio
async def test_edit_file_replace_all_updates_all_matches(fake_runtime) -> None:
    fake_runtime.files["repeat.py"] = "VALUE = 1\nVALUE = 1\n"
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "edit_file",
        {
            "path": "repeat.py",
            "old_text": "VALUE = 1",
            "new_text": "VALUE = 2",
            "replace_all": True,
        },
        _context(),
    )

    assert result.ok
    assert result.metadata["replace_all"] is True
    assert fake_runtime.files["repeat.py"] == "VALUE = 2\nVALUE = 2\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_missing_context(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)

    result = await registry.execute(
        "edit_file",
        {"path": "calculator.py", "old_text": "return a * b", "new_text": "return a + b"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "EDIT_CONTEXT_NOT_FOUND"
    assert result.metadata["recommended_action"] == 'file action="read"'
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_edit_file_rejects_stale_recent_read_snapshot(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    assert read.ok
    fake_runtime.files["calculator.py"] = (
        "def add(a: int, b: int) -> int:\n    return a - b\n# changed elsewhere\n"
    )
    result = await registry.execute(
        "edit_file",
        {"path": "calculator.py", "old_text": "return a - b", "new_text": "return a + b"},
        context,
    )

    assert not result.ok
    assert result.error_code == "FILE_CHANGED"
    assert result.metadata["expected_source"] == "recent_read_snapshot"
    assert result.metadata["expected_sha256"] == read.metadata["sha256"]
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_open_terminal_can_target_local_in_ssh_runtime(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        local_os="windows",
        local_shell="powershell",
        remote_os="linux",
        remote_shell="bash",
    )

    opened = await registry.execute("open_terminal", {"target": "local"}, context)

    assert opened.ok
    assert opened.metadata["target"] == "local"
    assert opened.metadata["target_os"] == "windows"
    assert opened.metadata["target_shell"] == "powershell"
    assert context.active_session_target == "local"
    assert context.active_session_os == "windows"
    assert fake_runtime.requests[-1] == (
        "open_terminal",
        {
            "environment_id": "env_1",
            "target_id": "target_1",
            "argv": ["powershell.exe", "-NoLogo"],
            "cwd": str(Path("/local/project").resolve()),
            "cols": 120,
            "rows": 30,
        },
    )


@pytest.mark.asyncio
async def test_command_facade_can_target_local_in_ssh_runtime(fake_runtime) -> None:
    registry = _facade_registry(fake_runtime)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        local_os="windows",
        local_shell="powershell",
        remote_os="linux",
        remote_shell="bash",
    )

    result = await registry.execute(
        "command",
        {"action": "run", "target": "local", "argv": ["python", "--version"]},
        context,
    )

    assert result.ok
    assert result.metadata["target"] == "local"
    assert result.metadata["inner_tool"] == "run_command"
    assert fake_runtime.requests[-2] == (
        "run_command",
        {
            "environment_id": "env_1",
            "target_id": "target_1",
            "argv": ["python", "--version"],
            "cwd": str(Path("/local/project").resolve()),
        },
    )
    assert fake_runtime.requests[-1] == (
        "observe_task",
        {
            "task_id": "task_1",
            "cursor": 0,
            "max_chars": 12000,
            "wait_seconds": 10.0,
        },
    )


@pytest.mark.asyncio
async def test_list_terminal_sessions_uses_runtime_session_registry(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute("list_terminal_sessions", {}, context)

    assert result.ok
    assert result.state == "ACTIVE"
    assert result.metadata["active_count"] == 1
    assert result.metadata["sessions"][0]["session_id"] == "session_1"
    assert result.metadata["sessions"][0]["backend"] == "ssh_tmux"
    assert "tmux ls" in result.metadata["note"]


@pytest.mark.asyncio
async def test_list_terminal_sessions_defaults_to_recent_ten(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    base = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    for index in range(12):
        fake_runtime.sessions[f"session_{index}"] = {
            "session_id": f"session_{index}",
            "state": "ACTIVE" if index % 2 == 0 else "TERMINATED",
            "backend": "local_pty",
            "environment_id": "env_1",
            "target_id": "target_1",
            "command": ["bash", "-l"],
            "default_cwd": "/project",
            "interaction_state": "AUTOMATION_CONTROLLED",
            "backend_ref": {"backend": "local_pty", "endpoint_id": "endpoint_1"},
            "created_at": (base + timedelta(minutes=index)).isoformat(),
            "updated_at": (base + timedelta(minutes=index)).isoformat(),
        }

    result = await registry.execute("list_terminal_sessions", {}, context)

    assert result.ok
    assert result.metadata["session_count"] == 10
    assert result.metadata["sessions"][0]["session_id"] == "session_11"
    assert result.metadata["sessions"][-1]["session_id"] == "session_2"
    assert {session["state"] for session in result.metadata["sessions"]} == {
        "ACTIVE",
        "TERMINATED",
    }


@pytest.mark.asyncio
async def test_list_terminal_sessions_filters_state_and_date(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    base = datetime(2026, 8, 10, 23, 0, tzinfo=UTC)
    for index, state in enumerate(["ACTIVE", "TERMINATED", "ACTIVE"]):
        fake_runtime.sessions[f"session_{index}"] = {
            "session_id": f"session_{index}",
            "state": state,
            "backend": "local_pty",
            "environment_id": "env_1",
            "target_id": "target_1",
            "command": ["bash", "-l"],
            "default_cwd": "/project",
            "interaction_state": "AUTOMATION_CONTROLLED",
            "backend_ref": {"backend": "local_pty", "endpoint_id": "endpoint_1"},
            "created_at": (base + timedelta(hours=index)).isoformat(),
            "updated_at": (base + timedelta(hours=index)).isoformat(),
        }

    result = await registry.execute(
        "list_terminal_sessions",
        {"state_filter": "active", "created_after": "2026-08-11"},
        context,
    )

    assert result.ok
    assert [session["session_id"] for session in result.metadata["sessions"]] == ["session_2"]


@pytest.mark.asyncio
async def test_list_terminal_sessions_conversation_scope_includes_brief(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    opened = await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    assert opened.ok
    ran = await registry.execute("terminal_command", {"data": "id -u"}, context)
    assert ran.ok
    fake_runtime.sessions["session_other"] = {
        "session_id": "session_other",
        "state": "ACTIVE",
        "backend": "local_pty",
        "environment_id": "env_1",
        "target_id": "target_1",
        "command": ["bash", "-l"],
        "default_cwd": "/project",
        "interaction_state": "AUTOMATION_CONTROLLED",
        "backend_ref": {"backend": "local_pty", "endpoint_id": "endpoint_1"},
        "created_at": "2026-08-11T09:00:00+00:00",
        "updated_at": "2026-08-11T09:00:00+00:00",
    }

    result = await registry.execute(
        "list_terminal_sessions",
        {"scope": "conversation", "state_filter": "all"},
        context,
    )

    assert result.ok
    assert result.metadata["session_count"] == 1
    session = result.metadata["sessions"][0]
    assert session["session_id"] == "session_1"
    assert session["last_command"] == "id -u"
    assert "Command/input sent" in str(session["brief"])


@pytest.mark.asyncio
async def test_terminal_command_accepts_model_brief_and_keeps_last_command(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    opened = await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    assert opened.ok
    ran = await registry.execute(
        "terminal_command",
        {
            "data": "sudo -i",
            "brief": "terminal is entering a privileged shell flow and waiting for output",
        },
        context,
    )

    assert ran.ok
    session = context.session_brief("session_1")
    assert session is not None
    assert session.brief == "terminal is entering a privileged shell flow and waiting for output"
    assert session.last_command == "sudo -i"


@pytest.mark.asyncio
async def test_inspect_disconnected_ssh_pty_reports_history_only(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )
    fake_runtime.sessions["session_old"] = {
        "session_id": "session_old",
        "state": "DISCONNECTED",
        "backend": "ssh_pty",
        "environment_id": "env_ssh",
        "target_id": "target_ssh",
        "command": ["bash", "-l"],
        "default_cwd": "/srv/app",
        "interaction_state": "NONE",
        "backend_ref": {"backend": "ssh_pty", "endpoint_id": "endpoint_ssh"},
    }

    inspected = await registry.execute(
        "inspect_terminal_session",
        {"session_ref": "session:session_old"},
        context,
    )
    activated = await registry.execute(
        "activate_terminal_session",
        {"session_ref": "session:session_old"},
        context,
    )
    ran = await registry.execute(
        "terminal_command",
        {"session_ref": "session:session_old", "data": "id"},
        context,
    )

    assert inspected.ok
    assert inspected.state == "DISCONNECTED"
    assert inspected.metadata["history_only"] is True
    assert "historical output may be readable" in inspected.summary
    assert "TERMINAL_READY" in str(inspected.content)
    assert not activated.ok
    assert activated.state == "DISCONNECTED"
    assert not ran.ok
    assert ran.metadata["history_only"] is True
    assert "write_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_observe_terminal_waits_past_plain_command_echo(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    calls = 0

    def observe_terminal(
        session_id: str,
        after_seq: int | None,
        limit_chars: int,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if after_seq is None:
            return {
                "session_id": session_id,
                "frames": [{"seq": 1, "data": "measure-cpu\n"}],
                "text": "measure-cpu\n",
                "cursor": 1,
            }
        if calls < 3:
            return {"session_id": session_id, "frames": [], "text": "", "cursor": after_seq}
        return {
            "session_id": session_id,
            "frames": [{"seq": 2, "data": "CPU=42\n"}],
            "text": "CPU=42\n",
            "cursor": 2,
        }

    fake_runtime.observe_terminal = observe_terminal

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    await registry.execute("terminal_command", {"data": "measure-cpu\n"}, context)
    observed = await registry.execute(
        "observe_terminal",
        {"wait_seconds": 0.4, "idle_seconds": 0.1},
        context,
    )

    assert observed.ok
    assert "measure-cpu" in str(observed.content)
    assert "CPU=42" in str(observed.content)
    assert observed.cursor == 2
    assert calls >= 3


@pytest.mark.asyncio
async def test_terminal_command_treats_empty_data_as_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute("terminal_command", {"data": ""}, context)

    assert result.ok
    assert result.summary.endswith("<ENTER>")
    assert result.metadata["normalized_empty_to_enter"] is True
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "\n"},
    )


@pytest.mark.asyncio
async def test_terminal_command_appends_enter_by_default(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "terminal_command",
        {"data": "id"},
        context,
    )

    assert result.ok
    assert result.metadata["input_only"] is False
    assert result.metadata["appended_enter"] is True
    assert result.metadata["display"] == "id<ENTER>"
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "id\n"},
    )


def test_terminal_command_tool_schema_has_only_input_only_submit_control(fake_runtime) -> None:
    definitions = {tool.definition.name: tool.definition for tool in build_runtime_tools(fake_runtime)}
    schema = definitions["terminal_command"].input_schema
    properties = schema["properties"]

    assert "run_directly" not in properties
    assert "input_only" in properties


@pytest.mark.asyncio
async def test_terminal_command_keeps_existing_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "terminal_command",
        {"data": "id\n"},
        context,
    )

    assert result.ok
    assert result.metadata["appended_enter"] is False
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "id\n"},
    )


@pytest.mark.asyncio
async def test_terminal_command_input_only_does_not_append_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "terminal_command",
        {"data": "partial", "input_only": True},
        context,
    )

    assert result.ok
    assert result.metadata["input_only"] is True
    assert result.metadata["appended_enter"] is False
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "partial"},
    )


@pytest.mark.asyncio
async def test_send_terminal_control_sends_real_ctrl_c_byte(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute("send_terminal_control", {"control": "ctrl_c"}, context)

    assert result.ok
    assert result.summary == "sent ctrl_c to session:session_1"
    assert result.metadata["display"] == "<CTRL+C>"
    assert result.metadata["terminal_input_pending"] is True
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "\x03"},
    )


@pytest.mark.asyncio
async def test_run_command_warns_when_root_session_is_active(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    await registry.execute(
        "terminal_command",
        {"data": "sudo -i"},
        context,
    )
    result = await registry.execute(
        "run_command",
        {"argv": ["apt-get", "install", "-y", "tmux"], "cwd": "."},
        context,
    )

    assert not result.ok
    assert result.recoverable
    assert result.metadata["recommended_tool"] == "terminal"
    assert result.metadata["recommended_action"] == "command"
    assert result.metadata["session_privilege"] == "root"
    assert "will not inherit that root shell" in result.summary
    assert "run_command" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_run_command_force_clean_overrides_active_session_guard(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    context.active_session_id = "session_1"
    context.mark_session_state(
        kind="pty",
        privilege="root",
        stateful=True,
        reason="root shell opened in terminal session",
    )

    result = await registry.execute(
        "run_command",
        {"argv": ["python", "-m", "pytest", "-q"], "cwd": ".", "force_clean": True},
        context,
    )

    assert result.ok
    assert result.resource_ref == "task:task_1"
    assert fake_runtime.requests[-2][0] == "run_command"
    assert fake_runtime.requests[-1][0] == "observe_task"


@pytest.mark.asyncio
async def test_run_command_timeout_keeps_task_observable(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()
    fake_runtime.task_state = "RUNNING"
    fake_runtime.task_exit_code = None
    fake_runtime.logs = [{"chunk": "server booting\n"}]

    result = await registry.execute(
        "run_command",
        {
            "argv": ["python", "-m", "http.server", "8000"],
            "cwd": ".",
            "timeout_seconds": 1,
            "max_output_chars": 200,
        },
        context,
    )
    assert context.active_task_id == "task_1"
    fake_runtime.task_state = "SUCCEEDED"
    fake_runtime.task_exit_code = 0
    observed = await registry.execute("observe_task", {"task_ref": "task:task_1"}, context)

    assert result.ok
    assert result.recoverable
    assert result.state == "RUNNING"
    assert result.metadata["timed_out"] is True
    assert result.metadata["observe_timeout_seconds"] == 1
    assert result.content == "server booting\n"
    assert result.metadata["task_id"] == "task_1"
    assert result.metadata["pid"] == 12345
    assert result.metadata["exit_code"] is None
    assert result.metadata["log_tail"] == "server booting\n"
    assert observed.ok
    assert observed.state == "SUCCEEDED"
    assert context.active_task_id is None


@pytest.mark.asyncio
async def test_run_command_denies_package_download_by_default(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    result = await registry.execute(
        "run_command",
        {"argv": ["apt-get", "download", "tmux"], "cwd": "."},
        context,
    )

    assert not result.ok
    assert result.error_code == "SANDBOX_DENIED"
    assert "package installation" in result.summary
    assert "run_command" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_terminal_command_uses_active_terminal_state(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    await registry.execute(
        "terminal_command",
        {"data": "sudo -i"},
        context,
    )
    result = await registry.execute(
        "terminal_command",
        {"data": "apt-get install -y tmux"},
        context,
    )

    assert result.ok
    assert result.resource_ref == "session:session_1"
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "apt-get install -y tmux\n"},
    )


def test_run_in_session_and_run_terminal_command_are_not_registered(fake_runtime) -> None:
    names = {tool.definition.name for tool in build_runtime_tools(fake_runtime)}

    assert "run_in_session" not in names
    assert "run_terminal_command" not in names
    assert "terminal_command" in names


@pytest.mark.asyncio
async def test_terminal_command_submits_command_to_active_session(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "terminal_command",
        {"data": "id"},
        context,
    )

    assert result.ok
    assert result.resource_ref == "session:session_1"
    assert result.metadata["display"] == "id<ENTER>"
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "id\n"},
    )


@pytest.mark.asyncio
async def test_open_terminal_rejects_nested_ssh_in_ssh_runtime(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    result = await registry.execute(
        "open_terminal",
        {"argv": ["ssh", "-t", "runtime@remote", "bash"]},
        context,
    )

    assert not result.ok
    assert result.recoverable
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert result.metadata["recommended_arguments"] == {"argv": ["bash", "-l"], "cwd": "."}


@pytest.mark.asyncio
async def test_ensure_remote_tool_reports_missing_and_can_install(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    missing = await registry.execute("ensure_remote_tool", {"tool": "tmux"}, context)
    assert not missing.ok
    assert missing.recoverable
    assert missing.metadata["recommended_action"] == (
        'remote action="ensure_tool" install=true or enable ssh_pty fallback'
    )
    assert "ENVRT_TOOL_MISSING tmux" in str(missing.content)
    assert context.remote_tool_statuses["tmux"].status == "missing"

    installed = await registry.execute(
        "ensure_remote_tool",
        {"tool": "tmux", "install": True},
        context,
    )

    assert installed.ok
    assert installed.metadata["installed"] is True
    assert "ENVRT_TOOL_INSTALLED tmux" in str(installed.content)
    assert context.remote_tool_statuses["tmux"].status == "present"
    assert context.remote_tool_statuses["tmux"].version == "tmux 3.4"


@pytest.mark.asyncio
async def test_ensure_remote_tool_manual_tmux_install_prompts_for_password(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(secret="sudo-secret")
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        approval_handler=approval,
    )
    fake_runtime.tmux_install_requires_password = True

    installed = await registry.execute(
        "ensure_remote_tool",
        {"tool": "tmux", "install": True},
        context,
    )

    assert installed.ok
    assert installed.metadata["phase"] == "manual_elevation"
    assert installed.metadata["manual_elevation"] is True
    assert installed.metadata["approved_by_user"] is True
    assert installed.metadata["password_requested"] is True
    assert installed.metadata["password_prompted"] is True
    assert installed.metadata["sudo_password_accepted"] is True
    assert installed.metadata["sudo_password_rejected"] is False
    assert installed.metadata["password_attempts"] == 1
    assert approval.requests
    assert approval.requests[0].tool_name == "remote"
    assert approval.requests[0].arguments["action"] == "ensure_tool"
    assert approval.secret_prompts == ["Remote sudo password for installing tmux"]
    assert "sudo-secret" not in str(installed.content)
    assert "close_terminal" in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_ensure_remote_tool_manual_tmux_install_retries_rejected_password(
    fake_runtime,
) -> None:
    approval = FakeInteractiveApprovalHandler(secret="sudo-secret")
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        approval_handler=approval,
    )
    fake_runtime.tmux_install_requires_password = True
    fake_runtime.tmux_manual_password_failures = 1

    installed = await registry.execute(
        "ensure_remote_tool",
        {"tool": "tmux", "install": True},
        context,
    )

    assert installed.ok
    assert installed.metadata["sudo_password_accepted"] is True
    assert installed.metadata["sudo_password_rejected"] is True
    assert installed.metadata["password_attempts"] == 2
    assert approval.secret_prompts == [
        "Remote sudo password for installing tmux",
        "Remote sudo password was rejected; try again",
    ]


@pytest.mark.asyncio
async def test_ensure_remote_tool_manual_tmux_install_can_be_denied(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(approved=False)
    registry = ToolRegistry(approval_handler=approval)
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        approval_handler=approval,
    )
    fake_runtime.tmux_install_requires_password = True

    installed = await registry.execute(
        "ensure_remote_tool",
        {"tool": "tmux", "install": True},
        context,
    )

    assert not installed.ok
    assert installed.metadata["phase"] == "manual_elevation"
    assert installed.metadata["approved_by_user"] is False
    assert "open_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_sync_status_reports_manifest_diff_summary(fake_runtime, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute("sync_status", {"max_paths": 10}, context)

    assert result.ok
    assert result.state == "LOCAL_AHEAD"
    assert result.metadata["sync_state"] == "LOCAL_AHEAD"
    assert result.metadata["retryable"] is True
    assert result.metadata["recommended_action"] == 'sync action="push"'
    assert result.metadata["diff"]["upload_count"] == 1
    assert result.metadata["diff"]["skipped_count"] == 1
    assert result.metadata["diff"]["uploads"] == ["src/app.py"]
    assert "uploads=1" in str(result.content)
    assert "src/app.py" in str(result.content)
    assert ".env (ignored" in str(result.content)
    assert fake_runtime.requests == []


@pytest.mark.asyncio
async def test_sync_push_uploads_remote_mirror_and_updates_state(
    fake_runtime,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        sync_config=SyncConfig(enabled=True),
    )

    pushed = await registry.execute("sync_push", {"max_paths": 10}, context)
    status = await registry.execute("sync_status", {"max_paths": 10}, context)

    assert pushed.ok
    assert pushed.state == "IN_SYNC"
    assert pushed.metadata["sync_state"] == "IN_SYNC"
    assert pushed.metadata["diff"]["upload_count"] == 1
    assert pushed.metadata["uploaded"] == ["src/app.py"]
    assert pushed.metadata["local_state_path"]
    assert pushed.metadata["remote_manifest_path"] == "/srv/app/.mini-harness/sync-manifest.json"
    write_paths = [
        payload["path"] for name, payload in fake_runtime.requests if name == "write_text"
    ]
    assert "/srv/app/src/app.py" in write_paths
    assert "/srv/app/.mini-harness/sync-manifest.json" in write_paths
    assert status.ok
    assert status.state == "IN_SYNC"
    assert status.metadata["diff"]["upload_count"] == 0
    assert status.metadata["diff"]["unchanged_count"] == 1


@pytest.mark.asyncio
async def test_file_write_sync_writes_local_and_pushes_remote_mirror(
    fake_runtime,
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute(
        "write_file",
        {
            "target": "sync",
            "path": "src/app.py",
            "content": "print('synced')\n",
        },
        context,
    )

    assert result.ok
    assert result.state == "IN_SYNC"
    assert result.metadata["target"] == "sync"
    assert result.metadata["local"]["ok"] is True
    assert result.metadata["remote"]["ok"] is True
    assert result.metadata["sync"]["ok"] is True
    assert result.metadata["sync"]["state"] == "IN_SYNC"
    assert result.metadata["sync"]["diff"]["upload_count"] == 1
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('synced')\n"
    write_paths = [
        payload["path"] for name, payload in fake_runtime.requests if name == "write_text"
    ]
    assert "/srv/app/src/app.py" in write_paths
    assert "/srv/app/.mini-harness/sync-manifest.json" in write_paths


@pytest.mark.asyncio
async def test_file_edit_sync_edits_local_and_pushes_remote_mirror(
    fake_runtime,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('old')\n", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute(
        "edit_file",
        {
            "target": "sync",
            "path": "src/app.py",
            "old_text": "old",
            "new_text": "new",
        },
        context,
    )

    assert result.ok
    assert result.state == "IN_SYNC"
    assert result.metadata["operation"] == "edit"
    assert result.metadata["sync"]["state"] == "IN_SYNC"
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('new')\n"
    write_paths = [
        payload["path"] for name, payload in fake_runtime.requests if name == "write_text"
    ]
    assert "/srv/app/src/app.py" in write_paths


@pytest.mark.asyncio
async def test_file_write_sync_reports_non_retryable_skip_failure(
    fake_runtime,
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_endpoint_id="endpoint_1",
        local_environment_id="env_1",
        local_target_id="target_1",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute(
        "write_file",
        {
            "target": "sync",
            "path": "node_modules/pkg/generated.js",
            "content": "module.exports = 1;\n",
        },
        context,
    )

    assert not result.ok
    assert result.state == "LOCAL_AHEAD"
    assert result.recoverable is False
    assert result.metadata["local"]["ok"] is True
    assert result.metadata["remote"]["ok"] is False
    assert result.metadata["sync"]["skipped_reason"] == "ignored"
    assert result.metadata["sync"]["retryable"] is False
    assert result.metadata["sync"]["failure"]["phase"] == "manifest_scan"
    assert (
        tmp_path / "node_modules" / "pkg" / "generated.js"
    ).read_text(encoding="utf-8") == "module.exports = 1;\n"


@pytest.mark.asyncio
async def test_open_terminal_surfaces_tmux_fallback_action(fake_runtime) -> None:
    fake_runtime.terminal_fallback = True
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/local/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
    )

    opened = await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)

    assert opened.ok
    assert "after ssh_tmux failed" in opened.summary
    assert opened.metadata["backend"] == "ssh_pty"
    assert opened.metadata["fallback_from"] == "ssh_tmux"
    assert opened.metadata["fallback_error"] == "tmux: command not found"
    assert opened.metadata["recommended_action"] == (
        'use remote action="ensure_tool" with tool=tmux and install=true'
    )


@pytest.mark.asyncio
async def test_registry_handles_unknown_tool(fake_runtime) -> None:
    result = await ToolRegistry().execute("missing", {}, _context())

    assert not result.ok
    assert result.error_code == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_controller_configures_ssh_runtime_before_loop(fake_runtime) -> None:
    sink = InMemoryEventSink()
    project_root = str(Path("/local/project").resolve())
    controller = AgentController(
        fake_runtime,
        FakeModelProvider([FinalDecision(type="final", summary="ok")]),
        event_sink=sink,
        runtime_config=RuntimeConfig(
            mode="ssh",
            name="remote-test",
            ssh=SSHRuntimeConfig(
                hostname="example.test",
                username="envrt",
                known_hosts_file="known_hosts",
                identity_file="id_ed25519",
                use_ssh_agent=False,
                remote_root="/srv/app",
            ),
        ),
    )

    result = await controller.run("say hello", "/local/project")

    assert result.final_state == AgentState.COMPLETED
    assert fake_runtime.requests[:2] == [
        ("ensure_local", {"name": "mini-harness-local", "root": project_root}),
        (
            "ensure_ssh",
            {
                "name": "remote-test",
                "hostname": "example.test",
                "auth_method": "auto",
                "has_password_secret_ref": False,
            },
        ),
    ]
    assert fake_runtime.requests[2:3] == [
        ("ensure_dir", {"endpoint_id": "endpoint_ssh", "path": "/srv/app"}),
    ]
    assert ("endpoint_health", {"endpoint_id": "endpoint_ssh"}) in fake_runtime.requests
    assert not any(
        name == "observe_task" and payload.get("max_chars") == 4000
        for name, payload in fake_runtime.requests
    )


@pytest.mark.asyncio
async def test_controller_prompts_for_ssh_password_secret(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(secret="runtime-password")
    controller = AgentController(
        fake_runtime,
        FakeModelProvider([FinalDecision(type="final", summary="ok")]),
        runtime_config=RuntimeConfig(
            mode="ssh",
            name="remote-test",
            ssh=SSHRuntimeConfig(
                hostname="example.test",
                username="envrt",
                known_hosts_file="known_hosts",
                auth_method="password",
                use_ssh_agent=False,
                remote_root="/srv/app",
            ),
        ),
        approval_handler=approval,
    )

    result = await controller.run("say hello", "/local/project")

    assert result.final_state == AgentState.COMPLETED
    assert approval.secret_prompts == ["SSH password for envrt@example.test"]
    assert ("put_secret", {"purpose": "ssh-password", "has_value": True}) in fake_runtime.requests
    assert (
        "ensure_ssh",
        {
            "name": "remote-test",
            "hostname": "example.test",
            "auth_method": "password",
            "has_password_secret_ref": True,
        },
    ) in fake_runtime.requests
    assert ("endpoint_health", {"endpoint_id": "endpoint_ssh"}) in fake_runtime.requests


@pytest.mark.asyncio
async def test_controller_can_approve_untrusted_ssh_host_key_once(fake_runtime) -> None:
    original_ensure_dir = fake_runtime.ensure_dir
    failures_left = 1

    def flaky_ensure_dir(endpoint_id: str, path: str) -> dict:
        nonlocal failures_left
        if path == "/srv/app" and failures_left:
            failures_left -= 1
            raise RuntimeError("ProviderError: SSH connection failed: Host key is not trusted")
        return original_ensure_dir(endpoint_id, path)

    fake_runtime.ensure_dir = flaky_ensure_dir
    approval = FakeInteractiveApprovalHandler(approved=True)
    controller = AgentController(
        fake_runtime,
        FakeModelProvider([FinalDecision(type="final", summary="ok")]),
        runtime_config=RuntimeConfig(
            mode="ssh",
            name="remote-test",
            ssh=SSHRuntimeConfig(
                hostname="example.test",
                username="envrt",
                known_hosts_file="known_hosts",
                identity_file="id_ed25519",
                use_ssh_agent=False,
                remote_root="/srv/app",
            ),
        ),
        approval_handler=approval,
    )

    result = await controller.run("say hello", "/local/project")

    assert result.final_state == AgentState.COMPLETED
    assert approval.requests[0].preview_kind == "ssh-host-key"
    assert (
        "ensure_ssh",
        {
            "name": "remote-test",
            "hostname": "example.test",
            "auth_method": "auto",
            "has_password_secret_ref": False,
            "trust_host_once": True,
        },
    ) in fake_runtime.requests


@pytest.mark.asyncio
async def test_request_ssh_connection_opens_interactive_setup(fake_runtime) -> None:
    approval = FakeInteractiveApprovalHandler(secret="runtime-password")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/local/project",
        runtime_name="remote-test",
        approval_handler=approval,
    )

    result = await registry.execute(
        "request_ssh_connection",
        {"reason": "remote dependencies are needed"},
        context,
    )

    assert result.ok
    assert context.runtime_mode == "ssh"
    assert context.endpoint_id == "endpoint_ssh"
    assert context.remote_environment.status == "ok"
    assert context.remote_environment.python3_path == "/usr/bin/python3"
    assert context.remote_tool_statuses["tmux"].status == "missing"
    assert approval.secret_prompts == [
        "ssh_connection:remote dependencies are needed:remote-test",
        "SSH password for envrt@example.test",
    ]
    assert ("put_secret", {"purpose": "ssh-password", "has_value": True}) in fake_runtime.requests
    assert (
        "ensure_ssh",
        {
            "name": "remote-test",
            "hostname": "example.test",
            "auth_method": "password",
            "has_password_secret_ref": True,
        },
    ) in fake_runtime.requests


@pytest.mark.asyncio
async def test_request_ssh_connection_can_approve_untrusted_host_key_once(fake_runtime) -> None:
    original_ensure_dir = fake_runtime.ensure_dir
    failures_left = 1

    def flaky_ensure_dir(endpoint_id: str, path: str) -> dict:
        nonlocal failures_left
        if path == "/srv/app" and failures_left:
            failures_left -= 1
            raise RuntimeError("ProviderError: SSH connection failed: Host key is not trusted")
        return original_ensure_dir(endpoint_id, path)

    fake_runtime.ensure_dir = flaky_ensure_dir
    approval = FakeInteractiveApprovalHandler(approved=True, secret="runtime-password")
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/local/project",
        runtime_name="remote-test",
        approval_handler=approval,
    )

    result = await registry.execute(
        "request_ssh_connection",
        {"reason": "remote dependencies are needed"},
        context,
    )

    assert result.ok
    assert approval.requests[0].preview_kind == "ssh-host-key"
    assert (
        "ensure_ssh",
        {
            "name": "remote-test",
            "hostname": "example.test",
            "auth_method": "password",
            "has_password_secret_ref": True,
            "trust_host_once": True,
        },
    ) in fake_runtime.requests


@pytest.mark.asyncio
async def test_controller_session_reuses_runtime_context_across_turns(fake_runtime) -> None:
    sink = InMemoryEventSink()
    project_root = str(Path("/local/project").resolve())
    controller = AgentController(
        fake_runtime,
        FakeModelProvider(
            [
                FinalDecision(type="final", summary="first done"),
                FinalDecision(type="final", summary="second done"),
            ]
        ),
        event_sink=sink,
    )
    session = controller.start_session("/local/project")

    first = await session.run_turn("first task")
    second = await session.run_turn("follow-up task")

    assert first.final_state == AgentState.COMPLETED
    assert second.final_state == AgentState.COMPLETED
    assert session.work_context.iteration == 2
    assert fake_runtime.requests == [
        ("ensure_local", {"name": "mini-harness-local", "root": project_root})
    ]
    assert session.context.user_tasks == ["first task", "follow-up task"]


@pytest.mark.asyncio
async def test_controller_applies_permissions_config(fake_runtime) -> None:
    sink = InMemoryEventSink()
    controller = AgentController(
        fake_runtime,
        FakeModelProvider(
            [
                ToolDecision(
                    type="tool",
                    tool_name="file",
                    arguments={
                        "action": "write",
                        "path": "calculator.py",
                        "content": "blocked\n",
                    },
                    reason_summary="Try to write a file.",
                ),
                FinalDecision(type="final", summary="done"),
            ]
        ),
        event_sink=sink,
        permissions_config=PermissionsConfig(deny=["file.write:*"]),
    )

    result = await controller.run("try a write", "/local/project")

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_error_count == 1
    assert "write_text" not in [name for name, _ in fake_runtime.requests]
