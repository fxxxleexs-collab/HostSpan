from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.common import TERMINAL_TASK_STATES, RuntimeTool
from mini_harness.tools.runtime.shell_analysis import _command_write_permission_requests
from mini_harness.tools.schemas import (
    CancelTaskInput,
    ListTasksInput,
    ObserveTaskInput,
    RunCommandInput,
    StartTaskInput,
    ToolResult,
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
        binding = context.terminal_target(data.target)
        cwd = context.normalize_cwd(data.cwd)
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.run",
                target=binding.location,
                operation="run",
                resource=cwd,
                argv=data.argv,
            )
        ]
        requests.extend(
            _command_write_permission_requests(
                tool_name=self.definition.name,
                target=binding.location,
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
        binding = context.terminal_target(data.target)
        guard = _clean_task_session_guard(
            data.argv,
            context,
            data.force_clean,
            binding.location,
        )
        if guard is not None:
            return guard
        cwd = context.runtime_cwd_for(data.cwd, binding.location)
        sandboxed = context.sandbox_task(data.argv, cwd, binding.location)
        task = await asyncio.to_thread(
            self.runtime.run_command,
            binding.environment_id,
            binding.target_id,
            sandboxed.argv,
            sandboxed.cwd,
        )
        task_id = str(task["task_id"])
        context.active_task_id = task_id
        context.task_log_cursor = 0
        context.last_task_state = str(task.get("state", "RUNNING"))
        context.last_command_exit_code = None
        pid = _task_pid(task)
        content: str | None = None
        truncated = False
        if data.timeout_seconds and data.timeout_seconds > 0:
            observation = await asyncio.to_thread(
                self.runtime.observe_task,
                task_id,
                0,
                data.max_output_chars,
                float(data.timeout_seconds),
            )
            content = str(observation.get("text", "")) or None
            context.task_log_cursor = int(observation.get("cursor", 0))
            task = observation.get("task", task)
            state = str(observation.get("state") or task.get("state", context.last_task_state))
            context.last_task_state = state
            context.last_command_exit_code = _task_exit_code(observation, task)
            pid = _task_pid(task) or pid
            truncated = bool(observation.get("truncated", False))
            if state in TERMINAL_TASK_STATES:
                context.active_task_id = None
        else:
            state = context.last_task_state
        timed_out = state not in TERMINAL_TASK_STATES
        brief = context.record_task_brief(
            task_id,
            argv=sandboxed.argv,
            cwd=sandboxed.cwd,
            state=state,
            pid=pid,
            persistent=bool(task.get("persistent", False)),
            brief=data.brief,
            log_tail=content,
            exit_code=context.last_command_exit_code,
            started_by=self.definition.name,
        )
        context.record_runtime_transition(
            kind="task",
            action="run",
            ref=f"task:{task_id}",
            summary=_run_command_summary(
                task_id,
                state,
                context.last_command_exit_code,
                timed_out,
            ),
            state=state,
            active_after=context.task_ref() or "none",
        )
        return ToolResult(
            ok=True,
            summary=_run_command_summary(task_id, state, context.last_command_exit_code, timed_out),
            content=content,
            resource_ref=f"task:{task_id}",
            state=state,
            cursor=context.task_log_cursor,
            truncated=truncated,
            recoverable=timed_out,
            metadata={
                "task_id": task_id,
                "pid": brief.pid,
                "persistent": brief.persistent,
                "brief": brief.brief,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
                "exit_code": context.last_command_exit_code,
                "log_tail": brief.log_tail,
                "timed_out": timed_out,
                "observe_timeout_seconds": data.timeout_seconds,
                "target": binding.location,
                "target_os": binding.os_name,
                "target_shell": binding.shell,
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
        binding = context.terminal_target(data.target)
        cwd = context.normalize_cwd(data.cwd)
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="task.run",
                target=binding.location,
                operation="start",
                resource=cwd,
                argv=data.argv,
            )
        ]
        requests.extend(
            _command_write_permission_requests(
                tool_name=self.definition.name,
                target=binding.location,
                command=" ".join(data.argv),
                resource=cwd,
            )
        )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, StartTaskInput) else StartTaskInput.model_validate(parsed)
        binding = context.terminal_target(data.target)
        cwd = context.runtime_cwd_for(data.cwd, binding.location)
        sandboxed = context.sandbox_task(data.argv, cwd, binding.location)
        task = await asyncio.to_thread(
            self.runtime.start_task,
            binding.environment_id,
            binding.target_id,
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
            brief=data.brief,
            log_tail=content,
            exit_code=exit_code,
            started_by=self.definition.name,
        )
        context.record_runtime_transition(
            kind="task",
            action="start",
            ref=f"task:{task_id}",
            summary=_task_started_summary("started long-running task", task_id, pid),
            state=state,
            active_after=context.task_ref() or "none",
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
                "brief": brief.brief,
                "argv": sandboxed.argv,
                "cwd": sandboxed.cwd,
                "target": binding.location,
                "target_os": binding.os_name,
                "target_shell": binding.shell,
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
            brief=data.brief,
            log_tail=new_text,
            exit_code=context.last_command_exit_code,
        )
        context.record_runtime_transition(
            kind="task",
            action="observe",
            ref=f"task:{task_id}",
            summary=_task_summary(task_id, state, context.last_command_exit_code),
            state=state,
            active_after=context.task_ref() or "none",
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
                "brief": brief.brief,
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
            brief=data.brief,
            exit_code=exit_code,
        )
        context.record_runtime_transition(
            kind="task",
            action="cancel",
            ref=f"task:{task_id}",
            summary=f"cancelled task:{task_id}",
            state=state,
            active_after=context.task_ref() or "none",
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
                "brief": brief.brief,
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
        rows = [_render_task_brief(task.as_dict()) for task in tasks]
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


def build_task_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        RunCommandTool(runtime),
        StartTaskTool(runtime),
        ObserveTaskTool(runtime),
        CancelTaskTool(runtime),
        ListTasksTool(runtime),
    ]


def _resolve_task_id(task_ref: str | None, context: WorkContext) -> str:
    if task_ref:
        return task_ref.removeprefix("task:")
    if context.active_task_id:
        return context.active_task_id
    raise MiniHarnessError(
        ErrorCode.TASK_NOT_FOUND,
        "no active task is available",
        recoverable=True,
    )


def _clean_task_session_guard(
    argv: list[str],
    context: WorkContext,
    force_clean: bool,
    target: ResolvedTerminalTarget,
) -> ToolResult | None:
    if force_clean or not context.active_session_id:
        return None
    if context.active_session_target is not None and context.active_session_target != target:
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
            f"{reason} Use terminal action=\"command\" for commands that require this state, "
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
            "recommended_tool": "terminal",
            "recommended_action": "command",
            "clean_task_override": {"force_clean": True},
        },
    )


