from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError

from mini_harness.approvals import ApprovalHandler, ToolApprovalRequest
from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import (
    AllowAllPermissionPolicy,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult

_SANDBOX_APPROVAL_ERROR_CODES = {
    ErrorCode.PATH_OUTSIDE_PROJECT.value,
    ErrorCode.SANDBOX_DENIED.value,
}

_TERMINAL_OPEN_TOOLS = {
    "open_terminal",
    "open_local_terminal",
    "open_remote_terminal",
}


class ToolRegistry:
    def __init__(
        self,
        config: AgentConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        approve_sandbox_denials: bool = True,
        approve_terminal_open: bool = False,
        approve_root_escalation: bool = True,
    ) -> None:
        self.config = config or AgentConfig()
        self.permission_policy = permission_policy or AllowAllPermissionPolicy()
        self.approval_handler = approval_handler
        self.approve_sandbox_denials = approve_sandbox_denials
        self.approve_terminal_open = approve_terminal_open
        self.approve_root_escalation = approve_root_escalation
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                summary=f"unknown tool: {name}",
                error_code=ErrorCode.UNKNOWN_TOOL.value,
                recoverable=True,
            )
        sandbox_overridden = False
        permission_requests_result = await self._permission_requests(
            tool, name, arguments, context
        )
        if isinstance(permission_requests_result, ToolResult):
            return permission_requests_result
        permission_requests, sandbox_overridden = permission_requests_result
        permission_decision = self.permission_policy.authorize_many(permission_requests)
        permission_needs_approval = not permission_decision.allowed
        if permission_needs_approval and self.approval_handler is None:
            return _permission_denied_result(name, permission_decision, permission_requests)
        permission_overridden = False
        terminal_open_approved = False
        preview_approved = False
        try:
            preview_result = None
            if self.approval_handler is not None or not self.config.allow_unguarded_write:
                preview_result = await self._approval_preview(
                    tool, name, arguments, context, permission_requests, sandbox_overridden
                )
            if isinstance(preview_result, ToolResult):
                return preview_result
            if preview_result is not None and permission_needs_approval:
                preview_result = _approval_request_with_permission_decision(
                    preview_result,
                    permission_decision=permission_decision,
                    permission_requests=permission_requests,
                )
            if (
                preview_result is not None
                and self.approval_handler is not None
                and not await self.approval_handler.approve(preview_result)
            ):
                return _permission_denied_result(
                    name, preview_result.decision, preview_result.permission_requests
                )
            preview_approved = preview_result is not None and self.approval_handler is not None
            if permission_needs_approval:
                if preview_approved:
                    permission_overridden = True
                else:
                    approval_request = ToolApprovalRequest(
                        tool_name=name,
                        arguments=arguments,
                        decision=permission_decision,
                        permission_requests=permission_requests,
                    )
                    approved = (
                        await self.approval_handler.approve(approval_request)
                        if self.approval_handler is not None
                        else False
                    )
                    if not approved:
                        return _permission_denied_result(
                            name, permission_decision, permission_requests
                        )
                    permission_overridden = True
            if self.approve_terminal_open and name in _TERMINAL_OPEN_TOOLS:
                approval_request = _terminal_open_approval_request(
                    name, arguments, permission_requests
                )
                approved = (
                    await self.approval_handler.approve(approval_request)
                    if self.approval_handler is not None
                    else False
                )
                if not approved:
                    return _permission_denied_result(
                        name, approval_request.decision, permission_requests
                    )
                terminal_open_approved = True
            result = await self._execute_tool(tool, arguments, context, sandbox_overridden)
            if (
                _is_sandbox_denial_result(result)
                and self.approve_sandbox_denials
                and (self.approve_root_escalation or not _is_root_escalation_result(result))
                and self.approval_handler is not None
                and not sandbox_overridden
            ):
                approval_request = _sandbox_approval_request_from_result(
                    name, arguments, result, permission_requests
                )
                if await self.approval_handler.approve(approval_request):
                    sandbox_overridden = True
                    result = await self._execute_tool(tool, arguments, context, True)
                else:
                    result.metadata["approved_by_user"] = False
            if permission_overridden:
                result.metadata["permission_override"] = True
                result.metadata["approved_by_user"] = True
                result.metadata["approved_capabilities"] = list(
                    permission_decision.missing_capabilities
                )
            if sandbox_overridden:
                result.metadata["sandbox_override"] = True
                result.metadata["approved_by_user"] = True
            if terminal_open_approved:
                result.metadata["terminal_open_approved"] = True
                result.metadata["approved_by_user"] = True
            if preview_approved:
                result.metadata["preview_approved"] = True
                result.metadata["approved_by_user"] = True
            return result
        except TimeoutError:
            return ToolResult(
                ok=False,
                summary=f"tool {name} timed out",
                error_code=ErrorCode.TOOL_TIMEOUT.value,
                recoverable=True,
            )

    async def _permission_requests(
        self,
        tool: AgentTool,
        name: str,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> tuple[list[PermissionRequest], bool] | ToolResult:
        try:
            return tool.permission_requests(arguments, context), False
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                summary=f"tool arguments are invalid: {errors[0].get('msg', 'validation failed')}",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"errors": errors},
            )
        except MiniHarnessError as exc:
            if (
                self.approve_sandbox_denials
                and (self.approve_root_escalation or not _is_root_escalation_error(exc))
                and self.approval_handler is not None
                and _is_sandbox_error(exc)
            ):
                approval_request = _sandbox_approval_request_from_error(name, arguments, exc)
                if await self.approval_handler.approve(approval_request):
                    with context.approved_sandbox():
                        return tool.permission_requests(arguments, context), True
                return _mini_harness_error_result(exc, approved_by_user=False)
            return _mini_harness_error_result(exc)

    async def _execute_tool(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        context: WorkContext,
        sandbox_overridden: bool,
    ) -> ToolResult:
        if sandbox_overridden:
            with context.approved_sandbox():
                return await asyncio.wait_for(
                    tool.execute(arguments, context),
                    timeout=self.config.tool_timeout_seconds,
                )
        return await asyncio.wait_for(
            tool.execute(arguments, context),
            timeout=self.config.tool_timeout_seconds,
        )

    async def _approval_preview(
        self,
        tool: AgentTool,
        name: str,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        sandbox_overridden: bool,
    ) -> ToolApprovalRequest | ToolResult | None:
        preview = getattr(tool, "approval_preview", None)
        if preview is None:
            return None
        try:
            if sandbox_overridden:
                with context.approved_sandbox():
                    return await asyncio.wait_for(
                        preview(arguments, context, permission_requests, self.config),
                        timeout=self.config.tool_timeout_seconds,
                    )
            return await asyncio.wait_for(
                preview(arguments, context, permission_requests, self.config),
                timeout=self.config.tool_timeout_seconds,
            )
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                summary=f"tool arguments are invalid: {errors[0].get('msg', 'validation failed')}",
                error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
                recoverable=True,
                metadata={"errors": errors},
            )
        except MiniHarnessError as exc:
            return _mini_harness_error_result(exc)


