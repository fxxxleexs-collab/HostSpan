from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import shlex
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig, RuntimeConfig
from mini_harness.diffing import (
    TextDiff,
    TextSnapshot,
    make_unified_diff,
    snapshot_text,
    summarize_diff,
)
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.file_ops import RuntimeWorkspaceFileOps, WorkspaceFileOps
from mini_harness.permissions import PermissionDecision, PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, TargetBinding, WorkContext
from mini_harness.sync.engine import SyncEngine, SyncPushResult
from mini_harness.sync.errors import SyncConflictError
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import (
    ActivateTerminalSessionInput,
    CancelTaskInput,
    CloseTerminalInput,
    EditFileInput,
    EnsureRemoteToolInput,
    InspectTerminalSessionInput,
    ListFilesInput,
    ListTasksInput,
    ListTerminalSessionsInput,
    ObserveTaskInput,
    ObserveTerminalInput,
    OpenTerminalInput,
    ReadFileInput,
    RequestSSHConnectionInput,
    RunCommandInput,
    RunInSessionInput,
    SendTerminalControlInput,
    SendTerminalInput,
    StartTaskInput,
    SyncPushInput,
    SyncStatusInput,
    ToolDefinition,
    ToolResult,
    WriteFileInput,
)

TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}


@dataclass(frozen=True)
class PreparedTextChange:
    path: str
    content: str
    before_snapshot: TextSnapshot
    after_snapshot: TextSnapshot
    diff: TextDiff
    diff_summary: str
    expected_sha256: str | None
    expected_source: str | None
    existed_before: bool

    @property
    def hash_guarded(self) -> bool:
        return self.expected_sha256 is not None

    @property
    def unguarded_write(self) -> bool:
        return self.expected_sha256 is None


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


class ListFilesTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "list_files", "List project files through the runtime SDK.", ListFilesInput
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ListFilesInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.list",
                target=context.default_terminal_target(),
                operation="list",
                resource=path,
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, ListFilesInput) else ListFilesInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        entries = await asyncio.to_thread(
            self.runtime.list_files,
            context.endpoint_id,
            context.runtime_path(path),
            data.recursive,
        )
        visible = sorted(
            context.display_path(entry)
            for entry in entries
            if not context.should_ignore_entry(context.display_path(entry))
        )
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

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ReadFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.read",
                target=context.default_terminal_target(),
                operation="read",
                resource=path,
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, ReadFileInput) else ReadFileInput.model_validate(parsed)
        path = context.normalize_path(data.path)
        if data.end_line is not None and data.max_lines is not None:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line and max_lines cannot be used together",
                recoverable=True,
            )
        if data.start_line and data.end_line and data.end_line < data.start_line:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line must be greater than or equal to start_line",
                recoverable=True,
            )
        file_ops = RuntimeWorkspaceFileOps(self.runtime, context)
        read = await asyncio.to_thread(file_ops.read_text, path)
        snapshot = snapshot_text(read.location.path, read.text)
        snapshot_summary = context.record_file_snapshot(snapshot)
        lines = read.text.splitlines()
        start_index = (data.start_line - 1) if data.start_line else 0
        if data.max_lines is not None:
            end_index = min(len(lines), start_index + data.max_lines)
        else:
            end_index = data.end_line if data.end_line else len(lines)
        selected = lines[start_index:end_index]
        selected_start_line = start_index + 1 if selected else None
        selected_end_line = end_index if selected else None
        has_more = end_index < len(lines)
        next_start_line = end_index + 1 if has_more else None
        rendered = "\n".join(
            f"{line_no} | {line}" for line_no, line in enumerate(selected, start=start_index + 1)
        )
        truncated = len(rendered) > self.max_chars
        if truncated:
            rendered = rendered[: self.max_chars] + "\n[truncated]"
        return ToolResult(
            ok=True,
            summary=_read_file_summary(path, len(selected), selected_start_line, selected_end_line),
            content=rendered,
            resource_ref=f"file:{path}",
            truncated=truncated,
            metadata={
                "path": path,
                "sha256": snapshot.sha256,
                "size": snapshot.size,
                "line_count": snapshot.line_count,
                "selected_line_count": len(selected),
                "start_line": selected_start_line,
                "end_line": selected_end_line,
                "requested_start_line": data.start_line,
                "requested_end_line": data.end_line,
                "requested_max_lines": data.max_lines,
                "has_more": has_more,
                "next_start_line": next_start_line,
                "newline": snapshot.newline,
                "encoding": snapshot.encoding,
                "snapshot": snapshot_summary.as_dict(),
                "file_location": read.location.as_dict(),
            },
        )


class WriteFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "write_file", "Overwrite a text file through the runtime SDK.", WriteFileInput
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = WriteFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.write",
                target=context.default_terminal_target(),
                operation="write",
                resource=path,
            )
        ]

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        data = WriteFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        prepared = await asyncio.to_thread(
            _prepare_text_change,
            file_ops=RuntimeWorkspaceFileOps(self.runtime, context),
            context=context,
            path=path,
            new_content=data.content,
            expected_sha256=data.expected_sha256,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if prepared.unguarded_write and not config.allow_unguarded_write:
            return _unguarded_write_denied_result(self.definition.name, prepared)
        return _file_change_approval_request(
            tool_name=self.definition.name,
            arguments=arguments,
            permission_requests=permission_requests,
            prepared=prepared,
            prefer_edit=prepared.existed_before,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, WriteFileInput) else WriteFileInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        prepared = await asyncio.to_thread(
            _prepare_text_change,
            file_ops=RuntimeWorkspaceFileOps(self.runtime, context),
            context=context,
            path=path,
            new_content=data.content,
            expected_sha256=data.expected_sha256,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        file_ops = RuntimeWorkspaceFileOps(self.runtime, context)
        write = await asyncio.to_thread(file_ops.write_text, path, data.content)
        written_summary = context.record_file_snapshot(prepared.after_snapshot)
        return ToolResult(
            ok=True,
            summary=_write_file_summary(
                path,
                write.size,
                prepared.diff.changed,
                prepared.diff.added_lines,
                prepared.diff.removed_lines,
            ),
            content=prepared.diff_summary,
            resource_ref=f"file:{path}",
            metadata={
                "path": path,
                "size": write.size,
                **_prepared_change_metadata(prepared),
                "parent_directory": write.parent_directory.path,
                "parent_directory_ensured": write.parent_directory.ensured,
                "parent_directory_result": write.parent_directory.as_dict(),
                "file_location": write.location.as_dict(),
                "snapshot": written_summary.as_dict(),
            },
        )


class EditFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "edit_file",
            "Edit a text file by replacing exact old_text with new_text.",
            EditFileInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = EditFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.write",
                target=context.default_terminal_target(),
                operation="edit",
                resource=path,
            )
        ]

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        data = EditFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        prepared = await asyncio.to_thread(
            _prepare_edit_change,
            file_ops=RuntimeWorkspaceFileOps(self.runtime, context),
            context=context,
            path=path,
            old_text=data.old_text,
            new_text=data.new_text,
            expected_sha256=data.expected_sha256,
            replace_all=data.replace_all,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if prepared.unguarded_write and not config.allow_unguarded_write:
            return _unguarded_write_denied_result(self.definition.name, prepared)
        return _file_change_approval_request(
            tool_name=self.definition.name,
            arguments=arguments,
            permission_requests=permission_requests,
            prepared=prepared,
            prefer_edit=False,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, EditFileInput) else EditFileInput.model_validate(parsed)
        path = context.normalize_path(data.path)
        prepared = await asyncio.to_thread(
            _prepare_edit_change,
            file_ops=RuntimeWorkspaceFileOps(self.runtime, context),
            context=context,
            path=path,
            old_text=data.old_text,
            new_text=data.new_text,
            expected_sha256=data.expected_sha256,
            replace_all=data.replace_all,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        file_ops = RuntimeWorkspaceFileOps(self.runtime, context)
        write = await asyncio.to_thread(file_ops.write_text, path, prepared.content)
        written_summary = context.record_file_snapshot(prepared.after_snapshot)
        return ToolResult(
            ok=True,
            summary=_write_file_summary(
                path,
                write.size,
                prepared.diff.changed,
                prepared.diff.added_lines,
                prepared.diff.removed_lines,
            ),
            content=prepared.diff_summary,
            resource_ref=f"file:{path}",
            metadata={
                "path": path,
                "size": write.size,
                **_prepared_change_metadata(prepared),
                "replace_all": data.replace_all,
                "parent_directory": write.parent_directory.path,
                "parent_directory_ensured": write.parent_directory.ensured,
                "parent_directory_result": write.parent_directory.as_dict(),
                "file_location": write.location.as_dict(),
                "snapshot": written_summary.as_dict(),
            },
        )


class RunCommandTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "run_command",
            (
                "Run a short clean non-interactive command as a managed task. "
                "Use for one-shot checks, builds, tests, and inspections. "
                "Do not use for dev servers, watchers, REPLs, sudo/password prompts, "
                "or commands expected to keep running; use start_task for long-running "
                "non-interactive services and terminal tools only for human interaction."
            ),
            RunCommandInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = RunCommandInput.model_validate(arguments)
        cwd = context.normalize_cwd(data.cwd)
        target = context.default_terminal_target()
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.run",
                target=target,
                operation="run",
                resource=cwd,
                argv=data.argv,
            )
        ]
        requests.extend(
            _command_write_permission_requests(
                tool_name=self.definition.name,
                target=target,
                command=" ".join(data.argv),
                resource=cwd,
            )
        )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RunCommandInput)
            else RunCommandInput.model_validate(parsed)
        )
        guard = _clean_task_session_guard(data.argv, context, data.force_clean)
        if guard is not None:
            return guard
        cwd = context.runtime_cwd(data.cwd)
        sandboxed = context.sandbox_task(data.argv, cwd)
        task = await asyncio.to_thread(
            self.runtime.run_command,
            context.environment_id,
            context.target_id,
            sandboxed.argv,
            sandboxed.cwd,
        )
        task_id = str(task["task_id"])
        context.active_task_id = task_id
        context.task_log_cursor = 0
        context.last_task_state = str(task.get("state", "RUNNING"))
        context.last_command_exit_code = None
        brief = context.record_task_brief(
            task_id,
            argv=sandboxed.argv,
            cwd=sandboxed.cwd,
            state=context.last_task_state,
            pid=_task_pid(task),
            persistent=bool(task.get("persistent", False)),
            started_by=self.definition.name,
        )
        return ToolResult(
            ok=True,
            summary=_task_started_summary("started command", task_id, _task_pid(task)),
            resource_ref=f"task:{task_id}",
            state=context.last_task_state,
            metadata={
                "task_id": task_id,
                "pid": brief.pid,
                "persistent": brief.persistent,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
                "sandbox_engine": sandboxed.engine,
            },
        )


class StartTaskTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "start_task",
            (
                "Start a managed long-running non-interactive task. Use for development "
                "servers, file watchers, background services, and commands expected to "
                "continue running while you inspect logs with observe_task. Do not use "
                "for commands that need passwords or live human interaction; use terminal "
                "tools for those."
            ),
            StartTaskInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = StartTaskInput.model_validate(arguments)
        cwd = context.normalize_cwd(data.cwd)
        target = context.default_terminal_target()
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.run",
                target=target,
                operation="start",
                resource=cwd,
                argv=data.argv,
            )
        ]
        requests.extend(
            _command_write_permission_requests(
                tool_name=self.definition.name,
                target=target,
                command=" ".join(data.argv),
                resource=cwd,
            )
        )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, StartTaskInput) else StartTaskInput.model_validate(parsed)
        cwd = context.runtime_cwd(data.cwd)
        sandboxed = context.sandbox_task(data.argv, cwd)
        task = await asyncio.to_thread(
            self.runtime.start_task,
            context.environment_id,
            context.target_id,
            sandboxed.argv,
            sandboxed.cwd,
            True,
        )
        task_id = str(task["task_id"])
        state = str(task.get("state", "RUNNING"))
        context.active_task_id = task_id
        context.task_log_cursor = 0
        context.last_task_state = state
        context.last_command_exit_code = None
        pid = _task_pid(task)
        content: str | None = None
        truncated = False
        if data.wait_seconds > 0:
            observation = await asyncio.to_thread(
                self.runtime.observe_task,
                task_id,
                0,
                data.max_output_chars,
                data.wait_seconds,
            )
            content = str(observation.get("text", "")) or None
            context.task_log_cursor = int(observation.get("cursor", 0))
            task = observation.get("task", task)
            state = str(observation.get("state") or task.get("state", state))
            pid = _task_pid(task) or pid
            truncated = bool(observation.get("truncated", False))
            exit_code = _task_exit_code(observation, task)
            context.last_command_exit_code = exit_code
        else:
            exit_code = _task_exit_code({}, task)
        context.last_task_state = state
        if state in TERMINAL_TASK_STATES:
            context.active_task_id = None
        brief = context.record_task_brief(
            task_id,
            argv=sandboxed.argv,
            cwd=sandboxed.cwd,
            state=state,
            pid=pid,
            persistent=True,
            log_tail=content,
            exit_code=exit_code,
            started_by=self.definition.name,
        )
        return ToolResult(
            ok=True,
            summary=_task_started_summary("started long-running task", task_id, pid),
            content=content,
            resource_ref=f"task:{task_id}",
            state=state,
            cursor=context.task_log_cursor,
            truncated=truncated,
            recoverable=state not in TERMINAL_TASK_STATES,
            metadata={
                "task_id": task_id,
                "pid": brief.pid,
                "persistent": True,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
                "log_tail": brief.log_tail,
                "exit_code": brief.exit_code,
                "sandbox_engine": sandboxed.engine,
            },
        )


class ObserveTaskTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "observe_task", "Observe task status and incremental logs.", ObserveTaskInput
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ObserveTaskInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.observe",
                target=context.default_terminal_target(),
                operation="observe",
                resource=data.task_ref or context.task_ref(),
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, ObserveTaskInput)
            else ObserveTaskInput.model_validate(parsed)
        )
        task_id = _resolve_task_id(data.task_ref, context)
        deadline = time.monotonic() + data.wait_seconds
        chunks: list[str] = []
        observation: dict[str, Any] | None = None
        state = "UNKNOWN"
        truncated = False
        while True:
            wait_seconds = max(0.0, deadline - time.monotonic())
            observation = await asyncio.to_thread(
                self.runtime.observe_task,
                task_id,
                context.task_log_cursor,
                data.max_output_chars,
                wait_seconds,
            )
            chunks.append(str(observation.get("text", "")))
            context.task_log_cursor = int(observation["cursor"])
            truncated = truncated or bool(observation.get("truncated", False))
            task = observation["task"]
            state = str(observation.get("state") or task.get("state", "UNKNOWN"))
            if state in TERMINAL_TASK_STATES or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        if observation is None:
            raise RuntimeError("task observation did not return a result")
        task = observation["task"]
        new_text = "".join(chunks)
        if len(new_text) > data.max_output_chars:
            new_text = new_text[-data.max_output_chars :]
            truncated = True
        context.last_task_state = state
        context.last_command_exit_code = _task_exit_code(observation, task)
        if state in TERMINAL_TASK_STATES:
            context.active_task_id = None
        pid = _task_pid(task)
        brief = context.record_task_brief(
            task_id,
            state=state,
            pid=pid,
            log_tail=new_text,
            exit_code=context.last_command_exit_code,
        )
        return ToolResult(
            ok=True,
            summary=_task_summary(task_id, state, context.last_command_exit_code),
            content=new_text or None,
            resource_ref=f"task:{task_id}",
            state=state,
            cursor=context.task_log_cursor,
            truncated=truncated,
            recoverable=state not in TERMINAL_TASK_STATES,
            metadata={
                "task_id": task_id,
                "pid": brief.pid,
                "persistent": brief.persistent,
                "exit_code": context.last_command_exit_code,
                "log_tail": brief.log_tail,
            },
        )


class CancelTaskTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "cancel_task",
            "Terminate/cancel the active or referenced managed runtime task.",
            CancelTaskInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = CancelTaskInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.cancel",
                target=context.default_terminal_target(),
                operation="cancel",
                resource=data.task_ref or context.task_ref(),
            )
        ]

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
        pid = _task_pid(task)
        exit_code = _task_exit_code({}, task)
        brief = context.record_task_brief(
            task_id,
            state=state,
            pid=pid,
            exit_code=exit_code,
        )
        return ToolResult(
            ok=True,
            summary=f"cancelled task:{task_id}",
            resource_ref=f"task:{task_id}",
            state=state,
            metadata={
                "task_id": task_id,
                "pid": brief.pid,
                "persistent": brief.persistent,
                "exit_code": brief.exit_code,
            },
        )


class ListTasksTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "list_tasks",
            "List managed tasks started or observed in this Mini Harness conversation.",
            ListTasksInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ListTasksInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.observe",
                target=context.default_terminal_target(),
                operation="list",
                resource=data.scope,
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, ListTasksInput) else ListTasksInput.model_validate(parsed)
        tasks = sorted(
            context.task_briefs.values(),
            key=lambda item: item.touch_index,
            reverse=True,
        )
        if data.state_filter == "active":
            tasks = [task for task in tasks if task.active]
        elif data.state_filter == "terminal":
            tasks = [task for task in tasks if not task.active]
        tasks = tasks[: data.max_tasks]
        rows = [
            _render_task_brief(task.as_dict())
            for task in tasks
        ]
        return ToolResult(
            ok=True,
            summary=f"found {len(tasks)} managed task(s)",
            content="\n".join(rows) if rows else "No managed tasks recorded in this conversation.",
            metadata={
                "tasks": [task.as_dict() for task in tasks],
                "task_count": len(tasks),
                "scope": data.scope,
                "state_filter": data.state_filter,
            },
        )


class EnsureRemoteToolTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "ensure_remote_tool",
            "Check or install a remote CLI tool required by the runtime.",
            EnsureRemoteToolInput,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, EnsureRemoteToolInput)
            else EnsureRemoteToolInput.model_validate(parsed)
        )
        if context.runtime_mode != "ssh":
            return ToolResult(
                ok=True,
                summary=f"{data.tool} is not required for local runtime mode",
                metadata={"tool": data.tool, "runtime_mode": context.runtime_mode},
            )

        command = _remote_tool_command(data.tool, install=data.install)
        automatic = await self._run_remote_tool_command(
            command,
            context=context,
            max_output_chars=data.max_output_chars,
            wait_seconds=data.wait_seconds,
        )
        result = _remote_tool_result(
            data,
            automatic,
            phase="automatic",
        )
        if (
            result.ok
            or not data.install
            or not _tmux_install_needs_manual_elevation(str(result.content or ""))
        ):
            return result
        manual = await self._manual_tmux_install(
            context=context,
            max_output_chars=data.max_output_chars,
            wait_seconds=data.wait_seconds,
        )
        if manual is None:
            result.metadata["manual_elevation_available"] = True
            result.metadata["recommended_action"] = "approve_temporary_tmux_install_elevation"
            return result
        return _remote_tool_result(data, manual, phase="manual_elevation")

    async def _run_remote_tool_command(
        self,
        command: str,
        *,
        context: WorkContext,
        max_output_chars: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        task = await asyncio.to_thread(
            self.runtime.run_command,
            context.environment_id,
            context.target_id,
            ["bash", "-lc", command],
            context.runtime_cwd("."),
        )
        task_id = str(task["task_id"])
        observation = await asyncio.to_thread(
            self.runtime.observe_task,
            task_id,
            0,
            max_output_chars,
            wait_seconds,
        )
        return {
            "task_id": task_id,
            "state": str(
                observation.get("state") or observation.get("task", {}).get("state", "UNKNOWN")
            ),
            "exit_code": observation.get("exit_code"),
            "output": str(observation.get("text", "")),
            "resource_ref": f"task:{task_id}",
        }

    async def _manual_tmux_install(
        self,
        *,
        context: WorkContext,
        max_output_chars: int,
        wait_seconds: float,
    ) -> dict[str, Any] | None:
        handler = context.approval_handler
        approve = getattr(handler, "approve", None)
        prompt_secret = getattr(handler, "prompt_secret", None)
        if approve is None:
            return None
        request = _tmux_elevation_approval_request()
        if not await approve(request):
            return {
                "task_id": None,
                "state": "DENIED",
                "exit_code": None,
                "output": "ENVRT_TOOL_INSTALL_FAILED tmux: user denied temporary elevation\n",
                "resource_ref": None,
                "approved_by_user": False,
            }
        target = context.terminal_target("remote")
        command = _tmux_interactive_install_command()
        session = await asyncio.to_thread(
            self.runtime.open_terminal,
            target.environment_id,
            target.target_id,
            ["bash", "-lc", command],
            context.runtime_cwd_for(".", "remote"),
            120,
            30,
        )
        session_id = str(session["session_id"])
        chunks: list[str] = []
        cursor: int | None = None
        password_requested = False
        password_prompted = False
        sudo_password_accepted = False
        sudo_password_rejected = False
        timeout_reason: str | None = None
        password_attempts = 0
        max_password_attempts = 2
        deadline = time.monotonic() + max(wait_seconds, 180.0)
        try:
            while True:
                observation = await asyncio.to_thread(
                    self.runtime.observe_terminal,
                    session_id,
                    cursor,
                    max_output_chars,
                )
                cursor = (
                    observation.get("cursor")
                    if isinstance(observation.get("cursor"), int)
                    else cursor
                )
                text = _terminal_observation_text(observation)
                if text:
                    chunks.append(text)
                output = "".join(chunks)
                if "ENVRT_SUDO_AUTH_OK" in output:
                    sudo_password_accepted = True
                if _tmux_sudo_password_rejected(text):
                    sudo_password_rejected = True
                if (
                    "ENVRT_SUDO_PASSWORD_PROMPT" in text
                    and not sudo_password_accepted
                    and password_attempts < max_password_attempts
                ):
                    password_requested = True
                    if prompt_secret is None:
                        chunks.append(
                            "\nENVRT_TOOL_INSTALL_FAILED tmux: sudo password is required but no password prompt is available\n"
                        )
                        break
                    prompt = (
                        "Remote sudo password for installing tmux"
                        if password_attempts == 0
                        else "Remote sudo password was rejected; try again"
                    )
                    password = await prompt_secret(prompt)
                    password_prompted = True
                    password_attempts += 1
                    await asyncio.to_thread(
                        self.runtime.write_terminal,
                        session_id,
                        f"{password or ''}\n",
                    )
                elif (
                    "ENVRT_SUDO_PASSWORD_PROMPT" in text
                    and not sudo_password_accepted
                    and password_attempts >= max_password_attempts
                ):
                    chunks.append(
                        "\nENVRT_TOOL_INSTALL_FAILED tmux: sudo password was rejected\n"
                    )
                    break
                if _tmux_install_output_terminal(output):
                    break
                if time.monotonic() >= deadline:
                    timeout_reason = (
                        "sudo_auth"
                        if password_requested and not sudo_password_accepted
                        else "package_install"
                    )
                    chunks.append(
                        "\nENVRT_TOOL_INSTALL_FAILED tmux: timed out during "
                        f"{timeout_reason.replace('_', ' ')}\n"
                    )
                    break
                await asyncio.sleep(0.2)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.runtime.close_terminal, session_id)
        return {
            "task_id": session_id,
            "state": "SUCCEEDED"
            if "ENVRT_TOOL_INSTALLED tmux" in "".join(chunks)
            or "ENVRT_TOOL_PRESENT tmux" in "".join(chunks)
            else "FAILED",
            "exit_code": 0
            if "ENVRT_TOOL_INSTALLED tmux" in "".join(chunks)
            or "ENVRT_TOOL_PRESENT tmux" in "".join(chunks)
            else 1,
            "output": "".join(chunks),
            "resource_ref": f"session:{session_id}",
            "manual_elevation": True,
            "approved_by_user": True,
            "password_requested": password_requested,
            "password_prompted": password_prompted,
            "password_attempts": password_attempts,
            "sudo_password_accepted": sudo_password_accepted,
            "sudo_password_rejected": sudo_password_rejected,
            "timeout_reason": timeout_reason,
        }


class SyncStatusTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "sync_status",
            "Report local-to-remote sync manifest status and planned changes.",
            SyncStatusInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SyncStatusInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="sync.status",
                target="remote",
                operation="status",
                resource=context.sync_remote_root(),
                metadata={"workspace_id": data.workspace_id},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, SyncStatusInput)
            else SyncStatusInput.model_validate(parsed)
        )
        disabled = _sync_disabled_result(context)
        if disabled is not None:
            return disabled
        _require_push_sync_context(context)
        engine = _sync_engine(self.runtime, context, data.workspace_id)
        result = await asyncio.to_thread(engine.status)
        return _sync_tool_result(
            action="status",
            result=result,
            context=context,
            max_paths=data.max_paths,
        )


class SyncPushTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "sync_push",
            "Push local workspace changes to the configured remote mirror.",
            SyncPushInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SyncPushInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="sync.push",
                target="remote",
                operation="push",
                resource=context.sync_remote_root(),
                metadata={"workspace_id": data.workspace_id},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, SyncPushInput) else SyncPushInput.model_validate(parsed)
        disabled = _sync_disabled_result(context)
        if disabled is not None:
            return disabled
        _require_push_sync_context(context)
        engine = _sync_engine(self.runtime, context, data.workspace_id)
        try:
            result = await asyncio.to_thread(engine.push)
        except SyncConflictError as exc:
            status = await asyncio.to_thread(engine.status)
            return _sync_tool_result(
                action="push",
                result=status,
                context=context,
                max_paths=data.max_paths,
                ok=False,
                summary=f"sync push blocked by manifest conflicts: {exc}",
                error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
                recoverable=True,
            )
        return _sync_tool_result(
            action="push",
            result=result,
            context=context,
            max_paths=data.max_paths,
        )


class RequestSSHConnectionTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "request_ssh_connection",
            (
                "Ask the user to open the interactive SSH connection setup flow. "
                "This tool never accepts passwords or key contents."
            ),
            RequestSSHConnectionInput,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RequestSSHConnectionInput)
            else RequestSSHConnectionInput.model_validate(parsed)
        )
        if context.runtime_mode == "ssh":
            return ToolResult(
                ok=True,
                summary="SSH runtime is already connected",
                content="The current conversation already has an SSH runtime target.",
                metadata={"runtime_mode": context.runtime_mode},
            )
        prompt_ssh_connection = getattr(
            context.approval_handler,
            "prompt_ssh_connection",
            None,
        )
        if prompt_ssh_connection is None:
            return ToolResult(
                ok=False,
                summary="interactive SSH setup is not available",
                content=(
                    "Ask the user to enter /connect-ssh in chat. Mini Harness will then "
                    "prompt for host, user, auth method, key path, and SSH password if needed."
                ),
                error_code=ErrorCode.PERMISSION_DENIED.value,
                recoverable=True,
                metadata={
                    "requires_user_command": "/connect-ssh",
                    "password_policy": "SSH passwords are accepted only via hidden interactive prompt.",
                },
            )
        runtime_config = await prompt_ssh_connection(
            reason=data.reason,
            default_name=context.runtime_name,
        )
        if runtime_config is None:
            return ToolResult(
                ok=False,
                summary="SSH connection setup was cancelled by the user",
                error_code=ErrorCode.PERMISSION_DENIED.value,
                recoverable=True,
            )
        if not isinstance(runtime_config, RuntimeConfig):
            runtime_config = RuntimeConfig.model_validate(runtime_config)
        password_secret_ref = await _prepare_ssh_password_secret_for_tool(
            self.runtime,
            context,
            runtime_config,
        )
        try:
            bundle = await asyncio.to_thread(
                self.runtime.ensure_ssh,
                runtime_config.name,
                runtime_config.ssh,
                password_secret_ref,
            )
        except Exception:
            if password_secret_ref is not None:
                await asyncio.to_thread(self.runtime.delete_secret, password_secret_ref)
            raise
        endpoint = bundle["endpoint"]
        environment = bundle["environment"]
        remote_root = runtime_config.ssh.remote_root
        await asyncio.to_thread(self.runtime.ensure_dir, str(endpoint["endpoint_id"]), remote_root)
        context.endpoint_id = str(endpoint["endpoint_id"])
        context.environment_id = str(environment["environment_id"])
        context.target_id = str(bundle["target_id"])
        context.runtime_mode = "ssh"
        context.runtime_name = runtime_config.name
        context.remote_root = remote_root
        context.remote_hostname = runtime_config.ssh.hostname
        context.remote_username = runtime_config.ssh.username
        context.remote_port = runtime_config.ssh.port
        context.remote_auth_method = runtime_config.ssh.auth_method
        context.remote_os = "linux"
        context.remote_shell = "bash"
        context.refresh_workspace_policy()
        return ToolResult(
            ok=True,
            summary="SSH runtime connected for this conversation",
            content=(
                f"Connected to {runtime_config.ssh.username}@{runtime_config.ssh.hostname} "
                f"with remote root {remote_root}."
            ),
            metadata={
                "runtime_mode": "ssh",
                "hostname": runtime_config.ssh.hostname,
                "username": runtime_config.ssh.username,
                "remote_root": remote_root,
                "password_policy": "SSH passwords are accepted only via hidden interactive prompt.",
            },
        )


class OpenTerminalTool(RuntimeTool):
    def __init__(
        self,
        runtime: HarnessRuntimeClient,
        name: str = "open_terminal",
        fixed_target: ResolvedTerminalTarget | None = None,
    ) -> None:
        description = (
            "Open an interactive runtime terminal session."
            if fixed_target is None
            else f"Open an interactive {fixed_target} terminal session."
        )
        super().__init__(
            runtime,
            name,
            description,
            OpenTerminalInput,
        )
        self.fixed_target = fixed_target

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = OpenTerminalInput.model_validate(arguments)
        target = self.fixed_target or context.resolve_terminal_target(data.target)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.open",
                target=target,
                operation="open",
                resource=context.normalize_cwd(data.cwd),
                argv=data.argv or (),
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, OpenTerminalInput)
            else OpenTerminalInput.model_validate(parsed)
        )
        requested_target = self.fixed_target or data.target
        target = context.terminal_target(requested_target)
        argv = data.argv or _default_terminal_argv(target)
        if not argv:
            return ToolResult(
                ok=False,
                summary="terminal argv cannot be empty",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"target": target.location},
            )
        if target.location == "remote" and _starts_nested_ssh(argv):
            return ToolResult(
                ok=False,
                summary=(
                    f"{self.definition.name} argv must start a program on the configured remote target, "
                    "not run ssh again. Use argv=['bash', '-l'] for a remote shell."
                ),
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={
                    "argv": argv,
                    "target": target.location,
                    "recommended_arguments": {"argv": ["bash", "-l"], "cwd": data.cwd},
                },
            )
        cwd = context.runtime_cwd_for(data.cwd, target.location)
        sandboxed = context.sandbox_terminal(argv, cwd, target.location)
        opened = await asyncio.to_thread(
            self.runtime.open_terminal,
            target.environment_id,
            target.target_id,
            sandboxed.argv,
            sandboxed.cwd,
            data.cols,
            data.rows,
        )
        session_id = str(opened["session_id"])
        context.active_session_id = session_id
        context.terminal_cursor = None
        fallback_from = opened.get("fallback_from")
        backend = opened.get("backend", "unknown")
        target_provider = opened.get("target_provider")
        context.mark_session_state(
            target=target.location,
            os_name=target.os_name,
            shell=target.shell,
            kind=_session_kind(str(backend)),
            runtime_state=str(opened.get("state") or "ACTIVE"),
            privilege="user",
        )
        _record_session_brief_from_record(
            opened,
            context,
            updated_by=self.definition.name,
            brief=(
                f"Opened {target.location} {target.os_name}/{target.shell} terminal "
                f"via {backend}; cwd={sandboxed.cwd}"
            ),
            pending=False,
            history_only=False,
        )
        summary = (
            f"opened {target.location} session:{session_id} "
            f"using {backend} on {target.os_name}/{target.shell}"
        )
        recommended_action = None
        if fallback_from:
            recommended_action = "run ensure_remote_tool with tool=tmux and install=true"
            summary = (
                f"opened {target.location} session:{session_id} using {backend} "
                f"after {fallback_from} failed; "
                f"{recommended_action}"
            )
        return ToolResult(
            ok=True,
            summary=summary,
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            metadata={
                "session_id": session_id,
                "target": target.location,
                "target_os": target.os_name,
                "target_shell": target.shell,
                "target_provider": target_provider,
                "backend": backend,
                "fallback_from": fallback_from,
                "fallback_error": opened.get("fallback_error"),
                "recommended_action": recommended_action,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
                "sandbox_engine": sandboxed.engine,
            },
        )


class ListTerminalSessionsTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "list_terminal_sessions",
            (
                "List Runtime-managed terminal sessions. Use this to discover open or "
                "disconnected terminals instead of opening a shell and running tmux commands."
            ),
            ListTerminalSessionsInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ListTerminalSessionsInput.model_validate(arguments)
        target = "any" if data.target == "any" else context.resolve_terminal_target(data.target)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.list",
                target=target,
                operation="list",
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, ListTerminalSessionsInput)
            else ListTerminalSessionsInput.model_validate(parsed)
        )
        target = None if data.target == "any" else context.resolve_terminal_target(data.target)
        state_filter = _list_state_filter(data)
        created_after = _parse_session_boundary(data.created_after, end_of_day=False)
        created_before = _parse_session_boundary(data.created_before, end_of_day=True)
        sessions = await asyncio.to_thread(self.runtime.list_sessions)
        filtered: list[dict[str, Any]] = []
        for index, session in enumerate(sessions):
            session_id = str(session.get("session_id") or "")
            item = _session_summary_item(session, context)
            item["_source_index"] = index
            if target is not None and not _session_matches_context(session, context, target):
                continue
            if data.scope == "conversation" and session_id not in context.session_briefs:
                continue
            if not _session_matches_state_filter(item, state_filter):
                continue
            if not _session_matches_created_range(item, created_after, created_before):
                continue
            filtered.append(item)
        filtered.sort(key=_session_sort_key, reverse=True)
        filtered = filtered[: data.max_sessions]
        for item in filtered:
            item.pop("_source_index", None)
        content = _render_session_list(filtered)
        active_count = sum(1 for session in filtered if session.get("state") == "ACTIVE")
        return ToolResult(
            ok=True,
            summary=(
                f"found {len(filtered)} Runtime terminal session(s), "
                f"{active_count} active for target={data.target}, scope={data.scope}, "
                f"state_filter={state_filter}"
            ),
            content=content or "No matching Runtime terminal sessions.",
            state="ACTIVE" if active_count else "NONE",
            recoverable=True,
            metadata={
                "target": data.target,
                "scope": data.scope,
                "state_filter": state_filter,
                "max_sessions": data.max_sessions,
                "created_after": data.created_after,
                "created_before": data.created_before,
                "session_count": len(filtered),
                "active_count": active_count,
                "sessions": filtered,
                "note": (
                    "Runtime session state is authoritative for the agent; tmux ls from "
                    "another shell can miss sessions because it depends on user/socket context."
                ),
            },
        )


class InspectTerminalSessionTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "inspect_terminal_session",
            (
                "Inspect a Runtime-managed terminal session by id and report whether it is "
                "interactive or only historical output remains."
            ),
            InspectTerminalSessionInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = InspectTerminalSessionInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.inspect",
                target=context.active_session_target or context.default_terminal_target(),
                operation="inspect",
                resource=data.session_ref,
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, InspectTerminalSessionInput)
            else InspectTerminalSessionInput.model_validate(parsed)
        )
        session_id = _session_id_from_ref(data.session_ref)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        tail = (
            await asyncio.to_thread(self.runtime.observe_terminal, session_id, None, data.tail_chars)
            if data.tail_chars > 0
            else {"text": "", "cursor": None}
        )
        item = _session_summary_item(session, context)
        _apply_session_record_to_context(session, context, activate=False)
        state = str(session.get("state") or "UNKNOWN")
        interactive = _is_interactive_session(session)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=_session_output_brief(
                str(tail.get("text") or ""),
                default="Inspected terminal session state and historical output",
            ),
            pending=False if not interactive else None,
            history_only=not interactive,
        )
        item = _session_summary_item(session, context)
        content = _render_session_inspection(item, str(tail.get("text") or ""))
        return ToolResult(
            ok=True,
            summary=_session_inspection_summary(session_id, state, interactive),
            content=content,
            resource_ref=f"session:{session_id}",
            state=state,
            cursor=tail.get("cursor") if isinstance(tail.get("cursor"), int) else None,
            recoverable=True,
            metadata={
                "session": item,
                "interactive": interactive,
                "history_only": not interactive,
                "tail_chars": data.tail_chars,
                "cursor": tail.get("cursor"),
            },
        )


class ActivateTerminalSessionTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "activate_terminal_session",
            (
                "Make an existing ACTIVE Runtime terminal session the current session for "
                "observe_terminal, send_terminal_input, and run_in_session."
            ),
            ActivateTerminalSessionInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ActivateTerminalSessionInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.activate",
                target=context.active_session_target or context.default_terminal_target(),
                operation="activate",
                resource=data.session_ref,
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, ActivateTerminalSessionInput)
            else ActivateTerminalSessionInput.model_validate(parsed)
        )
        session_id = _session_id_from_ref(data.session_ref)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        state = str(session.get("state") or "UNKNOWN")
        if not _is_interactive_session(session):
            _record_session_brief_from_record(
                session,
                context,
                updated_by=self.definition.name,
                brief=f"Session is {state}; historical output only",
                pending=False,
                history_only=True,
            )
            return ToolResult(
                ok=False,
                summary=(
                    f"session:{session_id} is {state}; it can be inspected for historical "
                    "output but cannot be activated for interactive input"
                ),
                resource_ref=f"session:{session_id}",
                state=state,
                error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
                recoverable=True,
                metadata={
                    "session": _session_summary_item(session, context),
                    "interactive": False,
                    "history_only": True,
                    "recommended_tool": "inspect_terminal_session",
                },
            )
        _apply_session_record_to_context(session, context, activate=True)
        return ToolResult(
            ok=True,
            summary=f"activated session:{session_id}",
            resource_ref=f"session:{session_id}",
            state=state,
            recoverable=True,
            metadata={
                "session": _session_summary_item(session, context),
                "interactive": True,
            },
        )


class ObserveTerminalTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "observe_terminal",
            "Observe incremental output from the active terminal session.",
            ObserveTerminalInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ObserveTerminalInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.observe",
                target=context.active_session_target or context.default_terminal_target(),
                operation="observe",
                resource=data.session_ref or context.session_ref(),
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, ObserveTerminalInput)
            else ObserveTerminalInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        session_state = str(session.get("state") or "UNKNOWN")
        interactive = _is_interactive_session(session)
        _apply_session_record_to_context(
            session,
            context,
            activate=bool(data.session_ref and interactive and context.active_session_id != session_id),
        )
        wait_seconds = (
            data.wait_seconds
            if data.wait_seconds is not None
            else (0.0 if not interactive else (12.0 if context.terminal_input_pending else 1.0))
        )
        deadline = time.monotonic() + wait_seconds
        chunks: list[str] = []
        frame_count = 0
        observation: dict[str, Any] | None = None
        last_output_at: float | None = None
        meaningful_output_seen = False
        first_observe = context.terminal_cursor is None

        while True:
            observation = await asyncio.to_thread(
                self.runtime.observe_terminal,
                session_id,
                context.terminal_cursor,
                data.max_output_chars,
            )
            frames = observation.get("frames", [])
            if isinstance(frames, list) and frames:
                content = "".join(str(frame.get("data", "")) for frame in frames)
                chunks.append(content)
                frame_count += len(frames)
                last_output_at = time.monotonic()
                meaningful_output_seen = meaningful_output_seen or not _is_plain_terminal_echo(
                    content, context.last_terminal_input
                )
            elif first_observe and not chunks:
                # Some backends expose an initial tail snapshot even when no frame cursor exists yet.
                content = str(observation.get("text", ""))
                if content:
                    chunks.append(content)
                    last_output_at = time.monotonic()
                    meaningful_output_seen = True
                first_observe = False

            context.terminal_cursor = (
                int(observation["cursor"]) if observation.get("cursor") is not None else None
            )
            now = time.monotonic()
            if wait_seconds <= 0 or now >= deadline:
                break
            if (
                meaningful_output_seen
                and last_output_at is not None
                and now - last_output_at >= data.idle_seconds
            ):
                break
            await asyncio.sleep(min(0.1, max(0.0, deadline - now)))

        if observation is None:
            raise RuntimeError("terminal observation did not return a result")
        content = "".join(chunks)
        if meaningful_output_seen:
            context.terminal_input_pending = False
            _update_session_state_from_output(content, context)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=_session_output_brief(
                content,
                default=(
                    "Observed terminal output"
                    if meaningful_output_seen
                    else "No new terminal output observed"
                ),
            ),
            pending=context.terminal_input_pending,
            history_only=not interactive,
        )
        truncated = len(content) > data.max_output_chars
        if truncated:
            content = content[-data.max_output_chars :]
        return ToolResult(
            ok=True,
            summary=(
                f"observed session:{session_id}"
                if interactive
                else f"observed historical output for session:{session_id}; state={session_state}"
            ),
            content=content or None,
            resource_ref=f"session:{session_id}",
            state=session_state,
            cursor=context.terminal_cursor,
            truncated=truncated,
            recoverable=True,
            metadata={
                "session_id": session_id,
                "session_state": session_state,
                "interactive": interactive,
                "history_only": not interactive,
                "backend": session.get("backend"),
                "frame_count": frame_count,
                "wait_seconds": wait_seconds,
                "idle_seconds": data.idle_seconds,
                "terminal_input_pending": context.terminal_input_pending,
            },
        )


class SendTerminalInputTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "send_terminal_input",
            "Send input to the active terminal session.",
            SendTerminalInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SendTerminalInput.model_validate(arguments)
        target = context.active_session_target or context.default_terminal_target()
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.send_input",
                target=target,
                operation="send_input",
                resource=data.session_ref or context.session_ref(),
                metadata={"run_directly": data.run_directly},
            )
        ]
        normalized = _normalize_terminal_input(data.data, data.run_directly)
        if _should_authorize_terminal_input(normalized):
            requests.extend(
                _command_write_permission_requests(
                    tool_name=self.definition.name,
                    target=target,
                    command=normalized,
                    resource=data.session_ref or context.session_ref(),
                )
            )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, SendTerminalInput)
            else SendTerminalInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        if not _is_interactive_session(session):
            return _inactive_session_result(
                session,
                context,
                attempted_tool=self.definition.name,
                recommended_tool="inspect_terminal_session",
            )
        _apply_session_record_to_context(
            session,
            context,
            activate=bool(data.session_ref and context.active_session_id != session_id),
        )
        normalized = _normalize_terminal_input(data.data, data.run_directly)
        if _should_authorize_terminal_input(normalized):
            context.authorize_session_command(normalized)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, normalized)
        context.last_terminal_input = normalized
        context.terminal_input_pending = "\n" in normalized or "\r" in normalized
        _update_session_state_from_input(normalized, context)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=(
                "Command/input sent; output is pending"
                if context.terminal_input_pending
                else "Terminal input sent without executing a command"
            ),
            last_command=normalized if context.terminal_input_pending else None,
            pending=context.terminal_input_pending,
            history_only=False,
        )
        display = _terminal_input_display(normalized)
        return ToolResult(
            ok=True,
            summary=f"sent terminal input to session:{session_id}: {display}",
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            metadata={
                "session_id": session_id,
                "bytes": len(normalized.encode("utf-8")),
                "display": display,
                "normalized_empty_to_enter": data.data == "",
                "run_directly": data.run_directly,
                "appended_enter": normalized != data.data and data.data != "",
            },
        )


class SendTerminalControlTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "send_terminal_control",
            (
                "Send a real terminal control key such as Ctrl+C or Ctrl+D to the "
                "active terminal session."
            ),
            SendTerminalControlInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SendTerminalControlInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.send_input",
                target=context.active_session_target or context.default_terminal_target(),
                operation=f"control:{data.control}",
                resource=data.session_ref or context.session_ref(),
                metadata={"control": data.control},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, SendTerminalControlInput)
            else SendTerminalControlInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        if not _is_interactive_session(session):
            return _inactive_session_result(
                session,
                context,
                attempted_tool=self.definition.name,
                recommended_tool="inspect_terminal_session",
            )
        _apply_session_record_to_context(
            session,
            context,
            activate=bool(data.session_ref and context.active_session_id != session_id),
        )
        control_bytes = _terminal_control_bytes(data.control)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, control_bytes)
        context.last_terminal_input = control_bytes
        context.terminal_input_pending = data.control in {"ctrl_c", "ctrl_d", "enter"}
        if data.control == "ctrl_c":
            context.mark_session_state(
                privilege="unknown"
                if context.active_session_privilege == "root"
                else context.active_session_privilege,
                stateful=True,
                reason="terminal foreground process interrupted with Ctrl+C",
            )
        if data.control == "ctrl_d" and context.active_session_privilege == "root":
            context.mark_session_state(
                privilege="unknown",
                stateful=True,
                reason="terminal EOF may have exited root shell; verify with id -u",
            )
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=f"Sent terminal control {data.control}; session state may need observation",
            pending=context.terminal_input_pending,
            history_only=False,
        )
        return ToolResult(
            ok=True,
            summary=f"sent {data.control} to session:{session_id}",
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            recoverable=True,
            metadata={
                "session_id": session_id,
                "control": data.control,
                "bytes": len(control_bytes.encode("utf-8")),
                "display": _terminal_control_display(data.control),
                "terminal_input_pending": context.terminal_input_pending,
            },
        )


class RunInSessionTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "run_in_session",
            "Run a shell command inside the active terminal session and observe its output.",
            RunInSessionInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = RunInSessionInput.model_validate(arguments)
        target = context.active_session_target or context.default_terminal_target()
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="session.run",
                target=target,
                operation="run_in_session",
                resource=data.session_ref or context.session_ref(),
                argv=(data.command,),
            )
        ]
        requests.extend(
            _command_write_permission_requests(
                tool_name=self.definition.name,
                target=target,
                command=data.command,
                resource=data.session_ref or context.session_ref(),
            )
        )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RunInSessionInput)
            else RunInSessionInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        session = await asyncio.to_thread(self.runtime.get_session, session_id)
        if not _is_interactive_session(session):
            return _inactive_session_result(
                session,
                context,
                attempted_tool=self.definition.name,
                recommended_tool="inspect_terminal_session",
            )
        _apply_session_record_to_context(
            session,
            context,
            activate=bool(data.session_ref and context.active_session_id != session_id),
        )
        command = _normalize_terminal_input(data.command, run_directly=True)
        context.authorize_session_command(command)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, command)
        context.last_terminal_input = command
        context.terminal_input_pending = True
        _update_session_state_from_input(command, context)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief="Command running in terminal session; waiting for output",
            last_command=command,
            pending=True,
            history_only=False,
        )

        deadline = time.monotonic() + data.wait_seconds
        chunks: list[str] = []
        frame_count = 0
        last_output_at: float | None = None
        meaningful_output_seen = False
        while True:
            observation = await asyncio.to_thread(
                self.runtime.observe_terminal,
                session_id,
                context.terminal_cursor,
                data.max_output_chars,
            )
            frames = observation.get("frames", [])
            if isinstance(frames, list) and frames:
                content = "".join(str(frame.get("data", "")) for frame in frames)
                chunks.append(content)
                frame_count += len(frames)
                last_output_at = time.monotonic()
                meaningful_output_seen = meaningful_output_seen or not _is_plain_terminal_echo(
                    content, command
                )
            context.terminal_cursor = (
                int(observation["cursor"]) if observation.get("cursor") is not None else None
            )
            now = time.monotonic()
            if data.wait_seconds <= 0 or now >= deadline:
                break
            if (
                meaningful_output_seen
                and last_output_at is not None
                and now - last_output_at >= data.idle_seconds
            ):
                break
            await asyncio.sleep(min(0.1, max(0.0, deadline - now)))

        output = "".join(chunks)
        if meaningful_output_seen:
            context.terminal_input_pending = False
            _update_session_state_from_output(output, context)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=_session_output_brief(output, default="Ran command in terminal session"),
            last_command=command,
            pending=context.terminal_input_pending,
            history_only=False,
        )
        truncated = len(output) > data.max_output_chars
        if truncated:
            output = output[-data.max_output_chars :]
        return ToolResult(
            ok=True,
            summary=f"ran command in session:{session_id}",
            content=output or None,
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            cursor=context.terminal_cursor,
            truncated=truncated,
            recoverable=True,
            metadata={
                "session_id": session_id,
                "frame_count": frame_count,
                "wait_seconds": data.wait_seconds,
                "idle_seconds": data.idle_seconds,
                "session_privilege": context.active_session_privilege,
                "session_stateful": context.active_session_stateful,
                "session_reason": context.active_session_reason,
            },
        )


class CloseTerminalTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "close_terminal",
            "Close the active terminal session.",
            CloseTerminalInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = CloseTerminalInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.close",
                target=context.active_session_target or context.default_terminal_target(),
                operation="close",
                resource=data.session_ref or context.session_ref(),
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, CloseTerminalInput)
            else CloseTerminalInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        session = await asyncio.to_thread(self.runtime.close_terminal, session_id)
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief="Closed terminal session",
            pending=False,
            history_only=True,
        )
        context.clear_session_state()
        return ToolResult(
            ok=True,
            summary=f"closed session:{session_id}",
            resource_ref=f"session:{session_id}",
            state=str(session.get("state", "TERMINATED")),
            metadata={"session_id": session_id},
        )


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        ListFilesTool(runtime),
        ReadFileTool(runtime),
        WriteFileTool(runtime),
        EditFileTool(runtime),
        RunCommandTool(runtime),
        StartTaskTool(runtime),
        ObserveTaskTool(runtime),
        CancelTaskTool(runtime),
        ListTasksTool(runtime),
        EnsureRemoteToolTool(runtime),
        SyncStatusTool(runtime),
        SyncPushTool(runtime),
        RequestSSHConnectionTool(runtime),
        OpenTerminalTool(runtime),
        OpenTerminalTool(runtime, "open_local_terminal", fixed_target="local"),
        OpenTerminalTool(runtime, "open_remote_terminal", fixed_target="remote"),
        ListTerminalSessionsTool(runtime),
        InspectTerminalSessionTool(runtime),
        ActivateTerminalSessionTool(runtime),
        ObserveTerminalTool(runtime),
        SendTerminalInputTool(runtime),
        SendTerminalControlTool(runtime),
        RunInSessionTool(runtime),
        CloseTerminalTool(runtime),
    ]


def _sync_engine(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    workspace_id: str,
) -> SyncEngine:
    config = context.sync_config
    return SyncEngine(
        runtime=runtime,
        endpoint_id=context.endpoint_id,
        local_root=context.project_root,
        remote_root=context.sync_remote_root(),
        workspace_id=workspace_id,
        config=config,
    )


def _sync_disabled_result(context: WorkContext) -> ToolResult | None:
    config = context.sync_config
    if config is not None and config.enabled:
        return None
    return ToolResult(
        ok=False,
        summary="sync is disabled; enable [sync].enabled=true before using sync tools",
        error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
        recoverable=True,
        metadata={
            "sync_enabled": False,
            "recommended_config": {"sync.enabled": True, "sync.mode": "push"},
        },
    )


async def _prepare_ssh_password_secret_for_tool(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    runtime_config: RuntimeConfig,
) -> str | None:
    if runtime_config.ssh.auth_method != "password":
        return None
    prompt_secret = getattr(context.approval_handler, "prompt_secret", None)
    if prompt_secret is None:
        raise MiniHarnessError(
            ErrorCode.PERMISSION_DENIED,
            "ssh password auth requires interactive secret input",
            recoverable=True,
        )
    password = await prompt_secret(
        f"SSH password for {runtime_config.ssh.username}@{runtime_config.ssh.hostname}"
    )
    if not password:
        raise MiniHarnessError(
            ErrorCode.PERMISSION_DENIED,
            "ssh password auth was cancelled",
            recoverable=True,
        )
    return await asyncio.to_thread(runtime.put_secret, password, "ssh-password")


def _require_push_sync_context(context: WorkContext) -> None:
    config = context.sync_config
    if context.runtime_mode != "ssh":
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            "sync tools require ssh runtime mode because they manage a remote mirror",
            recoverable=True,
        )
    if config is not None and config.mode != "push":
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            f"sync_push currently supports mode=push, got mode={config.mode}",
            recoverable=True,
        )


def _sync_tool_result(
    *,
    action: Literal["status", "push"],
    result: SyncPushResult,
    context: WorkContext,
    max_paths: int,
    ok: bool | None = None,
    summary: str | None = None,
    error_code: str | None = None,
    recoverable: bool = False,
) -> ToolResult:
    diff = result.plan.diff_summary(max_paths)
    resolved_ok = result.ok if ok is None else ok
    state = _sync_state(action, result, resolved_ok)
    truncated_flags = diff.get("truncated", {})
    truncated = (
        any(bool(value) for value in truncated_flags.values())
        if isinstance(truncated_flags, dict)
        else False
    )
    return ToolResult(
        ok=resolved_ok,
        summary=summary or _sync_summary(action, result),
        content=_render_sync_diff(result, max_paths),
        resource_ref=f"sync:{result.workspace_id}",
        state=state,
        truncated=truncated,
        error_code=error_code,
        recoverable=recoverable or state in {"DIRTY", "CONFLICT"},
        metadata={
            "workspace_id": result.workspace_id,
            "sync_enabled": True,
            "runtime_mode": context.runtime_mode,
            "local_root": context.project_root,
            "remote_root": context.sync_remote_root(),
            "manifest_file_count": len(result.manifest.files),
            "local_state_path": result.local_state_path,
            "remote_manifest_path": result.remote_manifest_path,
            "uploaded": result.uploaded[:max_paths],
            "deleted": result.deleted[:max_paths],
            "diff": diff,
        },
    )


def _sync_state(action: Literal["status", "push"], result: SyncPushResult, ok: bool) -> str:
    if result.plan.conflicts:
        return "CONFLICT"
    if action == "push" and ok:
        return "CLEAN"
    if result.plan.has_changes:
        return "DIRTY"
    return "CLEAN"


def _sync_summary(action: Literal["status", "push"], result: SyncPushResult) -> str:
    plan = result.plan
    if plan.conflicts:
        return f"sync {action}: {len(plan.conflicts)} manifest conflict(s)"
    if action == "push":
        return f"sync push: uploaded {len(result.uploaded)} file(s), deleted {len(result.deleted)}"
    if plan.has_changes:
        return (
            f"sync status: {len(plan.uploads)} upload(s), "
            f"{len(plan.deletes)} delete(s), {len(plan.skipped)} skipped"
        )
    return f"sync status: clean, {len(plan.unchanged)} unchanged file(s)"


def _render_sync_diff(result: SyncPushResult, max_paths: int) -> str:
    plan = result.plan
    lines = [
        f"workspace_id: {result.workspace_id}",
        f"files: {len(result.manifest.files)}",
        (
            "diff: "
            f"uploads={len(plan.uploads)}, deletes={len(plan.deletes)}, "
            f"unchanged={len(plan.unchanged)}, skipped={len(plan.skipped)}, "
            f"conflicts={len(plan.conflicts)}"
        ),
    ]
    _append_sync_paths(lines, "uploads", [action.path for action in plan.uploads], max_paths)
    _append_sync_paths(lines, "deletes", [action.path for action in plan.deletes], max_paths)
    _append_sync_paths(
        lines,
        "conflicts",
        [f"{item.path} ({item.reason})" for item in plan.conflicts],
        max_paths,
    )
    _append_sync_paths(
        lines,
        "skipped",
        [
            f"{item.path} ({item.reason}{': ' + item.detail if item.detail else ''})"
            for item in plan.skipped
        ],
        max_paths,
    )
    if result.uploaded:
        _append_sync_paths(lines, "uploaded_now", result.uploaded, max_paths)
    if result.local_state_path:
        lines.append(f"local_state_path: {result.local_state_path}")
    if result.remote_manifest_path:
        lines.append(f"remote_manifest_path: {result.remote_manifest_path}")
    return "\n".join(lines)


def _append_sync_paths(lines: list[str], label: str, paths: list[str], max_paths: int) -> None:
    if not paths:
        return
    lines.append(f"{label}:")
    for path in paths[:max_paths]:
        lines.append(f"- {path}")
    if len(paths) > max_paths:
        lines.append(f"- ... {len(paths) - max_paths} more")


def _read_file_summary(
    path: str,
    selected_count: int,
    start_line: int | None,
    end_line: int | None,
) -> str:
    if selected_count == 0:
        return f"0 lines read from {path}"
    if start_line == end_line:
        return f"1 line read from {path} at line {start_line}"
    return f"{selected_count} lines read from {path} lines {start_line}-{end_line}"


def _write_file_summary(
    path: str,
    size: int,
    changed: bool,
    added_lines: int,
    removed_lines: int,
) -> str:
    if not changed:
        return f"wrote {size} bytes to {path}; no content changes"
    return f"wrote {size} bytes to {path}; diff +{added_lines} -{removed_lines}"


