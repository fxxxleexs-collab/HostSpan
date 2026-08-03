from __future__ import annotations

import pytest

from mini_harness.agent.controller import AgentController
from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.state import AgentState
from mini_harness.config import RuntimeConfig, SSHRuntimeConfig
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.schemas import FinalDecision
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
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
    assert "TERMINAL_READY" in str(observed.content)
    assert sent.ok
    assert closed.state == "TERMINATED"
    assert context.active_session_id is None
    assert fake_runtime.requests[0][1]["path"] == "/srv/app"
    assert fake_runtime.requests[1][1]["path"] == "/srv/app/calculator.py"
    assert fake_runtime.requests[2][1]["cwd"] == "/srv/app"


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
        ("ensure_ssh", {"name": "remote-test", "hostname": "example.test"}),
        ("ensure_dir", {"endpoint_id": "endpoint_ssh", "path": "/srv/app"}),
    ]
