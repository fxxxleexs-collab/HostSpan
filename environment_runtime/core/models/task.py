from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ..commands import CommandSpec
from ..ids import new_id
from .session import InteractionState


class TaskState(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    LOST = "LOST"


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    environment_id: str
    target_id: str
    session_id: str | None = None
    backend_ref: dict | None = None
    command: CommandSpec
    cwd: str | None = None
    workspace_revision: str | None = None
    persistent: bool = False
    state: TaskState = TaskState.CREATED
    interaction_state: InteractionState = InteractionState.NONE
    exit_code: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
