from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult


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


__all__ = [
    "RuntimeTool",
    "_command_write_permission_requests",
    "_likely_written_paths",
    "_normalize_terminal_text",
    "_validation_error_summary",
]
