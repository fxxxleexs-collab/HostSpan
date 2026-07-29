from __future__ import annotations

from enum import StrEnum

from mini_harness.errors import ErrorCode, MiniHarnessError


class AgentState(StrEnum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    PLANNING = "planning"
    WAITING_FOR_MODEL = "waiting_for_model"
    TOOL_SELECTED = "tool_selected"
    EXECUTING_TOOL = "executing_tool"
    OBSERVING_TASK = "observing_task"
    PROCESSING_RESULT = "processing_result"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    AgentState.COMPLETED,
    AgentState.FAILED,
    AgentState.CANCELLED,
}


LEGAL_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.CREATED: {AgentState.INITIALIZING, AgentState.FAILED, AgentState.CANCELLED},
    AgentState.INITIALIZING: {AgentState.READY, AgentState.FAILED, AgentState.CANCELLED},
    AgentState.READY: {AgentState.PLANNING, AgentState.FAILED, AgentState.CANCELLED},
    AgentState.PLANNING: {AgentState.WAITING_FOR_MODEL, AgentState.FAILED, AgentState.CANCELLED},
    AgentState.WAITING_FOR_MODEL: {
        AgentState.TOOL_SELECTED,
        AgentState.PROCESSING_RESULT,
        AgentState.FAILED,
        AgentState.CANCELLED,
    },
    AgentState.TOOL_SELECTED: {AgentState.EXECUTING_TOOL, AgentState.FAILED, AgentState.CANCELLED},
    AgentState.EXECUTING_TOOL: {
        AgentState.OBSERVING_TASK,
        AgentState.PROCESSING_RESULT,
        AgentState.FAILED,
        AgentState.CANCELLED,
    },
    AgentState.OBSERVING_TASK: {
        AgentState.PROCESSING_RESULT,
        AgentState.FAILED,
        AgentState.CANCELLED,
    },
    AgentState.PROCESSING_RESULT: {
        AgentState.PLANNING,
        AgentState.COMPLETED,
        AgentState.FAILED,
        AgentState.CANCELLED,
    },
    AgentState.COMPLETED: set(),
    AgentState.FAILED: set(),
    AgentState.CANCELLED: set(),
}


class StateMachine:
    def __init__(self) -> None:
        self.state = AgentState.CREATED

    def transition(self, new_state: AgentState) -> tuple[AgentState, AgentState]:
        old_state = self.state
        if new_state not in LEGAL_TRANSITIONS[old_state]:
            raise MiniHarnessError(
                ErrorCode.RUNTIME_OPERATION_FAILED,
                f"illegal state transition: {old_state.value} -> {new_state.value}",
                recoverable=False,
            )
        self.state = new_state
        return old_state, new_state
