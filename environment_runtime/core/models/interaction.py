from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ..ids import new_id


class InputType(StrEnum):
    TEXT = "TEXT"
    CONFIRMATION = "CONFIRMATION"
    SECRET = "SECRET"
    KEYSTROKE = "KEYSTROKE"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"


class InputRequestStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class InputRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: new_id("input"))
    session_id: str
    task_id: str | None = None
    input_type: InputType
    status: InputRequestStatus = InputRequestStatus.PENDING
    prompt: str | None = None
    allowed_values: list[str] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None


class WriterLease(BaseModel):
    lease_id: str = Field(default_factory=lambda: new_id("lease"))
    session_id: str
    owner_type: str
    owner_id: str
    expires_at: datetime
    version: int = 1