def _permission_denied_result(
    name: str,
    permission_decision: PermissionDecision,
    permission_requests: list[PermissionRequest],
) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=f"permission denied for tool {name}: {permission_decision.reason}",
        error_code=ErrorCode.PERMISSION_DENIED.value,
        recoverable=True,
        metadata={
            "permission_denied": True,
            "approval_required": permission_decision.approval_required,
            "approved_by_user": False,
            "missing_capabilities": list(permission_decision.missing_capabilities),
            "permission_requests": [
                {
                    "capability": request.capability,
                    "target": request.target,
                    "operation": request.operation,
                    "resource": request.resource,
                }
                for request in permission_requests
            ],
            **permission_decision.metadata,
        },
    )


def _approval_request_with_permission_decision(
    request: ToolApprovalRequest,
    *,
    permission_decision: PermissionDecision,
    permission_requests: list[PermissionRequest],
) -> ToolApprovalRequest:
    metadata = dict(request.decision.metadata)
    risks = list(metadata.get("risks") or [])
    risks.append(permission_decision.reason)
    metadata["risks"] = risks
    for key, value in permission_decision.metadata.items():
        if key == "risks" and isinstance(value, list):
            metadata["risks"].extend(value)
        else:
            metadata.setdefault(key, value)
    missing = tuple(
        dict.fromkeys(
            [
                *request.decision.missing_capabilities,
                *permission_decision.missing_capabilities,
            ]
        )
    )
    return ToolApprovalRequest(
        tool_name=request.tool_name,
        arguments=request.arguments,
        decision=PermissionDecision.deny(
            request.decision.reason,
            missing_capabilities=missing,
            approval_required=True,
            metadata=metadata,
        ),
        permission_requests=permission_requests or request.permission_requests,
        preview_kind=request.preview_kind,
        preview_title=request.preview_title,
        preview_body=request.preview_body,
    )