def _task_summary(task_id: str, state: str, exit_code: int | None) -> str:
    if exit_code is None:
        return f"task:{task_id} is {state}"
    return f"task:{task_id} is {state} with exit={exit_code}"


def _task_started_summary(prefix: str, task_id: str, pid: int | None) -> str:
    if pid is None:
        return f"{prefix} task:{task_id}"
    return f"{prefix} task:{task_id} pid={pid}"


def _run_command_summary(
    task_id: str,
    state: str,
    exit_code: int | None,
    timed_out: bool,
) -> str:
    if timed_out:
        return f"command still running as task:{task_id}; use task observe for more output"
    if exit_code is None:
        return f"command completed as task:{task_id} state={state}"
    return f"command completed as task:{task_id} state={state} exit={exit_code}"


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
    brief = str(task.get("brief") or "").strip()
    if brief:
        parts.append(f"brief={brief}")
    log_tail = str(task.get("log_tail") or "").strip()
    if log_tail:
        compact_tail = " ".join(log_tail.split())
        parts.append(f"tail={compact_tail[:240]}")
    return " | ".join(parts)


__all__ = [
    "CancelTaskTool",
    "ListTasksTool",
    "ObserveTaskTool",
    "RunCommandTool",
    "StartTaskTool",
    "build_task_tools",
]
