from __future__ import annotations

import pytest

from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.loop import AgentLoop
from mini_harness.agent.state import AgentState
from mini_harness.agent.termination import validate_final
from mini_harness.config import AgentConfig
from mini_harness.context.messages import AgentContext
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry
from mini_harness.tools.schemas import ToolResult


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
    )


def _registry(fake_runtime) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    return registry


def test_final_guard_blocks_untracked_active_task() -> None:
    context = _context()
    context.active_task_id = "external_task"

    guard = validate_final(FinalDecision(type="final", summary="done"), context, "start server")

    assert guard is not None
    assert guard.error_code == "TASK_STILL_RUNNING"
    assert guard.metadata["reason"] == "untracked_active_task"
    assert guard.metadata["active_task_tracked"] is False
    assert guard.metadata["managed_tasks"] == []


@pytest.mark.asyncio
async def test_agent_loop_completes_with_fake_model(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "read", "path": "calculator.py"},
                reason_summary="Inspect implementation.",
            ),
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={
                    "action": "write",
                    "path": "calculator.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
                reason_summary="Fix the bug.",
            ),
            ToolDecision(
                type="tool",
                tool_name="command",
                arguments={"action": "run", "argv": ["python", "-m", "pytest", "-q"]},
                reason_summary="Verify tests.",
            ),
            ToolDecision(
                type="tool",
                tool_name="task",
                arguments={"action": "observe", "wait_seconds": 0},
                reason_summary="Observe verification.",
            ),
            FinalDecision(type="final", summary="done"),
        ]
    )
    sink = InMemoryEventSink()
    loop = AgentLoop(model, _registry(fake_runtime), event_sink=sink)

    result = await loop.run("fix tests", _context())

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_call_count == 4
    assert [event.sequence for event in sink.events] == list(range(1, len(sink.events) + 1))


@pytest.mark.asyncio
async def test_agent_loop_allows_test_final_when_command_returns_exit_code(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="command",
                arguments={"action": "run", "argv": ["python", "-m", "pytest", "-q"]},
                reason_summary="Verify tests.",
            ),
            FinalDecision(type="final", summary="done"),
        ]
    )
    loop = AgentLoop(
        model, _registry(fake_runtime), config=AgentConfig(max_consecutive_tool_errors=5)
    )

    result = await loop.run("fix tests", _context())

    assert result.final_state == AgentState.COMPLETED
    assert result.summary == "done"
    assert result.tool_error_count == 0


@pytest.mark.asyncio
async def test_agent_loop_allows_final_with_tracked_background_task(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="task",
                arguments={
                    "action": "start",
                    "argv": ["python", "-m", "http.server", "8000"],
                    "wait_seconds": 0,
                },
                reason_summary="Start a background dev server.",
            ),
            FinalDecision(type="final", summary="server started"),
        ]
    )
    context = _context()
    loop = AgentLoop(model, _registry(fake_runtime))

    result = await loop.run("start a dev server", context)

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_error_count == 0
    assert context.active_task_id == "task_1"
    assert context.active_task_is_tracked()
    inventory = context.managed_task_inventory(active_only=True)
    assert inventory[0]["task_id"] == "task_1"
    assert inventory[0]["pid"] == 12345


@pytest.mark.asyncio
async def test_agent_loop_allows_final_after_failed_command_by_default(fake_runtime) -> None:
    model = FakeModelProvider([FinalDecision(type="final", summary="done")])
    context = _context()
    context.last_command_exit_code = 1
    loop = AgentLoop(model, _registry(fake_runtime))

    result = await loop.run("inspect failure", context)

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_error_count == 0


@pytest.mark.asyncio
async def test_agent_loop_can_block_final_after_failed_command_when_configured(
    fake_runtime,
) -> None:
    model = FakeModelProvider(
        [
            FinalDecision(type="final", summary="too early"),
            FinalDecision(type="final", summary="still too early"),
        ]
    )
    context = _context()
    context.last_command_exit_code = 1
    loop = AgentLoop(
        model,
        _registry(fake_runtime),
        config=AgentConfig(
            block_final_on_failed_command=True,
            max_consecutive_tool_errors=2,
        ),
    )

    result = await loop.run("inspect failure", context)

    assert result.final_state == AgentState.FAILED
    assert result.error_code == "CONSECUTIVE_ERROR_LIMIT"


@pytest.mark.asyncio
async def test_agent_loop_fails_after_repeated_final_guard_blocks(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            FinalDecision(type="final", summary="too early"),
            FinalDecision(type="final", summary="still too early"),
        ]
    )
    sink = InMemoryEventSink()
    loop = AgentLoop(
        model,
        _registry(fake_runtime),
        config=AgentConfig(max_iterations=30, max_consecutive_tool_errors=2),
        event_sink=sink,
    )

    result = await loop.run("fix tests", _context())

    assert result.final_state == AgentState.FAILED
    assert result.error_code == "CONSECUTIVE_ERROR_LIMIT"
    assert result.iterations == 2
    failed_events = [event for event in sink.events if event.event_type.value == "tool.failed"]
    assert failed_events[-1].payload["metadata"]["guard"] == "final"
    assert failed_events[-1].payload["metadata"]["reason"] == "verification_missing"
    assert failed_events[-1].payload["metadata"]["attempted_final_summary"] == "still too early"