def _prepare_text_change(
    *,
    file_ops: WorkspaceFileOps,
    context: WorkContext,
    path: str,
    new_content: str,
    expected_sha256: str | None,
) -> PreparedTextChange | ToolResult:
    current_text, existed_before = _read_text_for_write(file_ops, path)
    return _prepare_text_change_from_current(
        context=context,
        path=path,
        current_text=current_text,
        new_content=new_content,
        expected_sha256=expected_sha256,
        existed_before=existed_before,
    )


def _prepare_edit_change(
    *,
    file_ops: WorkspaceFileOps,
    context: WorkContext,
    path: str,
    old_text: str,
    new_text: str,
    expected_sha256: str | None,
    replace_all: bool,
) -> PreparedTextChange | ToolResult:
    current_text, existed_before = _read_text_for_write(file_ops, path)
    if not existed_before:
        return ToolResult(
            ok=False,
            summary=f"cannot edit missing file: {path}",
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_NOT_FOUND",
            recoverable=True,
            metadata={"path": path, "recommended_action": "read_file"},
        )
    count = current_text.count(old_text)
    if count == 0:
        return ToolResult(
            ok=False,
            summary=f"edit context was not found in {path}; reread the file before editing",
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_NOT_FOUND",
            recoverable=True,
            metadata={"path": path, "match_count": count, "recommended_action": "read_file"},
        )
    if count > 1 and not replace_all:
        return ToolResult(
            ok=False,
            summary=(
                f"edit context matched {count} locations in {path}; provide more context "
                "or set replace_all=true"
            ),
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_AMBIGUOUS",
            recoverable=True,
            metadata={"path": path, "match_count": count},
        )
    replacement_count = count if replace_all else 1
    new_content = current_text.replace(old_text, new_text, replacement_count)
    return _prepare_text_change_from_current(
        context=context,
        path=path,
        current_text=current_text,
        new_content=new_content,
        expected_sha256=expected_sha256,
        existed_before=True,
    )


def _prepare_text_change_from_current(
    *,
    context: WorkContext,
    path: str,
    current_text: str,
    new_content: str,
    expected_sha256: str | None,
    existed_before: bool,
) -> PreparedTextChange | ToolResult:
    before_snapshot = snapshot_text(path, current_text)
    cached_snapshot = context.file_snapshot(path)
    expected_source = "argument" if expected_sha256 else None
    if expected_sha256 is None and cached_snapshot is not None:
        expected_sha256 = cached_snapshot.sha256
        expected_source = "recent_read_snapshot"
    if expected_sha256 is not None and before_snapshot.sha256 != expected_sha256:
        return ToolResult(
            ok=False,
            summary=f"file changed since it was read: {path}; reread the file before writing",
            resource_ref=f"file:{path}",
            error_code=ErrorCode.FILE_CHANGED.value,
            recoverable=True,
            metadata={
                "path": path,
                "expected_sha256": expected_sha256,
                "actual_sha256": before_snapshot.sha256,
                "expected_source": expected_source,
                "existed_before": existed_before,
                "recommended_action": "read_file",
            },
        )
    after_snapshot = snapshot_text(path, new_content)
    diff = make_unified_diff(path, before_snapshot, after_snapshot)
    return PreparedTextChange(
        path=path,
        content=new_content,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        diff=diff,
        diff_summary=summarize_diff(diff),
        expected_sha256=expected_sha256,
        expected_source=expected_source,
        existed_before=existed_before,
    )


def _prepared_change_metadata(prepared: PreparedTextChange) -> dict[str, Any]:
    return {
        "expected_sha256": prepared.expected_sha256,
        "expected_source": prepared.expected_source,
        "hash_guarded": prepared.hash_guarded,
        "unguarded_write": prepared.unguarded_write,
        "existed_before": prepared.existed_before,
        "before_sha256": prepared.before_snapshot.sha256,
        "after_sha256": prepared.after_snapshot.sha256,
        "sha256": prepared.after_snapshot.sha256,
        "line_count": prepared.after_snapshot.line_count,
        "newline": prepared.after_snapshot.newline,
        "encoding": prepared.after_snapshot.encoding,
        "diff": {
            "changed": prepared.diff.changed,
            "added_lines": prepared.diff.added_lines,
            "removed_lines": prepared.diff.removed_lines,
            "unified": prepared.diff.unified,
            "summary": prepared.diff_summary,
        },
    }


def _unguarded_write_denied_result(tool_name: str, prepared: PreparedTextChange) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=(
            f"unguarded write is disabled for {prepared.path}; read_file first or provide "
            "expected_sha256"
        ),
        resource_ref=f"file:{prepared.path}",
        error_code=ErrorCode.PERMISSION_DENIED.value,
        recoverable=True,
        metadata={
            "tool_name": tool_name,
            "path": prepared.path,
            "unguarded_write": True,
            "recommended_action": "read_file",
        },
    )


def _file_change_approval_request(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    permission_requests: list[PermissionRequest],
    prepared: PreparedTextChange,
    prefer_edit: bool,
) -> ToolApprovalRequest:
    risks: list[str] = []
    if prepared.unguarded_write:
        risks.append("This write has no expected_sha256 guard.")
    if prefer_edit:
        risks.append("This overwrites an existing file; prefer edit_file for small changes.")
    if prepared.diff.added_lines + prepared.diff.removed_lines > 200:
        risks.append("This is a large diff; review carefully before approving.")
    if not risks:
        risks.append("This operation will modify file contents.")
    return ToolApprovalRequest(
        tool_name=tool_name,
        arguments=arguments,
        decision=PermissionDecision.deny(
            f"{tool_name} will modify {prepared.path}",
            missing_capabilities=tuple(request.capability_key for request in permission_requests),
            metadata={
                "warning": _file_change_warning(prepared, prefer_edit=prefer_edit),
                "risks": risks,
            },
        ),
        permission_requests=permission_requests,
        preview_kind="diff",
        preview_title=f"Diff preview for {prepared.path}",
        preview_body=prepared.diff_summary,
    )


def _file_change_warning(prepared: PreparedTextChange, *, prefer_edit: bool) -> str:
    if prepared.unguarded_write:
        return "This file write is not protected by an expected hash."
    if prefer_edit:
        return "This overwrites an existing file; edit_file is safer for targeted changes."
    return "Review the diff before allowing this file change."


def _read_text_for_write(
    file_ops: WorkspaceFileOps,
    path: str,
) -> tuple[str, bool]:
    try:
        return file_ops.read_text(path).text, True
    except Exception as exc:
        if _looks_like_missing_file(exc):
            return "", False
        raise


def _looks_like_missing_file(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "no such file",
            "not found",
            "cannot find the path",
            "does not exist",
        )
    )


def _resolve_task_id(task_ref: str | None, context: WorkContext) -> str:
    if task_ref:
        return task_ref.removeprefix("task:")
    if context.active_task_id:
        return context.active_task_id
    raise MiniHarnessError(
        ErrorCode.TASK_NOT_FOUND, "no active task is available", recoverable=True
    )


def _resolve_session_id(session_ref: str | None, context: WorkContext) -> str:
    if session_ref:
        return _session_id_from_ref(session_ref)
    if context.active_session_id:
        return context.active_session_id
    raise MiniHarnessError(
        ErrorCode.RUNTIME_OPERATION_FAILED,
        "no active terminal session is available",
        recoverable=True,
    )


def _session_id_from_ref(session_ref: str) -> str:
    return session_ref.removeprefix("session:")


def _is_active_session_state(state: object) -> bool:
    return str(state or "").upper() == "ACTIVE"


def _is_interactive_session(session: Mapping[str, Any]) -> bool:
    return _is_active_session_state(session.get("state"))


def _list_state_filter(
    data: ListTerminalSessionsInput,
) -> Literal["all", "active", "inactive"]:
    if "include_inactive" in data.model_fields_set and "state_filter" not in data.model_fields_set:
        return "all" if data.include_inactive else "active"
    return data.state_filter


def _session_matches_context(
    session: Mapping[str, Any],
    context: WorkContext,
    target: ResolvedTerminalTarget,
) -> bool:
    location = _session_location(session, context)
    return location == target


def _session_location(
    session: Mapping[str, Any],
    context: WorkContext,
) -> ResolvedTerminalTarget | None:
    target_id = str(session.get("target_id") or "")
    local = context.local_target()
    if local is not None and target_id == local.target_id:
        return "local"
    remote = context.remote_target()
    if remote is not None and target_id == remote.target_id:
        return "remote"
    if context.runtime_mode == "local" and target_id == context.target_id:
        return "local"
    if context.runtime_mode == "ssh" and target_id == context.target_id:
        return "remote"
    return None


def _session_binding(
    session: Mapping[str, Any],
    context: WorkContext,
) -> TargetBinding | None:
    location = _session_location(session, context)
    if location == "local":
        return context.local_target()
    if location == "remote":
        return context.remote_target()
    return None


def _apply_session_record_to_context(
    session: Mapping[str, Any],
    context: WorkContext,
    *,
    activate: bool,
) -> None:
    session_id = str(session.get("session_id") or "")
    if not session_id:
        return
    binding = _session_binding(session, context)
    if activate:
        context.active_session_id = session_id
        context.terminal_cursor = None
        context.terminal_input_pending = False
    elif context.active_session_id != session_id:
        return
    context.mark_session_state(
        target=binding.location if binding is not None else None,
        os_name=binding.os_name if binding is not None else None,
        shell=binding.shell if binding is not None else None,
        kind=_session_kind(str(session.get("backend") or "")),
        runtime_state=str(session.get("state") or "UNKNOWN"),
        privilege=context.active_session_privilege if context.active_session_id == session_id else "unknown",
    )
    _record_session_brief_from_record(session, context, updated_by="inspect" if not activate else "activate")


def _session_summary_item(
    session: Mapping[str, Any],
    context: WorkContext,
) -> dict[str, Any]:
    backend_ref = session.get("backend_ref")
    if not isinstance(backend_ref, Mapping):
        backend_ref = {}
    command = session.get("command")
    session_id = str(session.get("session_id") or "")
    brief = context.session_brief(session_id)
    created_at = _session_timestamp_text(session, "created_at")
    updated_at = _session_timestamp_text(session, "updated_at")
    return {
        "session_id": session_id or session.get("session_id"),
        "state": session.get("state"),
        "interaction_state": session.get("interaction_state"),
        "backend": session.get("backend"),
        "target": _session_location(session, context) or "unknown",
        "environment_id": session.get("environment_id"),
        "target_id": session.get("target_id"),
        "default_cwd": session.get("default_cwd"),
        "created_at": created_at,
        "updated_at": updated_at,
        "command": command if isinstance(command, list) else [],
        "exit_code": session.get("exit_code"),
        "interactive": _is_interactive_session(session),
        "history_only": not _is_interactive_session(session),
        "brief": brief.brief if brief else None,
        "last_command": brief.last_command if brief else None,
        "cwd_hint": brief.cwd_hint if brief else None,
        "privilege": brief.privilege if brief else "unknown",
        "pending": brief.pending if brief else False,
        "touched_at": brief.touched_at if brief else None,
        "touch_index": brief.touch_index if brief else 0,
        "tmux_session": backend_ref.get("tmux_session"),
        "tmux_target": backend_ref.get("tmux_target"),
    }


