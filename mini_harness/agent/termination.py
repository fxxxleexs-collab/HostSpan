from __future__ import annotations

from mini_harness.errors import ErrorCode
from mini_harness.models.schemas import FinalDecision
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolResult


def validate_final(
    decision: FinalDecision,
    context: WorkContext,
    user_task: str,
    *,
    block_failed_command: bool = False,
) -> ToolResult | None:
    if context.active_task_id:
        return ToolResult(
            ok=False,
            summary="A task is still active. Call observe_task before finalizing.",
            error_code=ErrorCode.TASK_STILL_RUNNING.value,
            recoverable=True,
            metadata=_final_guard_metadata(decision, "active_task", context),
        )
    if context.terminal_input_pending and context.active_session_id:
        return ToolResult(
            ok=False,
            summary="Terminal input may still be running. Call observe_terminal before finalizing.",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
            metadata=_final_guard_metadata(decision, "terminal_input_pending", context),
        )
    if block_failed_command and context.last_command_exit_code not in {None, 0}:
        return ToolResult(
            ok=False,
            summary="The last command failed. Fix the issue or rerun verification before finalizing.",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
            metadata=_final_guard_metadata(decision, "last_command_failed", context),
        )
    task_lower = user_task.lower()
    asks_tests = "test" in task_lower or "pytest" in task_lower or "测试" in user_task
    if asks_tests and context.last_command_exit_code is None:
        return ToolResult(
            ok=False,
            summary="The task asks for tests, but no verification command has completed.",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
            metadata=_final_guard_metadata(decision, "verification_missing", context),
        )
    return None


def _final_guard_metadata(
    decision: FinalDecision,
    reason: str,
    context: WorkContext,
) -> dict[str, object]:
    return {
        "guard": "final",
        "reason": reason,
        "attempted_final_summary": decision.summary,
        "attempted_final_details": decision.details,
        "active_task": context.task_ref(),
        "active_session": context.session_ref(),
        "terminal_input_pending": context.terminal_input_pending,
        "last_command_exit_code": context.last_command_exit_code,
    }
