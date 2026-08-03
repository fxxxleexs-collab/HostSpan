from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, TargetBinding, WorkContext
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
    SendTerminalInput,
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
        task = await asyncio.to_thread(
            self.runtime.run_command,
            context.environment_id,
            context.target_id,
            data.argv,
            cwd,
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
        opened = await asyncio.to_thread(
            self.runtime.open_terminal,
            target.environment_id,
            target.target_id,
            argv,
            context.runtime_cwd_for(data.cwd, target.location),
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
                "argv": argv,
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

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, SendTerminalInput)
            else SendTerminalInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        normalized = _normalize_terminal_input(data.data, data.run_directly)
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


class RunInSessionTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "run_in_session",
            "Run a shell command inside the active terminal session and observe its output.",
            RunInSessionInput,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RunInSessionInput)
            else RunInSessionInput.model_validate(parsed)
        )
        session_id = _resolve_session_id(data.session_ref, context)
        command = _normalize_terminal_input(data.command, run_directly=True)
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
        OpenTerminalTool(runtime),
        OpenTerminalTool(runtime, "open_local_terminal", fixed_target="local"),
        OpenTerminalTool(runtime, "open_remote_terminal", fixed_target="remote"),
        ObserveTerminalTool(runtime),
        SendTerminalInputTool(runtime),
        RunInSessionTool(runtime),
        CloseTerminalTool(runtime),
    ]


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
