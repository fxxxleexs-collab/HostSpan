from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import (
    CancelTaskInput,
    ListFilesInput,
    ObserveTaskInput,
    ReadFileInput,
    RunCommandInput,
    ToolDefinition,
    ToolResult,
    WriteFileInput,
)

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

    async def execute(self, arguments: dict[str, Any], context: WorkContext) -> ToolResult:
        try:
            parsed = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                summary="tool arguments are invalid",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"errors": exc.errors(include_url=False)},
            )
        try:
            return await self._execute(parsed, context)
        except MiniHarnessError as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=exc.code.value,
                recoverable=exc.recoverable,
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


class ListFilesTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "list_files", "List project files through the runtime SDK.", ListFilesInput
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, ListFilesInput) else ListFilesInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        entries = await asyncio.to_thread(
            self.runtime.list_files,
            context.endpoint_id,
            path,
            data.recursive,
        )
        visible = sorted(entry for entry in entries if not context.should_ignore_entry(entry))
        truncated = len(visible) > data.max_entries
        visible = visible[: data.max_entries]
        return ToolResult(
            ok=True,
            summary=f"{len(visible)} entries listed",
            content="\n".join(visible),
            truncated=truncated,
            metadata={"entry_count": len(visible), "path": path},
        )


class ReadFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient, max_chars: int = 40_000) -> None:
        super().__init__(
            runtime, "read_file", "Read a text file through the runtime SDK.", ReadFileInput
        )
        self.max_chars = max_chars

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, ReadFileInput) else ReadFileInput.model_validate(parsed)
        path = context.normalize_path(data.path)
        if data.start_line and data.end_line and data.end_line < data.start_line:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line must be greater than or equal to start_line",
                recoverable=True,
            )
        text = await asyncio.to_thread(self.runtime.read_text, context.endpoint_id, path)
        lines = text.splitlines()
        start_index = (data.start_line - 1) if data.start_line else 0
        end_index = data.end_line if data.end_line else len(lines)
        selected = lines[start_index:end_index]
        rendered = "\n".join(
            f"{line_no} | {line}" for line_no, line in enumerate(selected, start=start_index + 1)
        )
        truncated = len(rendered) > self.max_chars
        if truncated:
            rendered = rendered[: self.max_chars] + "\n[truncated]"
        return ToolResult(
            ok=True,
            summary=f"{len(selected)} lines read from {path}",
            content=rendered,
            resource_ref=f"file:{path}",
            truncated=truncated,
            metadata={"path": path, "line_count": len(selected)},
        )


class WriteFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "write_file", "Overwrite a text file through the runtime SDK.", WriteFileInput
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, WriteFileInput) else WriteFileInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        result = await asyncio.to_thread(
            self.runtime.write_text,
            context.endpoint_id,
            path,
            data.content,
        )
        size = int(result.get("size", len(data.content.encode("utf-8"))))
        return ToolResult(
            ok=True,
            summary=f"wrote {size} bytes to {path}",
            resource_ref=f"file:{path}",
            metadata={"path": path, "size": size},
        )


class RunCommandTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "run_command", "Start a non-interactive runtime task.", RunCommandInput
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RunCommandInput)
            else RunCommandInput.model_validate(parsed)
        )
        cwd = context.runtime_cwd(data.cwd)
        task = await asyncio.to_thread(
            self.runtime.start_task,
            context.environment_id,
            context.target_id,
            data.argv,
            cwd,
            False,
        )
        task_id = str(task["task_id"])
        context.active_task_id = task_id
        context.task_log_cursor = 0
        context.last_task_state = str(task.get("state", "RUNNING"))
        context.last_command_exit_code = None
        return ToolResult(
            ok=True,
            summary=f"started task:{task_id}",
            resource_ref=f"task:{task_id}",
            state=context.last_task_state,
            metadata={"task_id": task_id, "argv": data.argv, "cwd": cwd},
        )


class ObserveTaskTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "observe_task", "Observe task status and incremental logs.", ObserveTaskInput
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, ObserveTaskInput)
            else ObserveTaskInput.model_validate(parsed)
        )
        task_id = _resolve_task_id(data.task_ref, context)
        deadline = time.monotonic() + data.wait_seconds
        task = await asyncio.to_thread(self.runtime.get_task, task_id)
        while task.get("state") not in TERMINAL_TASK_STATES and time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(deadline - time.monotonic(), 0)))
            task = await asyncio.to_thread(self.runtime.get_task, task_id)
        logs = await asyncio.to_thread(self.runtime.task_logs, task_id)
        full_text = "".join(str(item.get("chunk", "")) for item in logs)
        start = min(context.task_log_cursor, len(full_text))
        new_text = full_text[start:]
        context.task_log_cursor = len(full_text)
        truncated = len(new_text) > data.max_output_chars
        if truncated:
            new_text = new_text[-data.max_output_chars :]
        state = str(task.get("state", "UNKNOWN"))
        context.last_task_state = state
        exit_code = task.get("exit_code")
        context.last_command_exit_code = int(exit_code) if isinstance(exit_code, int) else None
        if state in TERMINAL_TASK_STATES:
            context.active_task_id = None
        return ToolResult(
            ok=True,
            summary=_task_summary(task_id, state, context.last_command_exit_code),
            content=new_text or None,
            resource_ref=f"task:{task_id}",
            state=state,
            cursor=context.task_log_cursor,
            truncated=truncated,
            recoverable=state not in TERMINAL_TASK_STATES,
            metadata={"task_id": task_id, "exit_code": context.last_command_exit_code},
        )


class CancelTaskTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(runtime, "cancel_task", "Cancel the active runtime task.", CancelTaskInput)

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, CancelTaskInput)
            else CancelTaskInput.model_validate(parsed)
        )
        task_id = _resolve_task_id(data.task_ref, context)
        task = await asyncio.to_thread(self.runtime.cancel_task, task_id)
        context.active_task_id = None
        state = str(task.get("state", "CANCELLED"))
        context.last_task_state = state
        return ToolResult(
            ok=True,
            summary=f"cancelled task:{task_id}",
            resource_ref=f"task:{task_id}",
            state=state,
            metadata={"task_id": task_id},
        )


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        ListFilesTool(runtime),
        ReadFileTool(runtime),
        WriteFileTool(runtime),
        RunCommandTool(runtime),
        ObserveTaskTool(runtime),
        CancelTaskTool(runtime),
    ]


def _resolve_task_id(task_ref: str | None, context: WorkContext) -> str:
    if task_ref:
        return task_ref.removeprefix("task:")
    if context.active_task_id:
        return context.active_task_id
    raise MiniHarnessError(
        ErrorCode.TASK_NOT_FOUND, "no active task is available", recoverable=True
    )


def _task_summary(task_id: str, state: str, exit_code: int | None) -> str:
    if exit_code is None:
        return f"task:{task_id} is {state}"
    return f"task:{task_id} is {state} with exit={exit_code}"
