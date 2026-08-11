from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, TargetBinding, WorkContext
from mini_harness.sync.engine import SyncEngine, SyncPushResult
from mini_harness.sync.errors import SyncConflictError
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import (
    CancelTaskInput,
    CloseTerminalInput,
    EnsureRemoteToolInput,
    ListFilesInput,
    ObserveTaskInput,
    ObserveTerminalInput,
    OpenTerminalInput,
    ReadFileInput,
    RunCommandInput,
    RunInSessionInput,
    SendTerminalControlInput,
    SendTerminalInput,
    SyncPushInput,
    SyncStatusInput,
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
        if data.start_line and data.end_line and data.end_line < data.start_line:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line must be greater than or equal to start_line",
                recoverable=True,
            )
        text = await asyncio.to_thread(
            self.runtime.read_text,
            context.endpoint_id,
            context.runtime_path(path),
        )
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

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, WriteFileInput) else WriteFileInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        result = await asyncio.to_thread(
            self.runtime.write_text,
            context.endpoint_id,
            context.runtime_path(path),
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
            runtime,
            "run_command",
            (
                "Start a clean non-interactive runtime task. This does not inherit "
                "terminal session state such as root shell, cwd, env vars, venv, "
                "or login state."
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
        return ToolResult(
            ok=True,
            summary=f"started task:{task_id}",
            resource_ref=f"task:{task_id}",
            state=context.last_task_state,
            metadata={
                "task_id": task_id,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
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
        exit_code = observation.get("exit_code", task.get("exit_code"))
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
        return ToolResult(
            ok=True,
            summary=f"cancelled task:{task_id}",
            resource_ref=f"task:{task_id}",
            state=state,
            metadata={"task_id": task_id},
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
            data.max_output_chars,
            data.wait_seconds,
        )
        output = str(observation.get("text", ""))
        state = str(observation.get("state") or observation.get("task", {}).get("state", "UNKNOWN"))
        exit_code = observation.get("exit_code")
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
        elif data.install:
            summary = f"{data.tool} could not be installed automatically"
        elif missing:
            summary = f"{data.tool} is missing; rerun ensure_remote_tool with install=true"
        else:
            summary = f"{data.tool} availability is unknown"
        return ToolResult(
            ok=ok,
            summary=summary,
            content=output or None,
            resource_ref=f"task:{task_id}",
            state=state,
            recoverable=not ok,
            metadata={
                "tool": data.tool,
                "install_requested": data.install,
                "present": present,
                "installed": installed,
                "missing": missing,
                "failed": failed,
                "task_id": task_id,
                "exit_code": exit_code,
                "recommended_action": None if ok else "install_tmux_or_enable_ssh_pty_fallback",
            },
        )


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
            privilege="user",
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
        wait_seconds = (
            data.wait_seconds
            if data.wait_seconds is not None
            else (12.0 if context.terminal_input_pending else 1.0)
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
        truncated = len(content) > data.max_output_chars
        if truncated:
            content = content[-data.max_output_chars :]
        return ToolResult(
            ok=True,
            summary=f"observed session:{session_id}",
            content=content or None,
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            cursor=context.terminal_cursor,
            truncated=truncated,
            recoverable=True,
            metadata={
                "session_id": session_id,
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
        normalized = _normalize_terminal_input(data.data, data.run_directly)
        if _should_authorize_terminal_input(normalized):
            context.authorize_session_command(normalized)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, normalized)
        context.last_terminal_input = normalized
        context.terminal_input_pending = "\n" in normalized or "\r" in normalized
        _update_session_state_from_input(normalized, context)
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
        command = _normalize_terminal_input(data.command, run_directly=True)
        context.authorize_session_command(command)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, command)
        context.last_terminal_input = command
        context.terminal_input_pending = True
        _update_session_state_from_input(command, context)

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
        RunCommandTool(runtime),
        ObserveTaskTool(runtime),
        CancelTaskTool(runtime),
        EnsureRemoteToolTool(runtime),
        SyncStatusTool(runtime),
        SyncPushTool(runtime),
        OpenTerminalTool(runtime),
        OpenTerminalTool(runtime, "open_local_terminal", fixed_target="local"),
        OpenTerminalTool(runtime, "open_remote_terminal", fixed_target="remote"),
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
        return session_ref.removeprefix("session:")
    if context.active_session_id:
        return context.active_session_id
    raise MiniHarnessError(
        ErrorCode.RUNTIME_OPERATION_FAILED,
        "no active terminal session is available",
        recoverable=True,
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
    return r"""
set -u
if command -v tmux >/dev/null 2>&1; then
  echo ENVRT_TOOL_PRESENT tmux
  tmux -V
  exit 0
fi
echo ENVRT_TOOL_MISSING tmux
SUDO=""
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo -n"
  else
    echo "ENVRT_TOOL_INSTALL_FAILED tmux: root or passwordless sudo is required"
    exit 8
  fi
fi
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update && $SUDO apt-get install -y tmux
elif command -v dnf >/dev/null 2>&1; then
  $SUDO dnf install -y tmux
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y tmux
elif command -v apk >/dev/null 2>&1; then
  $SUDO apk add --no-cache tmux
elif command -v pacman >/dev/null 2>&1; then
  $SUDO pacman -Sy --noconfirm tmux
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


def _task_summary(task_id: str, state: str, exit_code: int | None) -> str:
    if exit_code is None:
        return f"task:{task_id} is {state}"
    return f"task:{task_id} is {state} with exit={exit_code}"
