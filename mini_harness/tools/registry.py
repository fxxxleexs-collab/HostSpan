from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError

from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import AllowAllPermissionPolicy, PermissionPolicy
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult


class ToolRegistry:
    def __init__(
        self,
        config: AgentConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.permission_policy = permission_policy or AllowAllPermissionPolicy()
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
            permission_requests = tool.permission_requests(arguments, context)
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                summary=f"tool arguments are invalid: {errors[0].get('msg', 'validation failed')}",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"errors": errors},
            )
        except MiniHarnessError as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=exc.code.value,
                recoverable=exc.recoverable,
            )
        permission_decision = self.permission_policy.authorize_many(permission_requests)
        if not permission_decision.allowed:
            return ToolResult(
                ok=False,
                summary=f"permission denied for tool {name}: {permission_decision.reason}",
                error_code=ErrorCode.PERMISSION_DENIED.value,
                recoverable=True,
                metadata={
                    "permission_denied": True,
                    "approval_required": permission_decision.approval_required,
                    "missing_capabilities": list(permission_decision.missing_capabilities),
                    "permission_requests": [
                        {
                            "capability": request.capability,
                            "target": request.target,
                            "operation": request.operation,
                            "resource": request.resource,
                        }
                        for request in permission_requests
                    ],
                    **permission_decision.metadata,
                },
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
