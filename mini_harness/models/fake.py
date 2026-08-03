from __future__ import annotations

import json
from collections import deque

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.models.schemas import AgentDecision, ModelMessage
from mini_harness.tools.schemas import ToolDefinition


class FakeModelProvider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = deque(decisions)
        self.requests: list[tuple[list[ModelMessage], list[ToolDefinition]]] = []

    async def decide(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> AgentDecision:
        self.requests.append((messages, tools))
        if not self.decisions:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "fake model has no decisions left",
                recoverable=False,
            )
        decision = self.decisions.popleft()
        if decision.raw_output is None:
            decision = decision.model_copy(
                update={
                    "raw_output": json.dumps(
                        decision.model_dump(exclude_none=True),
                        ensure_ascii=False,
                        indent=2,
                    )
                }
            )
        return decision
