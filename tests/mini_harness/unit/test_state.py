from __future__ import annotations

import pytest

from mini_harness.agent.state import AgentState, StateMachine
from mini_harness.errors import MiniHarnessError


def test_state_machine_accepts_core_tool_path() -> None:
    machine = StateMachine()

    for state in [
        AgentState.INITIALIZING,
        AgentState.READY,
        AgentState.PLANNING,
        AgentState.WAITING_FOR_MODEL,
        AgentState.TOOL_SELECTED,
        AgentState.EXECUTING_TOOL,
        AgentState.PROCESSING_RESULT,
        AgentState.PLANNING,
        AgentState.WAITING_FOR_MODEL,
        AgentState.PROCESSING_RESULT,
        AgentState.COMPLETED,
    ]:
        machine.transition(state)

    assert machine.state == AgentState.COMPLETED


def test_state_machine_rejects_illegal_transition() -> None:
    machine = StateMachine()

    with pytest.raises(MiniHarnessError):
        machine.transition(AgentState.EXECUTING_TOOL)


def test_state_machine_allows_task_observation_path() -> None:
    machine = StateMachine()
    for state in [
        AgentState.INITIALIZING,
        AgentState.READY,
        AgentState.PLANNING,
        AgentState.WAITING_FOR_MODEL,
        AgentState.TOOL_SELECTED,
        AgentState.EXECUTING_TOOL,
        AgentState.OBSERVING_TASK,
        AgentState.PROCESSING_RESULT,
    ]:
        machine.transition(state)

    assert machine.state == AgentState.PROCESSING_RESULT
