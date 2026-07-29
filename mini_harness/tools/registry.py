from __future__ import annotations

import asyncio
import json
from typing import Any

from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult


class ToolRegistry:
    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                summary=f"unknown tool: {name}",
                error_code=ErrorCode.UNKNOWN_TOOL.value,
                recoverable=True,
            )
        try:
            return await asyncio.wait_for(
                tool.execute(arguments, context),
                timeout=self.config.tool_timeout_seconds,
            )
        except TimeoutError:
            return ToolResult(
                ok=False,
                summary=f"tool {name} timed out",
                error_code=ErrorCode.TOOL_TIMEOUT.value,
                recoverable=True,
            )


def tool_call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'))}"
