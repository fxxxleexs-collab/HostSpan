from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None


class FinalDecision(BaseModel):
    type: Literal["final"]
    summary: str
    details: str | None = None


class ToolDecision(BaseModel):
    type: Literal["tool"]
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason_summary: str


AgentDecision = FinalDecision | ToolDecision
