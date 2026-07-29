from __future__ import annotations

from typing import Protocol

from mini_harness.models.schemas import AgentDecision, ModelMessage
from mini_harness.tools.schemas import ToolDefinition


class ModelProvider(Protocol):
    async def decide(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> AgentDecision: ...
