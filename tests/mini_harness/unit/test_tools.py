from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.agent.controller import AgentController
from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.state import AgentState
from mini_harness.config import RuntimeConfig, SSHRuntimeConfig
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.permissions import PermissionsConfig
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.config import SyncConfig
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry
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
    observed = await registry.execute("observe_task", {"wait_seconds": 0}, context)

    assert listed.ok
    assert "calculator.py" in str(listed.content)
    assert read.ok
    assert "return a - b" in str(read.content)
    assert write.ok
    assert run.resource_ref == "task:task_1"
    assert observed.ok
    assert observed.cursor == len(".\n1 passed\n")
    assert context.active_task_id is None
    assert [name for name, _ in fake_runtime.requests] == [
        "list_files",
        "read_text",
        "write_text",
        "run_command",
        "observe_task",
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
    sent = await registry.execute("send_terminal_input", {"data": "echo ok\n"}, context)
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
    assert fake_runtime.requests[0][1]["path"] == "/srv/app"
    assert fake_runtime.requests[1][1]["path"] == "/srv/app/calculator.py"
    assert fake_runtime.requests[2][1]["cwd"] == "/srv/app"


@pytest.mark.asyncio
async def test_open_local_terminal_is_available_in_ssh_runtime(fake_runtime) -> None:
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

    opened = await registry.execute("open_local_terminal", {}, context)

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
    await registry.execute("send_terminal_input", {"data": "measure-cpu\n"}, context)
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
async def test_send_terminal_input_treats_empty_data_as_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute("send_terminal_input", {"data": ""}, context)

    assert result.ok
    assert result.summary.endswith("<ENTER>")
    assert result.metadata["normalized_empty_to_enter"] is True
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "\n"},
    )


@pytest.mark.asyncio
async def test_send_terminal_input_run_directly_appends_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "send_terminal_input",
        {"data": "id", "run_directly": True},
        context,
    )

    assert result.ok
    assert result.metadata["run_directly"] is True
    assert result.metadata["appended_enter"] is True
    assert result.metadata["display"] == "id<ENTER>"
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "id\n"},
    )


@pytest.mark.asyncio
async def test_send_terminal_input_run_directly_keeps_existing_enter(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    result = await registry.execute(
        "send_terminal_input",
        {"data": "id\n", "run_directly": True},
        context,
    )

    assert result.ok
    assert result.metadata["appended_enter"] is False
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "id\n"},
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
        "send_terminal_input",
        {"data": "sudo -i", "run_directly": True},
        context,
    )
    result = await registry.execute(
        "run_command",
        {"argv": ["apt-get", "install", "-y", "tmux"], "cwd": "."},
        context,
    )

    assert not result.ok
    assert result.recoverable
    assert result.metadata["recommended_tool"] == "run_in_session"
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
    assert fake_runtime.requests[-1][0] == "run_command"


@pytest.mark.asyncio
async def test_run_in_session_uses_active_terminal_state(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    await registry.execute("open_terminal", {"argv": ["bash", "-l"]}, context)
    await registry.execute(
        "send_terminal_input",
        {"data": "sudo -i", "run_directly": True},
        context,
    )
    result = await registry.execute(
        "run_in_session",
        {"command": "apt-get install -y tmux", "wait_seconds": 0},
        context,
    )

    assert result.ok
    assert result.resource_ref == "session:session_1"
    assert result.metadata["session_privilege"] == "root"
    assert fake_runtime.requests[-2] == (
        "write_terminal",
        {"session_id": "session_1", "data": "apt-get install -y tmux\n"},
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
    installed = await registry.execute(
        "ensure_remote_tool",
        {"tool": "tmux", "install": True},
        context,
    )

    assert not missing.ok
    assert missing.recoverable
    assert missing.metadata["recommended_action"] == "install_tmux_or_enable_ssh_pty_fallback"
    assert "ENVRT_TOOL_MISSING tmux" in str(missing.content)
    assert installed.ok
    assert installed.metadata["installed"] is True
    assert "ENVRT_TOOL_INSTALLED tmux" in str(installed.content)


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
    assert result.state == "DIRTY"
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
    assert pushed.state == "CLEAN"
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
    assert status.state == "CLEAN"
    assert status.metadata["diff"]["upload_count"] == 0
    assert status.metadata["diff"]["unchanged_count"] == 1


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
        "run ensure_remote_tool with tool=tmux and install=true"
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
        ("ensure_ssh", {"name": "remote-test", "hostname": "example.test"}),
    ]
    assert fake_runtime.requests[2:3] == [
        ("ensure_dir", {"endpoint_id": "endpoint_ssh", "path": "/srv/app"}),
    ]


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
                    tool_name="write_file",
                    arguments={"path": "calculator.py", "content": "blocked\n"},
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