@pytest.mark.asyncio
async def test_agent_loop_fails_on_max_iterations(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "read", "path": "calculator.py"},
                reason_summary="Repeat.",
            )
        ]
    )
    loop = AgentLoop(model, _registry(fake_runtime), config=AgentConfig(max_iterations=1))

    result = await loop.run("fix tests", _context())

    assert result.final_state == AgentState.FAILED
    assert result.error_code == "MAX_ITERATIONS_EXCEEDED"


@pytest.mark.asyncio
async def test_agent_loop_repeated_action_warning_uses_legal_state_path(fake_runtime) -> None:
    repeated = ToolDecision(
        type="tool",
        tool_name="file",
        arguments={"action": "read", "path": "calculator.py"},
        reason_summary="Repeat.",
    )
    model = FakeModelProvider(
        [
            repeated,
            repeated,
            repeated,
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "read", "path": "test_calculator.py"},
                reason_summary="Choose a different action.",
            ),
            FinalDecision(type="final", summary="done"),
        ]
    )
    loop = AgentLoop(
        model,
        _registry(fake_runtime),
        config=AgentConfig(max_consecutive_tool_errors=5),
    )

    result = await loop.run("inspect code", _context())

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_error_count == 1


@pytest.mark.asyncio
async def test_agent_loop_can_continue_with_existing_context(fake_runtime) -> None:
    work_context = _context()
    config = AgentConfig()
    agent_context = AgentContext(config, work_context)
    first_model = FakeModelProvider([FinalDecision(type="final", summary="first done")])
    second_model = FakeModelProvider([FinalDecision(type="final", summary="second done")])

    first = await AgentLoop(first_model, _registry(fake_runtime), config=config).run_with_context(
        "first task",
        work_context,
        agent_context,
    )
    second = await AgentLoop(second_model, _registry(fake_runtime), config=config).run_with_context(
        "follow-up task",
        work_context,
        agent_context,
    )

    assert first.final_state == AgentState.COMPLETED
    assert second.final_state == AgentState.COMPLETED
    assert work_context.iteration == 2
    request_messages = second_model.requests[0][0]
    rendered = "\n".join(message.content for message in request_messages)
    assert "first task" in rendered
    assert "first done" in rendered
    assert "follow-up task" in rendered


def test_agent_context_compacts_older_turns_and_tool_results() -> None:
    config = AgentConfig(max_context_chars=1_000, recent_tool_turns=4, max_tool_result_chars=1_000)
    context = AgentContext(config, _context())
    context.add_user_task("old task")
    context.add_final_decision(FinalDecision(type="final", summary="old answer"))
    context.add_user_task("new task")
    for index in range(8):
        context.add_tool_result(
            "read_file",
            {"path": f"file_{index}.py"},
            ToolResult(
                ok=True,
                summary=f"read file {index}",
                content="x" * 900,
                resource_ref=f"file:file_{index}.py",
            ),
        )

    messages = context.build_messages([])

    assert context.truncated
    assert context.compacted_summary is not None
    assert len(context.tool_turns) == 2
    rendered = "\n".join(message.content for message in messages)
    assert "Compacted conversation context" in rendered
    assert "old task" in rendered
    assert "old answer" in rendered
    assert "new task" in rendered


def test_agent_context_includes_remote_connection_status() -> None:
    work_context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        remote_hostname="example.test",
        remote_username="envrt",
        remote_port=2222,
        remote_auth_method="password",
        remote_os="linux",
        remote_shell="bash",
    )
    context = AgentContext(AgentConfig(), work_context)

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert "Remote connection: host=envrt@example.test:2222" in rendered
    assert "configured=true" in rendered
    assert "connected=true" in rendered
    assert "auth=password" in rendered
    assert "Remote tools: tmux=unknown reason=not probed" in rendered


def test_agent_context_includes_remote_tool_status() -> None:
    work_context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        remote_hostname="example.test",
        remote_username="envrt",
        remote_port=2222,
        remote_auth_method="password",
        remote_os="linux",
        remote_shell="bash",
    )
    work_context.record_remote_tool_status("tmux", "present", version="tmux 3.4")
    context = AgentContext(AgentConfig(), work_context)

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert "Remote tools: tmux=present version=tmux 3.4" in rendered


