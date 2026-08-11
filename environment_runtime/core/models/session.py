from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ..ids import new_id


class SessionState(StrEnum):
    CREATING = "CREATING"
    ACTIVE = "ACTIVE"
    DETACHED = "DETACHED"
    DISCONNECTED = "DISCONNECTED"
    TERMINATED = "TERMINATED"
    LOST = "LOST"


class InteractionState(StrEnum):
    NONE = "NONE"
    INPUT_SUSPECTED = "INPUT_SUSPECTED"
    INPUT_REQUESTED = "INPUT_REQUESTED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    HUMAN_CONTROLLED = "HUMAN_CONTROLLED"
    AUTOMATION_CONTROLLED = "AUTOMATION_CONTROLLED"


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id("session"))
    environment_id: str
    target_id: str
    backend: str
    backend_ref: dict = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    environment_variables: dict[str, str] = Field(default_factory=dict)
    default_workspace_id: str | None = None
    default_cwd: str | None = None
    terminal_cols: int = 120
    terminal_rows: int = 30
    term_type: str = "xterm-256color"
    state: SessionState = SessionState.CREATING
    interaction_state: InteractionState = InteractionState.NONE
    exit_code: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