def _render_session_list(sessions: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for session in sessions:
        parts = [
            f"- session:{session.get('session_id')}",
            f"state={session.get('state')}",
            f"backend={session.get('backend')}",
            f"target={session.get('target')}",
            f"cwd={session.get('cwd_hint') or session.get('default_cwd') or 'n/a'}",
        ]
        if session.get("privilege") not in {None, "unknown"}:
            parts.append(f"privilege={session.get('privilege')}")
        if session.get("pending"):
            parts.append("pending=true")
        if session.get("brief"):
            parts.append(f"brief={session.get('brief')}")
        if session.get("last_command"):
            parts.append(f"last={session.get('last_command')}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _session_matches_state_filter(
    session: Mapping[str, Any],
    state_filter: Literal["all", "active", "inactive"],
) -> bool:
    if state_filter == "all":
        return True
    active = _is_active_session_state(session.get("state"))
    return active if state_filter == "active" else not active


def _session_matches_created_range(
    session: Mapping[str, Any],
    created_after: dt.datetime | None,
    created_before: dt.datetime | None,
) -> bool:
    if created_after is None and created_before is None:
        return True
    created_at = _parse_session_timestamp(
        session.get("created_at") or session.get("touched_at") or session.get("updated_at")
    )
    if created_at is None:
        return False
    if created_after is not None and created_at < created_after:
        return False
    return not (created_before is not None and created_at > created_before)


def _session_sort_key(session: Mapping[str, Any]) -> tuple[float, int, int]:
    timestamp = _parse_session_timestamp(
        session.get("updated_at") or session.get("touched_at") or session.get("created_at")
    )
    timestamp_value = timestamp.timestamp() if timestamp is not None else 0.0
    touch_index = int(session.get("touch_index") or 0)
    source_index = int(session.get("_source_index") or 0)
    return (timestamp_value, touch_index, source_index)


def _parse_session_boundary(value: str | None, *, end_of_day: bool) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            day = dt.date.fromisoformat(normalized)
            boundary_time = dt.time.max if end_of_day else dt.time.min
            return dt.datetime.combine(day, boundary_time, tzinfo=dt.UTC)
        return _parse_session_timestamp(normalized)
    except ValueError as exc:
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            f"invalid ISO date/datetime for session list filter: {value}",
            recoverable=True,
        ) from exc


def _parse_session_timestamp(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _session_timestamp_text(session: Mapping[str, Any], key: str) -> str | None:
    timestamp = _parse_session_timestamp(session.get(key))
    if timestamp is None:
        value = session.get(key)
        return str(value) if value else None
    return timestamp.isoformat()


def _record_session_brief_from_record(
    session: Mapping[str, Any],
    context: WorkContext,
    *,
    updated_by: str,
    brief: str | None = None,
    last_command: str | None = None,
    pending: bool | None = None,
    history_only: bool | None = None,
) -> None:
    session_id = str(session.get("session_id") or "")
    if not session_id:
        return
    context.record_session_interaction(
        session_id,
        target=_session_location(session, context) or "unknown",
        backend=str(session.get("backend") or "unknown"),
        runtime_state=str(session.get("state") or "UNKNOWN"),
        brief=brief or _default_session_brief(session, context),
        last_command=last_command,
        cwd_hint=str(session.get("default_cwd") or "") or None,
        privilege=context.active_session_privilege if context.active_session_id == session_id else None,
        pending=pending,
        history_only=history_only if history_only is not None else not _is_interactive_session(session),
        updated_by=updated_by,
    )


def _default_session_brief(session: Mapping[str, Any], context: WorkContext) -> str:
    location = _session_location(session, context) or "unknown target"
    state = str(session.get("state") or "UNKNOWN")
    backend = str(session.get("backend") or "unknown backend")
    cwd = str(session.get("default_cwd") or "unknown cwd")
    return f"{state} {location} terminal via {backend}; cwd={cwd}"


def _session_output_brief(output: str, *, default: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return default
    tail = " | ".join(lines[-3:])
    return f"Latest terminal output: {tail}"


def _render_session_inspection(session: dict[str, Any], tail: str) -> str:
    lines = [
        f"session: {session.get('session_id')}",
        f"state: {session.get('state')}",
        f"backend: {session.get('backend')}",
        f"target: {session.get('target')}",
        f"interactive: {session.get('interactive')}",
        f"history_only: {session.get('history_only')}",
        f"cwd: {session.get('default_cwd') or 'n/a'}",
    ]
    if session.get("tmux_session"):
        lines.append(f"tmux_session: {session.get('tmux_session')}")
    command = session.get("command")
    if isinstance(command, list) and command:
        lines.append("command: " + " ".join(str(item) for item in command))
    if tail:
        lines.append("")
        lines.append("tail:")
        lines.append(tail)
    return "\n".join(lines)


def _session_inspection_summary(session_id: str, state: str, interactive: bool) -> str:
    if interactive:
        return f"session:{session_id} is {state} and can accept terminal input"
    return (
        f"session:{session_id} is {state}; historical output may be readable but "
        "interactive input is not available"
    )


def _inactive_session_result(
    session: Mapping[str, Any],
    context: WorkContext,
    *,
    attempted_tool: str,
    recommended_tool: str,
) -> ToolResult:
    session_id = str(session.get("session_id") or "")
    state = str(session.get("state") or "UNKNOWN")
    _apply_session_record_to_context(session, context, activate=False)
    _record_session_brief_from_record(
        session,
        context,
        updated_by=attempted_tool,
        brief=f"Session is {state}; {attempted_tool} could not send input",
        pending=False,
        history_only=True,
    )
    return ToolResult(
        ok=False,
        summary=(
            f"session:{session_id} is {state}; {attempted_tool} cannot send input to it. "
            "Use inspect_terminal_session to read historical output or open_terminal to start a new session."
        ),
        resource_ref=f"session:{session_id}" if session_id else None,
        state=state,
        error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
        recoverable=True,
        metadata={
            "session": _session_summary_item(session, context),
            "interactive": False,
            "history_only": True,
            "recommended_tool": recommended_tool,
        },
    )


def _starts_nested_ssh(argv: list[str]) -> bool:
    return bool(argv) and argv[0].lower() == "ssh"


def _default_terminal_argv(target: TargetBinding) -> list[str]:
    if target.os_name == "windows":
        if target.shell == "powershell":
            return ["powershell.exe", "-NoLogo"]
        return ["cmd.exe"]
    shell = target.shell or "sh"
    if shell in {"bash", "zsh", "fish"}:
        return [shell, "-l"]
    if shell.endswith(("bash", "zsh", "fish")):
        return [shell, "-l"]
    return [shell]


def _clean_task_session_guard(
    argv: list[str],
    context: WorkContext,
    force_clean: bool,
) -> ToolResult | None:
    if force_clean or not context.active_session_id:
        return None
    if (
        context.active_session_target is not None
        and context.active_session_target != context.default_terminal_target()
    ):
        return None
    if not context.active_session_stateful and context.active_session_privilege != "root":
        return None
    command = " ".join(argv)
    if context.active_session_privilege == "root":
        reason = (
            "A root-capable terminal session is active. run_command starts a clean task "
            "and will not inherit that root shell."
        )
    else:
        reason = (
            "A stateful terminal session is active. run_command starts a clean task "
            "and will not inherit that session's cwd/env/venv/login state."
        )
    return ToolResult(
        ok=False,
        summary=(
            f"{reason} Use run_in_session for commands that require this state, "
            "or set force_clean=true to deliberately run a clean task."
        ),
        error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
        recoverable=True,
        metadata={
            "attempted_command": command,
            "active_session": context.session_ref(),
            "session_kind": context.active_session_kind,
            "session_privilege": context.active_session_privilege,
            "session_stateful": context.active_session_stateful,
            "session_reason": context.active_session_reason,
            "recommended_tool": "run_in_session",
            "clean_task_override": {"force_clean": True},
        },
    )


def _command_write_permission_requests(
    *,
    tool_name: str,
    target: ResolvedTerminalTarget,
    command: str,
    resource: str | None,
) -> list[PermissionRequest]:
    write_targets = _likely_written_paths(command)
    if not write_targets:
        return []
    return [
        PermissionRequest.for_target(
            tool_name=tool_name,
            capability="file.write",
            target=target,
            operation="shell_write",
            resource=", ".join(write_targets[:10]) or resource,
            argv=(command,),
            metadata={
                "detected_shell_write": True,
                "detected_paths": write_targets[:10],
                "path_count": len(write_targets),
            },
        )
    ]


def _likely_written_paths(command: str) -> list[str]:
    normalized = _normalize_terminal_text(command)
    if not normalized:
        return []
    paths: list[str] = []
    paths.extend(_redirection_write_paths(normalized))
    paths.extend(_simple_command_write_paths(normalized))
    return _unique_paths(paths)


def _redirection_write_paths(command: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"(?<![<])(?:^|[\s;|&])(?:\d*)>>?\s*(?P<path>[^\s;&|]+)", command):
        path = _clean_shell_path(match.group("path"))
        if path:
            paths.append(path)
    return paths


def _simple_command_write_paths(command: str) -> list[str]:
    paths: list[str] = []
    statements = re.split(r"[;\n]", command)
    for statement in statements:
        tokens = _shell_words(statement)
        if not tokens:
            continue
        command_name = tokens[0]
        if command_name == "sudo" and len(tokens) > 1:
            tokens = tokens[1:]
            command_name = tokens[0]
        if command_name in {"tee", "touch", "mkdir"}:
            paths.extend(_non_option_tokens(tokens[1:]))
        elif command_name in {"cp", "mv"}:
            candidates = _non_option_tokens(tokens[1:])
            if candidates:
                paths.append(candidates[-1])
    return paths


def _shell_words(statement: str) -> list[str]:
    return [
        _clean_shell_path(token)
        for token in re.findall(r"""(?:"[^"]*"|'[^']*'|[^\s]+)""", statement)
        if _clean_shell_path(token)
    ]


def _non_option_tokens(tokens: list[str]) -> list[str]:
    return [
        token
        for token in tokens
        if not token.startswith("-") and token not in {"|", ">", ">>", "2>", "2>>"}
    ]


def _clean_shell_path(value: str) -> str:
    token = value.strip().strip("'\"")
    if not token:
        return ""
    if token.startswith(("&", "$", "`", "<", "(")):
        return ""
    if token in {"-", "/dev/null"}:
        return ""
    return token


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _session_kind(backend: str) -> Literal["pty", "tmux", "unknown"]:
    if "tmux" in backend:
        return "tmux"
    if "pty" in backend:
        return "pty"
    return "unknown"


def _update_session_state_from_input(data: str, context: WorkContext) -> None:
    command = _normalize_terminal_text(data)
    lowered = command.lower()
    if not command:
        return
    if _opens_root_shell(lowered):
        context.mark_session_state(
            privilege="root",
            stateful=True,
            reason="root shell opened in terminal session",
        )
        return
    if lowered in {"exit", "logout"} and context.active_session_privilege == "root":
        context.mark_session_state(
            privilege="unknown",
            stateful=True,
            reason="root shell may have exited; verify with id -u",
        )
        return
    stateful_reason = _stateful_shell_reason(lowered)
    if stateful_reason:
        context.mark_session_state(stateful=True, reason=stateful_reason)


def _update_session_state_from_output(content: str, context: WorkContext) -> None:
    normalized = _normalize_terminal_text(content).lower()
    if re.search(r"\buid=0\b", normalized) or re.search(r"(^|\n)0($|\n)", normalized):
        context.mark_session_state(
            privilege="root",
            stateful=True,
            reason="terminal output indicates uid 0",
        )
    elif "permission denied" in normalized and context.active_session_privilege == "root":
        context.mark_session_state(
            privilege="unknown",
            stateful=True,
            reason="terminal root state is uncertain after permission error",
        )


def _opens_root_shell(lowered_command: str) -> bool:
    patterns = [
        r"(^|[;&|]\s*)sudo\s+(-i|-s)\b",
        r"(^|[;&|]\s*)sudo\s+su(\s|$)",
        r"(^|[;&|]\s*)su\s+(-|root)?\s*$",
        r"(^|[;&|]\s*)doas\s+(-s|-u\s+root)\b",
    ]
    return any(re.search(pattern, lowered_command) for pattern in patterns)


def _stateful_shell_reason(lowered_command: str) -> str | None:
    checks = [
        (r"(^|[;&|]\s*)cd\s+", "terminal cwd changed"),
        (r"(^|[;&|]\s*)export\s+", "environment variable exported in terminal session"),
        (r"(^|[;&|]\s*)(source|\.)\s+", "shell script sourced in terminal session"),
        (r"\bactivate(\.ps1)?\b", "virtual environment activated in terminal session"),
        (r"\bconda\s+activate\b", "conda environment activated in terminal session"),
        (r"\bnvm\s+use\b", "node version selected in terminal session"),
        (r"\bssh\s+", "nested login/session state established in terminal"),
    ]
    for pattern, reason in checks:
        if re.search(pattern, lowered_command):
            return reason
    return None


def _terminal_input_display(data: str) -> str:
    if data == "\n":
        return "<ENTER>"
    if data == "\r":
        return "<CR>"
    value = data.replace("\r\n", "<ENTER>").replace("\n", "<ENTER>").replace("\r", "<CR>")
    return value if len(value) <= 120 else value[:117] + "..."


def _terminal_control_bytes(control: str) -> str:
    controls = {
        "ctrl_c": "\x03",
        "ctrl_d": "\x04",
        "enter": "\n",
        "escape": "\x1b",
        "tab": "\t",
        "backspace": "\x7f",
    }
    try:
        return controls[control]
    except KeyError as exc:
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            f"unsupported terminal control: {control}",
            recoverable=True,
        ) from exc


def _terminal_control_display(control: str) -> str:
    displays = {
        "ctrl_c": "<CTRL+C>",
        "ctrl_d": "<CTRL+D>",
        "enter": "<ENTER>",
        "escape": "<ESC>",
        "tab": "<TAB>",
        "backspace": "<BACKSPACE>",
    }
    return displays.get(control, control)


def _should_authorize_terminal_input(data: str) -> bool:
    stripped = _normalize_terminal_text(data)
    return bool(stripped) and ("\n" in data or "\r" in data)


def _normalize_terminal_input(data: str, run_directly: bool) -> str:
    if data == "":
        return "\n"
    if run_directly and not data.endswith(("\n", "\r")):
        return data + "\n"
    return data


def _is_plain_terminal_echo(content: str, expected_input: str | None) -> bool:
    observed = _normalize_terminal_text(content).replace("[REDACTED_INPUT]", "")
    lines = [line.strip() for line in observed.splitlines() if line.strip()]
    if not lines:
        return True
    if not expected_input:
        return False
    expected = _normalize_terminal_text(expected_input)
    return bool(expected) and lines == [expected]


def _normalize_terminal_text(value: str) -> str:
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return without_ansi.replace("\r\n", "\n").replace("\r", "\n").strip()


def _validation_error_summary(errors: Sequence[Mapping[str, Any]]) -> str:
    if not errors:
        return "unknown validation error"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "input"
    message = str(first.get("msg", "invalid value"))
    return f"{loc}: {message}"


def _remote_tool_command(tool: str, install: bool) -> str:
    if tool != "tmux":
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            f"unsupported remote tool: {tool}",
            recoverable=True,
        )
    if not install:
        return (
            "if command -v tmux >/dev/null 2>&1; then "
            "echo ENVRT_TOOL_PRESENT tmux; tmux -V; "
            "else echo ENVRT_TOOL_MISSING tmux; exit 7; fi"
        )
    install_body = _tmux_install_body()
    elevated_install_body = _tmux_elevated_install_body()
    return f"""
set -u
if command -v tmux >/dev/null 2>&1; then
  echo ENVRT_TOOL_PRESENT tmux
  tmux -V
  exit 0
fi
echo ENVRT_TOOL_MISSING tmux
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo -n true >/dev/null 2>&1 || {{
      echo "ENVRT_TOOL_INSTALL_FAILED tmux: sudo password or elevated privileges are required"
      exit 8
    }}
    exec sudo -n sh -c {shlex.quote(elevated_install_body)}
  else
    echo "ENVRT_TOOL_INSTALL_FAILED tmux: root or sudo is required"
    exit 8
  fi
fi
{install_body}
""".strip()


