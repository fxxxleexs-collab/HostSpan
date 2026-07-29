from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    TOOL_ARGUMENT_INVALID = "TOOL_ARGUMENT_INVALID"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    RUNTIME_OPERATION_FAILED = "RUNTIME_OPERATION_FAILED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_STILL_RUNNING = "TASK_STILL_RUNNING"
    PATH_OUTSIDE_PROJECT = "PATH_OUTSIDE_PROJECT"
    MAX_ITERATIONS_EXCEEDED = "MAX_ITERATIONS_EXCEEDED"
    REPEATED_ACTION_LIMIT = "REPEATED_ACTION_LIMIT"
    CONSECUTIVE_ERROR_LIMIT = "CONSECUTIVE_ERROR_LIMIT"


class MiniHarnessError(Exception):
    def __init__(self, code: ErrorCode, message: str, recoverable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
