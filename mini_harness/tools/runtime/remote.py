from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import RuntimeConfig, SSHRuntimeConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionDecision, PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.ssh_trust import approve_trust_host_once, is_untrusted_host_key_error
from mini_harness.runtime.work_context import RemoteEnvironmentInfo, RemoteToolStatus, WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.common import TERMINAL_TASK_STATES, RuntimeTool
from mini_harness.tools.schemas import EnsureRemoteToolInput, RequestSSHConnectionInput, ToolResult


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
            context.record_runtime_transition(
                kind="remote",
                action="ensure_tool",
                ref=data.tool,
                summary=f"{data.tool} is not required for local runtime mode",
                state="SKIPPED",
                active_after=context.remote_address_summary(),
            )
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
            _record_remote_tool_status_from_result(context, data.tool, result)
            context.record_runtime_transition(
                kind="remote",
                action="ensure_tool",
                ref=data.tool,
                summary=result.summary,
                state=str(result.state or result.metadata.get("state") or "UNKNOWN"),
                active_after=context.remote_address_summary(),
            )
            return result
        manual = await self._manual_tmux_install(
            context=context,
            max_output_chars=data.max_output_chars,
            wait_seconds=data.wait_seconds,
        )
        if manual is None:
            result.metadata["manual_elevation_available"] = True
            result.metadata["recommended_action"] = "approve remote action=\"ensure_tool\" temporary elevation"
            _record_remote_tool_status_from_result(context, data.tool, result)
            context.record_runtime_transition(
                kind="remote",
                action="ensure_tool",
                ref=data.tool,
                summary=result.summary,
                state=str(result.state or result.metadata.get("state") or "UNKNOWN"),
                active_after=context.remote_address_summary(),
            )
            return result
        result = _remote_tool_result(data, manual, phase="manual_elevation")
        _record_remote_tool_status_from_result(context, data.tool, result)
        context.record_runtime_transition(
            kind="remote",
            action="ensure_tool",
            ref=data.tool,
            summary=result.summary,
            state=str(result.state or result.metadata.get("state") or "UNKNOWN"),
            active_after=context.remote_address_summary(),
        )
        return result

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
            context.record_runtime_transition(
                kind="remote",
                action="request_ssh_connection",
                ref=context.remote_address_summary(),
                summary="SSH runtime is already connected",
                state="CONNECTED",
                active_after=context.remote_address_summary(),
            )
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
            bundle = await _ensure_ssh_bundle_with_root(
                self.runtime.ensure_ssh,
                self.runtime.ensure_dir,
                runtime_config.name,
                runtime_config.ssh,
                runtime_config.ssh.remote_root,
                password_secret_ref=password_secret_ref,
                trust_host_once=False,
            )
        except Exception as exc:
            if not is_untrusted_host_key_error(exc) or not await approve_trust_host_once(
                context.approval_handler,
                tool_name="request_ssh_connection",
                ssh=runtime_config.ssh,
                error=exc,
            ):
                if password_secret_ref is not None:
                    await asyncio.to_thread(self.runtime.delete_secret, password_secret_ref)
                raise
            try:
                bundle = await _ensure_ssh_bundle_with_root(
                    self.runtime.ensure_ssh,
                    self.runtime.ensure_dir,
                    runtime_config.name,
                    runtime_config.ssh,
                    runtime_config.ssh.remote_root,
                    password_secret_ref=password_secret_ref,
                    trust_host_once=True,
                )
            except Exception:
                if password_secret_ref is not None:
                    await asyncio.to_thread(self.runtime.delete_secret, password_secret_ref)
                raise
        endpoint = bundle["endpoint"]
        environment = bundle["environment"]
        remote_root = runtime_config.ssh.remote_root
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
        context.remote_os = "unknown"
        context.remote_shell = "unknown"
        context.refresh_workspace_policy()
        await probe_remote_environment(self.runtime, context)
        context.record_runtime_transition(
            kind="remote",
            action="request_ssh_connection",
            ref=context.remote_address_summary(),
            summary="SSH runtime connected for this conversation",
            state="CONNECTED",
            active_after=context.remote_address_summary(),
        )
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


def build_remote_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        EnsureRemoteToolTool(runtime),
        RequestSSHConnectionTool(runtime),
    ]


async def probe_remote_tool_status(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    *,
    tool: str = "tmux",
    wait_seconds: float = 5.0,
    max_output_chars: int = 4_000,
) -> RemoteToolStatus:
    if context.runtime_mode != "ssh":
        return context.record_remote_tool_status(
            tool,
            "unknown",
            reason="remote runtime is not connected",
        )
    try:
        command = _remote_tool_command(tool, install=False)
        task = await asyncio.to_thread(
            runtime.run_command,
            context.environment_id,
            context.target_id,
            ["bash", "-lc", command],
            context.runtime_cwd_for(".", "remote"),
        )
        task_id = str(task["task_id"])
        observation = await asyncio.to_thread(
            runtime.observe_task,
            task_id,
            0,
            max_output_chars,
            wait_seconds,
        )
        result = _remote_tool_result(
            EnsureRemoteToolInput(
                tool="tmux",
                install=False,
                wait_seconds=wait_seconds,
                max_output_chars=max_output_chars,
            ),
            {
                "task_id": task_id,
                "state": str(
                    observation.get("state")
                    or observation.get("task", {}).get("state", "UNKNOWN")
                ),
                "exit_code": observation.get("exit_code"),
                "output": str(observation.get("text", "")),
                "resource_ref": f"task:{task_id}",
            },
            phase="probe",
        )
        status = _record_remote_tool_status_from_result(context, tool, result)
        context.record_runtime_transition(
            kind="remote",
            action="probe_tool",
            ref=tool,
            summary=f"remote {tool} probe: {status.status}",
            state=status.status.upper(),
            active_after=context.remote_address_summary(),
        )
        return status
    except Exception as exc:
        status = context.record_remote_tool_status(
            tool,
            "unknown",
            reason=f"probe failed: {exc}",
        )
        context.record_runtime_transition(
            kind="remote",
            action="probe_tool",
            ref=tool,
            summary=f"remote {tool} probe failed",
            state="UNKNOWN",
            active_after=context.remote_address_summary(),
        )
        return status