def test_agent_context_includes_runtime_activity_summary() -> None:
    work_context = _context()
    work_context.record_task_brief(
        "task_1",
        argv=["python", "app.py"],
        cwd="/project",
        state="RUNNING",
        pid=12345,
        persistent=True,
        log_tail=" * Running on http://127.0.0.1:5000\n",
        started_by="start_task",
    )
    work_context.record_session_interaction(
        "session_1",
        target="local",
        backend="pty",
        runtime_state="ACTIVE",
        brief="interactive shell waiting for input",
        last_command="python -i",
        pending=True,
        updated_by="observe_terminal",
    )
    context = AgentContext(AgentConfig(), work_context)

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert "Runtime activity:" in rendered
    assert "task:task_1" in rendered
    assert "pid=12345" in rendered
    assert "python app.py" in rendered
    assert "session:session_1" in rendered
    assert "interactive shell waiting for input" in rendered


def test_agent_context_exposes_safe_tool_metadata_to_model() -> None:
    context = AgentContext(AgentConfig(), _context())
    context.add_tool_result(
        "file",
        {"action": "read", "path": "calculator.py"},
        ToolResult(
            ok=True,
            summary="2 lines read from calculator.py lines 1-2",
            content="1 | def add(a, b):\n2 |     return a + b",
            metadata={
                "path": "calculator.py",
                "sha256": "abc123",
                "size": 42,
                "line_count": 2,
                "selected_line_count": 2,
                "encoding": "utf-8",
                "secret_ref": "secret_should_not_be_rendered",
            },
        ),
    )

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert '"sha256": "abc123"' in rendered
    assert '"line_count": 2' in rendered
    assert "secret_should_not_be_rendered" not in rendered


def test_agent_context_saves_long_tool_results_as_artifacts(tmp_path) -> None:
    work_context = WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root=str(tmp_path),
    )
    config = AgentConfig(max_tool_result_chars=1_000, tool_result_artifact_chars=1_000)
    context = AgentContext(config, work_context)
    long_content = "A" * 1_200 + "\nimportant middle\n" + "B" * 1_200

    context.add_tool_result(
        "command",
        {"action": "run", "argv": ["python", "-m", "pytest", "-q"]},
        ToolResult(ok=True, summary="command output", content=long_content),
    )

    stored_result = context.tool_turns[0][2]
    artifact_path = stored_result.metadata["artifact_path"]
    assert stored_result.truncated is True
    assert stored_result.content is not None
    assert len(stored_result.content) <= config.max_tool_result_chars
    assert (tmp_path / str(artifact_path)).read_text(encoding="utf-8") == long_content

    rendered = "\n".join(message.content for message in context.build_messages([]))
    assert "artifact_path" in rendered
    assert "full tool output saved" in rendered
    assert "important middle" not in rendered
    assert rendered.count("A") < 1_200
    assert rendered.count("B") < 1_200


def test_agent_context_terminal_input_prompt_uses_input_only_not_run_directly() -> None:
    context = AgentContext(AgentConfig(), _context())

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert "run_directly" not in rendered
    assert "input_only=true" in rendered
    assert "password prompts" in rendered
    assert "all data is submitted by default" in rendered


def test_agent_context_describes_task_terminal_boundaries() -> None:
    context = AgentContext(AgentConfig(), _context())

    rendered = "\n".join(message.content for message in context.build_messages([]))

    assert "Runtime tasks and terminal sessions are separate" in rendered
    assert "task_id values" in rendered
    assert "session_id values" in rendered
    assert 'use terminal action="command" in that same session' in rendered
    assert "not a task" in rendered


def test_agent_context_auto_compacts_when_turn_threshold_is_exceeded() -> None:
    config = AgentConfig(max_context_chars=100_000, auto_compact_turns=2, recent_tool_turns=4)
    context = AgentContext(config, _context())
    context.add_user_task("first task")
    context.add_final_decision(FinalDecision(type="final", summary="first answer"))
    context.add_user_task("second task")
    context.add_final_decision(FinalDecision(type="final", summary="second answer"))
    context.add_user_task("third task")

    messages = context.build_messages([])

    assert context.compacted
    assert context.last_compact_result is not None
    assert context.last_compact_result.reason == "auto:user-turns"
    assert context.user_tasks == ["third task"]
    rendered = "\n".join(message.content for message in messages)
    assert "Compacted conversation context" in rendered
    assert "first task" in rendered
    assert "second answer" in rendered
    assert "third task" in rendered


def test_agent_context_manual_compact_returns_stats() -> None:
    config = AgentConfig(max_context_chars=100_000, recent_tool_turns=4)
    context = AgentContext(config, _context())
    context.add_user_task("old task")
    context.add_final_decision(FinalDecision(type="final", summary="old answer"))
    context.add_user_task("current task")
    for index in range(5):
        context.add_tool_result(
            "read_file",
            {"path": f"file_{index}.py"},
            ToolResult(ok=True, summary=f"read file {index}", content="content"),
        )

    result = context.compact()

    assert result.compacted
    assert result.reason == "manual"
    assert result.user_turns_before == 2
    assert result.user_turns_after == 1
    assert result.tool_turns_before == 5
    assert result.tool_turns_after == 2
    assert "old task" in str(context.compacted_summary)
    assert "Context compacted" in result.summary
