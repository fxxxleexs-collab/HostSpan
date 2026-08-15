from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import shlex
import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import RuntimeConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionDecision, PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, TargetBinding, WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.common import (
    TERMINAL_TASK_STATES,
    RuntimeTool,
    _command_write_permission_requests,
    _normalize_terminal_text,
)
from mini_harness.tools.runtime.file import (
    EditFileTool as EditFileTool,
)
from mini_harness.tools.runtime.file import (
    ListFilesTool as ListFilesTool,
)
from mini_harness.tools.runtime.file import (
    ReadFileTool as ReadFileTool,
)
from mini_harness.tools.runtime.file import (
    WriteFileTool as WriteFileTool,
)
from mini_harness.tools.runtime.sync import (
    SyncPushTool as SyncPushTool,
)
from mini_harness.tools.runtime.sync import (
    SyncStatusTool as SyncStatusTool,
)
from mini_harness.tools.runtime.task import (
    CancelTaskTool as CancelTaskTool,
)
from mini_harness.tools.runtime.task import (
    ListTasksTool as ListTasksTool,
)
from mini_harness.tools.runtime.task import (
    ObserveTaskTool as ObserveTaskTool,
)
from mini_harness.tools.runtime.task import (
    RunCommandTool as RunCommandTool,
)
from mini_harness.tools.runtime.task import (
    StartTaskTool as StartTaskTool,
)
from mini_harness.tools.schemas import (
    ActivateTerminalSessionInput,
    CloseTerminalInput,
    EnsureRemoteToolInput,
    InspectTerminalSessionInput,
    ListTerminalSessionsInput,
    ObserveTerminalInput,
    OpenTerminalInput,
    RequestHumanTerminalInput,
    RequestSSHConnectionInput,
    RunInSessionInput,
    SendTerminalControlInput,
    SendTerminalInput,
    ToolResult,
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
                "observe_terminal, send_terminal_input, and run_terminal_command."
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
                metadata={"run_directly": data.run_directly, "input_only": data.input_only},
            )
        ]
        normalized = _normalize_terminal_input(
            data.data,
            run_directly=data.run_directly,
            input_only=data.input_only,
        )
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
        normalized = _normalize_terminal_input(
            data.data,
            run_directly=data.run_directly,
            input_only=data.input_only,
        )
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
                "input_only": data.input_only,
                "appended_enter": normalized != data.data and data.data != "",
            },
        )


class RequestHumanTerminalInputTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "request_human_terminal_input",
            (
                "Ask the user for hidden input and submit it to the active terminal session. "
                "Use after observe_terminal shows a password, one-time code, or other "
                "sensitive prompt. The user input is not returned to the model. By default "
                "the tool appends Enter before sending it."
            ),
            RequestHumanTerminalInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = RequestHumanTerminalInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="terminal.human_input",
                target=context.active_session_target or context.default_terminal_target(),
                operation="human_input",
                resource=data.session_ref or context.session_ref(),
                metadata={"hidden": True, "submit": data.submit},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, RequestHumanTerminalInput)
            else RequestHumanTerminalInput.model_validate(parsed)
        )
        handler = context.approval_handler
        prompt_secret = getattr(handler, "prompt_secret", None)
        if prompt_secret is None:
            return ToolResult(
                ok=False,
                summary="human terminal input requires an interactive secret prompt",
                error_code=ErrorCode.PERMISSION_DENIED.value,
                recoverable=True,
                metadata={"secret_prompt_available": False},
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
        secret = await prompt_secret(data.prompt)
        if secret is None:
            return ToolResult(
                ok=False,
                summary="human terminal input was cancelled",
                error_code=ErrorCode.PERMISSION_DENIED.value,
                recoverable=True,
                resource_ref=f"session:{session_id}",
                metadata={"cancelled": True, "secret_prompt_available": True},
            )
        payload = _normalize_human_terminal_input(secret, submit=data.submit)
        await asyncio.to_thread(self.runtime.write_terminal, session_id, payload)
        context.last_terminal_input = payload
        context.terminal_input_pending = data.submit
        _record_session_brief_from_record(
            session,
            context,
            updated_by=self.definition.name,
            brief=(
                "Hidden human input submitted; output is pending"
                if data.submit
                else "Hidden human input sent without submit"
            ),
            last_command="[HIDDEN HUMAN INPUT]" if data.submit else None,
            pending=context.terminal_input_pending,
            history_only=False,
        )
        return ToolResult(
            ok=True,
            summary=f"submitted hidden human input to session:{session_id}",
            resource_ref=f"session:{session_id}",
            state="ACTIVE",
            recoverable=True,
            metadata={
                "session_id": session_id,
                "hidden_input": True,
                "submitted": data.submit,
                "bytes": len(payload.encode("utf-8")),
                "terminal_input_pending": context.terminal_input_pending,
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
    def __init__(self, runtime: HarnessRuntimeClient, name: str = "run_in_session") -> None:
        super().__init__(
            runtime,
            name,
            (
                "Submit a shell command into the active interactive terminal session "
                "and observe terminal output. This is terminal input, not a clean task: "
                "it inherits that terminal's cwd, env, venv, login, and sudo/root state. "
                "Use only when the command depends on active terminal state or interaction; "
                "otherwise use run_command."
            ),
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
        command = _normalize_terminal_input(data.command, run_directly=True, input_only=False)
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
                "command": command,
                "tool_semantics": "terminal_session_command",
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
    from mini_harness.tools.runtime.builder import build_runtime_tools as build

    return build(runtime)


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


def _normalize_terminal_input(data: str, run_directly: bool, input_only: bool = False) -> str:
    if data == "":
        return "\n"
    if input_only:
        return data
    if run_directly and not data.endswith(("\n", "\r")):
        return data + "\n"
    return data


def _normalize_human_terminal_input(data: str, *, submit: bool) -> str:
    if not submit:
        return data
    if data.endswith(("\n", "\r")):
        return data
    return data + "\n"


def _is_plain_terminal_echo(content: str, expected_input: str | None) -> bool:
    observed = _normalize_terminal_text(content).replace("[REDACTED_INPUT]", "")
    lines = [line.strip() for line in observed.splitlines() if line.strip()]
    if not lines:
        return True
    if not expected_input:
        return False
    expected = _normalize_terminal_text(expected_input)
    return bool(expected) and lines == [expected]


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
