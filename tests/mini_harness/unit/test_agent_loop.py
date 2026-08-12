from __future__ import annotations

import pytest

from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.loop import AgentLoop
from mini_harness.agent.state import AgentState
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


@pytest.mark.asyncio
async def test_agent_loop_completes_with_fake_model(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="read_file",
                arguments={"path": "calculator.py"},
                reason_summary="Inspect implementation.",
            ),
            ToolDecision(
                type="tool",
                tool_name="write_file",
                arguments={
                    "path": "calculator.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
                reason_summary="Fix the bug.",
            ),
            ToolDecision(
                type="tool",
                tool_name="run_command",
                arguments={"argv": ["python", "-m", "pytest", "-q"]},
                reason_summary="Verify tests.",
            ),
            ToolDecision(
                type="tool",
                tool_name="observe_task",
                arguments={"wait_seconds": 0},
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
async def test_agent_loop_blocks_final_while_task_active(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="run_command",
                arguments={"argv": ["python", "-m", "pytest", "-q"]},
                reason_summary="Verify tests.",
            ),
            FinalDecision(type="final", summary="too early"),
            ToolDecision(
                type="tool",
                tool_name="observe_task",
                arguments={"wait_seconds": 0},
                reason_summary="Observe active task.",
            ),
            FinalDecision(type="final", summary="done"),
        ]
    )
    loop = AgentLoop(
        model, _registry(fake_runtime), config=AgentConfig(max_consecutive_tool_errors=5)
    )

    result = await loop.run("fix tests", _context())

    assert result.final_state == AgentState.COMPLETED
    assert result.tool_error_count == 1


@pytest.mark.asyncio
async def test_agent_loop_fails_on_max_iterations(fake_runtime) -> None:
    model = FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="read_file",
                arguments={"path": "calculator.py"},
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
        tool_name="read_file",
        arguments={"path": "calculator.py"},
        reason_summary="Repeat.",
    )
    model = FakeModelProvider(
        [
            repeated,
            repeated,
            repeated,
            ToolDecision(
                type="tool",
                tool_name="read_file",
                arguments={"path": "test_calculator.py"},
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
