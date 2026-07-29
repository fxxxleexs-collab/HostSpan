from __future__ import annotations

import pytest

from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.loop import AgentLoop
from mini_harness.agent.state import AgentState
from mini_harness.config import AgentConfig
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
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
