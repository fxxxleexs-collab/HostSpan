from __future__ import annotations

from typing import Any, Protocol

from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolDefinition, ToolResult


class AgentTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: dict[str, Any], context: WorkContext) -> ToolResult: ...
