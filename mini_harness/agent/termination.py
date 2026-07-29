from __future__ import annotations

from mini_harness.errors import ErrorCode
from mini_harness.models.schemas import FinalDecision
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolResult


def validate_final(
    decision: FinalDecision, context: WorkContext, user_task: str
) -> ToolResult | None:
    _ = decision
    if context.active_task_id:
        return ToolResult(
            ok=False,
            summary="A task is still active. Call observe_task before finalizing.",
            error_code=ErrorCode.TASK_STILL_RUNNING.value,
            recoverable=True,
        )
    if context.last_command_exit_code not in {None, 0}:
        return ToolResult(
            ok=False,
            summary="The last command failed. Fix the issue or rerun verification before finalizing.",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
        )
    task_lower = user_task.lower()
    asks_tests = "test" in task_lower or "pytest" in task_lower or "测试" in user_task
    if asks_tests and context.last_command_exit_code is None:
        return ToolResult(
            ok=False,
            summary="The task asks for tests, but no verification command has completed.",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
        )
    return None