async def probe_remote_environment(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
) -> RemoteEnvironmentInfo:
    if context.runtime_mode != "ssh":
        return context.record_remote_environment(
            status="unknown",
            reason="remote runtime is not connected",
        )
    try:
        health = await asyncio.to_thread(runtime.endpoint_health, context.endpoint_id)
        environment = health.get("environment") if isinstance(health, Mapping) else None
        if not isinstance(environment, Mapping):
            environment = {}
        info = context.record_remote_environment(
            status="ok",
            os_name=_env_text(environment, "uname_s"),
            arch=_env_text(environment, "uname_m"),
            shell=_env_text(environment, "shell") or _first_available_shell(environment),
            sh_path=_env_text(environment, "sh_path"),
            bash_path=_env_text(environment, "bash_path"),
            python3_path=_env_text(environment, "python3_path"),
            python_path=_env_text(environment, "python_path"),
            python3_version=_env_text(environment, "python3_version"),
            python_version=_env_text(environment, "python_version"),
            nohup_path=_env_text(environment, "nohup_path"),
            tmux_path=_env_text(environment, "tmux_path"),
            tmux_version=_env_text(environment, "tmux_version"),
            sudo_path=_env_text(environment, "sudo_path"),
        )
        if info.tmux_path:
            context.record_remote_tool_status(
                "tmux",
                "present",
                version=info.tmux_version or info.tmux_path,
            )
        else:
            context.record_remote_tool_status(
                "tmux",
                "missing",
                reason="not found by SSH environment probe",
            )
        missing_core = [
            name
            for name, value in {
                "sh": info.sh_path,
                "nohup": info.nohup_path,
                "python3": info.python3_path,
                "python": info.python_path,
            }.items()
            if not value
        ]
        context.record_runtime_transition(
            kind="remote",
            action="probe_environment",
            ref=context.remote_address_summary(),
            summary=(
                f"remote environment probe ok: os={info.os_name} arch={info.arch}; "
                f"missing={','.join(missing_core) if missing_core else 'none'}"
            ),
            state="OK",
            active_after=context.remote_address_summary(),
        )
        return info
    except Exception as exc:
        info = context.record_remote_environment(
            status="unknown",
            reason=f"probe failed: {exc}",
        )
        context.record_runtime_transition(
            kind="remote",
            action="probe_environment",
            ref=context.remote_address_summary(),
            summary="remote environment probe failed",
            state="UNKNOWN",
            active_after=context.remote_address_summary(),
        )
        return info


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


async def _ensure_ssh_bundle_with_root(
    ensure_ssh: Callable[..., dict[str, Any]],
    ensure_dir: Callable[[str, str], dict[str, Any]],
    name: str,
    ssh: SSHRuntimeConfig,
    remote_root: str,
    *,
    password_secret_ref: str | None,
    trust_host_once: bool,
) -> dict[str, Any]:
    bundle = await asyncio.to_thread(
        ensure_ssh,
        name,
        ssh,
        password_secret_ref,
        trust_host_once,
    )
    endpoint = bundle["endpoint"]
    if not isinstance(endpoint, Mapping):
        raise MiniHarnessError(
            ErrorCode.RUNTIME_OPERATION_FAILED,
            "runtime ensure_ssh returned an invalid endpoint payload",
            recoverable=False,
        )
    await asyncio.to_thread(ensure_dir, str(endpoint["endpoint_id"]), remote_root)
    return bundle


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
        summary = f"{data.tool} is missing; rerun remote action=\"ensure_tool\" with install=true"
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
        "recommended_action": None
        if ok
        else 'remote action="ensure_tool" install=true or enable ssh_pty fallback',
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


def _record_remote_tool_status_from_result(
    context: WorkContext,
    tool: str,
    result: ToolResult,
) -> RemoteToolStatus:
    version = _remote_tool_version(tool, str(result.content or ""))
    if result.metadata.get("present") or result.metadata.get("installed"):
        return context.record_remote_tool_status(tool, "present", version=version)
    if result.metadata.get("missing"):
        return context.record_remote_tool_status(tool, "missing", reason=result.summary)
    return context.record_remote_tool_status(tool, "unknown", reason=result.summary)


def _remote_tool_version(tool: str, output: str) -> str | None:
    if tool != "tmux":
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("tmux "):
            return stripped
    return None


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


def _env_text(environment: Mapping[str, Any], key: str) -> str | None:
    value = environment.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_available_shell(environment: Mapping[str, Any]) -> str | None:
    return _env_text(environment, "bash_path") or _env_text(environment, "sh_path")


def _tmux_elevation_approval_request() -> ToolApprovalRequest:
    return ToolApprovalRequest(
        tool_name="remote",
        arguments={"action": "ensure_tool", "tool": "tmux", "install": True, "elevation": "temporary"},
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
                tool_name="remote",
                capability="remote_tool.install.elevated",
                target="remote",
                operation="install",
                resource="tmux",
                argv=("tmux",),
            )
        ],
    )


__all__ = [
    "EnsureRemoteToolTool",
    "RequestSSHConnectionTool",
    "build_remote_tools",
    "probe_remote_environment",
    "probe_remote_tool_status",
]
