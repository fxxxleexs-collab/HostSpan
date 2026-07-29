from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from mini_harness.agent.state import AgentState


class AgentEventType(StrEnum):
    AGENT_STARTED = "agent.started"
    STATE_CHANGED = "agent.state_changed"
    PLAN_UPDATED = "agent.plan_updated"
    MODEL_REQUEST_STARTED = "model.request.started"
    MODEL_REQUEST_COMPLETED = "model.request.completed"
    TOOL_SELECTED = "tool.selected"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TASK_STARTED = "task.started"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_OUTPUT = "task.output"
    TASK_COMPLETED = "task.completed"
    CONTEXT_TRUNCATED = "context.truncated"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"


class AgentEvent(BaseModel):
    sequence: int
    timestamp: datetime
    event_type: AgentEventType
    state: AgentState
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentEventSink(Protocol):
    def emit(
        self,
        event_type: AgentEventType,
        state: AgentState,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent: ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(
        self,
        event_type: AgentEventType,
        state: AgentState,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            sequence=len(self.events) + 1,
            timestamp=datetime.now(UTC),
            event_type=event_type,
            state=state,
            summary=summary,
            payload=payload or {},
        )
        self.events.append(event)
        return event
