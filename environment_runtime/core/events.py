from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .ids import new_id


class ExposurePolicy(StrEnum):
    INTERNAL = "internal"
    USER = "user"
    SECRET = "secret"


class RuntimeEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    sequence: int = 0
    event_type: str
    resource_type: str
    resource_id: str
    environment_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict = Field(default_factory=dict)
    exposure: ExposurePolicy = ExposurePolicy.USER