def _tmux_install_body() -> str:
    return r"""
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y tmux
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y tmux
elif command -v yum >/dev/null 2>&1; then
  yum install -y tmux
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache tmux
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm tmux
else
  echo "ENVRT_TOOL_INSTALL_FAILED tmux: unsupported package manager"
  exit 9
fi
if command -v tmux >/dev/null 2>&1; then
  echo ENVRT_TOOL_INSTALLED tmux
  tmux -V
else
  echo ENVRT_TOOL_INSTALL_FAILED tmux: install command completed but tmux is still missing
  exit 10
fi
""".strip()


def _tmux_elevated_install_body() -> str:
    return "echo ENVRT_SUDO_AUTH_OK\n" + _tmux_install_body()


def _tmux_interactive_install_command() -> str:
    install_body = _tmux_install_body()
    elevated_install_body = _tmux_elevated_install_body()
    return f"""
set -u
if command -v tmux >/dev/null 2>&1; then
  echo ENVRT_TOOL_PRESENT tmux
  tmux -V
  exit 0
fi
echo ENVRT_TOOL_MISSING tmux
if [ "$(id -u)" = "0" ]; then
  echo ENVRT_SUDO_AUTH_OK
  {install_body}
  exit $?
fi
if ! command -v sudo >/dev/null 2>&1; then
  echo "ENVRT_TOOL_INSTALL_FAILED tmux: root or sudo is required"
  exit 8
fi
if sudo -n true >/dev/null 2>&1; then
  exec sudo -n sh -c {shlex.quote(elevated_install_body)}
fi
exec sudo -S -k -p 'ENVRT_SUDO_PASSWORD_PROMPT\n' sh -c {shlex.quote(elevated_install_body)}
""".strip()


def _remote_tool_result(
    data: EnsureRemoteToolInput,
    execution: Mapping[str, Any],
    *,
    phase: str,
) -> ToolResult:
    output = str(execution.get("output") or "")
    state = str(execution.get("state") or "UNKNOWN")
    exit_code = execution.get("exit_code")
    present = "ENVRT_TOOL_PRESENT tmux" in output
    installed = "ENVRT_TOOL_INSTALLED tmux" in output
    missing = "ENVRT_TOOL_MISSING tmux" in output
    failed = (
        "ENVRT_TOOL_INSTALL_FAILED tmux" in output
        or state in TERMINAL_TASK_STATES
        and exit_code not in {0, None}
    )
    ok = present or installed
    if ok:
        summary = f"{data.tool} is available"
    elif data.install and phase == "manual_elevation":
        summary = f"{data.tool} could not be installed with temporary elevation"
    elif data.install:
        summary = f"{data.tool} could not be installed automatically"
    elif missing:
        summary = f"{data.tool} is missing; rerun ensure_remote_tool with install=true"
    else:
        summary = f"{data.tool} availability is unknown"
    metadata = {
        "tool": data.tool,
        "install_requested": data.install,
        "present": present,
        "installed": installed,
        "missing": missing,
        "failed": failed,
        "phase": phase,
        "task_id": execution.get("task_id"),
        "exit_code": exit_code,
        "recommended_action": None if ok else "install_tmux_or_enable_ssh_pty_fallback",
    }
    for key in (
        "manual_elevation",
        "approved_by_user",
        "password_requested",
        "password_prompted",
        "password_attempts",
        "sudo_password_accepted",
        "sudo_password_rejected",
        "timeout_reason",
    ):
        if key in execution:
            metadata[key] = execution[key]
    return ToolResult(
        ok=ok,
        summary=summary,
        content=output or None,
        resource_ref=execution.get("resource_ref"),
        state=state,
        recoverable=not ok,
        metadata=metadata,
    )


def _tmux_install_needs_manual_elevation(output: str) -> bool:
    return any(
        marker in output
        for marker in (
            "sudo password or elevated privileges are required",
            "root or sudo is required",
            "a password is required",
            "Permission denied",
        )
    )


def _tmux_install_output_terminal(output: str) -> bool:
    return any(
        marker in output
        for marker in (
            "ENVRT_TOOL_PRESENT tmux",
            "ENVRT_TOOL_INSTALLED tmux",
            "ENVRT_TOOL_INSTALL_FAILED tmux",
            "sudo: 3 incorrect password attempts",
        )
    )


def _tmux_sudo_password_rejected(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "sorry, try again",
            "incorrect password",
            "authentication failure",
        )
    )


def _terminal_observation_text(observation: Mapping[str, Any]) -> str:
    frames = observation.get("frames")
    if isinstance(frames, list) and frames:
        return "".join(str(frame.get("data", "")) for frame in frames)
    return str(observation.get("text") or "")


def _tmux_elevation_approval_request() -> ToolApprovalRequest:
    return ToolApprovalRequest(
        tool_name="ensure_remote_tool",
        arguments={"tool": "tmux", "install": True, "elevation": "temporary"},
        decision=PermissionDecision.deny(
            "tmux installation requires temporary remote privilege elevation",
            missing_capabilities=("remote_tool.install.elevated:remote",),
            metadata={
                "warning": (
                    "This will run a single sudo/root package-manager command on the "
                    "configured remote host. The elevation is limited to installing tmux "
                    "and no root shell is kept open."
                ),
                "risks": [
                    "The remote package manager may change system package state.",
                    "A sudo password may be requested and used only for this install attempt.",
                ],
            },
        ),
        permission_requests=[
            PermissionRequest.for_target(
                tool_name="ensure_remote_tool",
                capability="remote_tool.install.elevated",
                target="remote",
                operation="install",
                resource="tmux",
                argv=("tmux",),
            )
        ],
    )


def _task_summary(task_id: str, state: str, exit_code: int | None) -> str:
    if exit_code is None:
        return f"task:{task_id} is {state}"
    return f"task:{task_id} is {state} with exit={exit_code}"


def _task_started_summary(prefix: str, task_id: str, pid: int | None) -> str:
    if pid is None:
        return f"{prefix} task:{task_id}"
    return f"{prefix} task:{task_id} pid={pid}"


def _task_pid(task: Mapping[str, Any] | None) -> int | None:
    if not task:
        return None
    for key in ("pid", "process_id"):
        value = task.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    process = task.get("process")
    if isinstance(process, Mapping):
        return _task_pid(process)
    return None


def _task_exit_code(
    observation: Mapping[str, Any] | None,
    task: Mapping[str, Any] | None,
) -> int | None:
    for source in (observation, task):
        if not source:
            continue
        value = source.get("exit_code")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            return int(value)
    return None


def _render_task_brief(task: Mapping[str, Any]) -> str:
    argv = task.get("argv")
    command = " ".join(str(part) for part in argv) if isinstance(argv, list) else ""
    parts = [
        f"task:{task.get('task_id')}",
        f"state={task.get('state')}",
        f"pid={task.get('pid') or 'unknown'}",
        f"persistent={str(task.get('persistent')).lower()}",
        f"cwd={task.get('cwd')}",
    ]
    exit_code = task.get("exit_code")
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if command:
        parts.append(f"cmd={command}")
    log_tail = str(task.get("log_tail") or "").strip()
    if log_tail:
        compact_tail = " ".join(log_tail.split())
        parts.append(f"tail={compact_tail[:240]}")
    return " | ".join(parts)
