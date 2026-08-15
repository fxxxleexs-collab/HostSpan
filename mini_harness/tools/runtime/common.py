from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult

TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}


class RuntimeTool(AgentTool):
    input_model: type[BaseModel]

    def __init__(
        self,
        runtime: HarnessRuntimeClient,
        name: str,
        description: str,
        input_model: type[BaseModel],
    ) -> None:
        self.runtime = runtime
        self._definition = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_model.model_json_schema(),
        )
        self.input_model = input_model

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        _ = arguments, context
        return []

    async def execute(self, arguments: dict[str, Any], context: WorkContext) -> ToolResult:
        try:
            parsed = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                summary=f"tool arguments are invalid: {_validation_error_summary(errors)}",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"errors": errors},
            )
        try:
            return await self._execute(parsed, context)
        except MiniHarnessError as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=exc.code.value,
                recoverable=exc.recoverable,
                metadata=dict(exc.metadata),
            )
        except TimeoutError as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=ErrorCode.TOOL_TIMEOUT.value,
                recoverable=True,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
                recoverable=True,
            )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        raise NotImplementedError

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        _ = arguments, context, permission_requests, config
        return None


def _validation_error_summary(errors: Sequence[Mapping[str, Any]]) -> str:
    if not errors:
        return "unknown validation error"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "input"
    message = str(first.get("msg", "invalid value"))
    return f"{loc}: {message}"


__all__ = [
    "RuntimeTool",
    "TERMINAL_TASK_STATES",
    "_validation_error_summary",
]