def _mini_harness_error_result(
    exc: MiniHarnessError,
    *,
    approved_by_user: bool | None = None,
) -> ToolResult:
    metadata: dict[str, Any] = {}
    metadata.update(exc.metadata)
    if approved_by_user is not None:
        metadata["approved_by_user"] = approved_by_user
    return ToolResult(
        ok=False,
        summary=str(exc),
        error_code=exc.code.value,
        recoverable=exc.recoverable,
        metadata=metadata,
    )


def _sandbox_approval_request_from_error(
    name: str,
    arguments: dict[str, Any],
    exc: MiniHarnessError,
) -> ToolApprovalRequest:
    return ToolApprovalRequest(
        tool_name=name,
        arguments=arguments,
        decision=PermissionDecision.deny(
            str(exc),
            missing_capabilities=("sandbox.override:any",),
            metadata=_sandbox_warning_metadata(
                exc.code.value,
                sandbox_reason=exc.metadata.get("sandbox_reason"),
            ),
        ),
        permission_requests=[
            PermissionRequest.for_target(
                tool_name=name,
                capability="sandbox.override",
                target="any",
                operation=exc.code.value,
                resource=str(exc),
            )
        ],
    )


def _sandbox_approval_request_from_result(
    name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    permission_requests: list[PermissionRequest],
) -> ToolApprovalRequest:
    return ToolApprovalRequest(
        tool_name=name,
        arguments=arguments,
        decision=PermissionDecision.deny(
            result.summary,
            missing_capabilities=("sandbox.override:any",),
            metadata=_sandbox_warning_metadata(
                result.error_code,
                sandbox_reason=result.metadata.get("sandbox_reason"),
            ),
        ),
        permission_requests=permission_requests
        or [
            PermissionRequest.for_target(
                tool_name=name,
                capability="sandbox.override",
                target="any",
                operation=result.error_code,
                resource=result.summary,
            )
        ],
    )


def _terminal_open_approval_request(
    name: str,
    arguments: dict[str, Any],
    permission_requests: list[PermissionRequest],
) -> ToolApprovalRequest:
    capabilities = tuple(request.capability_key for request in permission_requests)
    return ToolApprovalRequest(
        tool_name=name,
        arguments=arguments,
        decision=PermissionDecision.deny(
            "opening an interactive terminal requires user approval",
            missing_capabilities=capabilities,
            metadata={
                "warning": (
                    "An interactive terminal can keep state such as cwd, env vars, "
                    "login sessions, and elevated privileges."
                ),
                "risks": [
                    "Later terminal input may execute arbitrary shell commands in this session.",
                    "A remote terminal runs on the configured SSH host, not just the local project.",
                    "Privilege escalation inside the terminal can persist until the session is closed.",
                ],
            },
        ),
        permission_requests=permission_requests,
    )


def _sandbox_warning_metadata(
    error_code: str | None,
    *,
    sandbox_reason: object | None = None,
) -> dict[str, Any]:
    if sandbox_reason == "root_escalation":
        return {
            "warning": (
                "This operation attempts to open or enter a root/elevated shell. "
                "The elevated state can persist inside this terminal session."
            ),
            "risks": [
                "Subsequent terminal commands in this session may run with root privileges.",
                "On a remote target, this can modify system files, packages, users, services, or process state on the SSH host.",
                "A root shell can bypass project workspace boundaries and ordinary file permission protections.",
                "Only approve if you intend to grant temporary administrative control for this task, and close or exit the root shell when finished.",
            ],
        }
    if error_code == ErrorCode.PATH_OUTSIDE_PROJECT.value:
        warning = "This operation references a path outside the configured workspace."
    else:
        warning = "This operation was blocked by the workspace sandbox policy."
    return {
        "warning": warning,
        "risks": [
            "The tool may read, write, or execute outside the configured project boundary.",
            "On SSH targets, this can affect files or processes on the remote host.",
            "Only approve if the path or command is expected for this task.",
        ],
    }


def _is_sandbox_error(exc: MiniHarnessError) -> bool:
    return exc.code.value in _SANDBOX_APPROVAL_ERROR_CODES


def _is_root_escalation_error(exc: MiniHarnessError) -> bool:
    return exc.metadata.get("sandbox_reason") == "root_escalation"


def _is_sandbox_denial_result(result: ToolResult) -> bool:
    return result.error_code in _SANDBOX_APPROVAL_ERROR_CODES


def _is_root_escalation_result(result: ToolResult) -> bool:
    return result.metadata.get("sandbox_reason") == "root_escalation"


def tool_call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'))}"
