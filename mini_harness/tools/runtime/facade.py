from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.base import AgentTool
from mini_harness.tools.schemas import ToolDefinition, ToolResult


class FileToolInput(BaseModel):
    action: Literal["list", "read", "write", "edit"]
    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=200, ge=1, le=2_000)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(default=None, ge=1, le=5_000)
    content: str | None = Field(default=None, max_length=1_000_000)
    old_text: str | None = Field(default=None, max_length=200_000)
    new_text: str | None = Field(default=None, max_length=200_000)
    expected_sha256: str | None = None
    replace_all: bool = False


class CommandToolInput(BaseModel):
    action: Literal["run"] = "run"
    target: Literal["local", "remote"] = "local"
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: int | None = Field(default=10, ge=1, le=3_600)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    force_clean: bool = False


class TaskToolInput(BaseModel):
    action: Literal["start", "observe", "cancel", "list"]
    target: Literal["local", "remote"] = "local"
    argv: list[str] | None = Field(default=None, min_length=1)
    cwd: str = "."
    wait_seconds: float = Field(
        default=1.0,
        ge=0,
        le=30,
        description=(
            "For action=start, initial wait for startup logs. For action=observe, maximum "
            "seconds to wait for new logs or task completion; use 0 for an immediate poll "
            "and larger values for long operations."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    task_ref: str | None = None
    scope: Literal["conversation"] = "conversation"
    state_filter: Literal["all", "active", "terminal"] = "all"
    max_tasks: int = Field(default=10, ge=1, le=100)


class RemoteToolInput(BaseModel):
    action: Literal["ensure_tool", "request_ssh_connection"]
    tool: Literal["tmux"] = "tmux"
    install: bool = False
    wait_seconds: float = Field(default=300.0, ge=1, le=900)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    reason: str = Field(default="remote runtime is needed", max_length=500)


class SyncToolInput(BaseModel):
    action: Literal["status", "push"]
    workspace_id: str = Field(default="default", min_length=1, max_length=120)
    max_paths: int = Field(default=50, ge=1, le=500)


class TerminalToolInput(BaseModel):
    action: Literal[
        "open",
        "list",
        "inspect",
        "activate",
        "observe",
        "command",
        "control",
        "human_input",
        "close",
    ]
    target: Literal["current", "local", "remote", "any"] = "current"
    argv: list[str] | None = None
    cwd: str = "."
    cols: int = Field(default=120, ge=20, le=400)
    rows: int = Field(default=30, ge=5, le=120)
    session_ref: str | None = None
    tail_chars: int = Field(default=4000, ge=0, le=100_000)
    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=300,
        description=(
            "For action=observe, maximum seconds to wait for terminal output. Leave unset "
            "for a quick observe; use 0 for an immediate poll; set larger values when a "
            "foreground command is expected to produce output later."
        ),
    )
    idle_seconds: float = Field(
        default=1.5,
        ge=0.1,
        le=30,
        description=(
            "For action=observe, return after this many quiet seconds once meaningful "
            "output has arrived."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    data: str | None = Field(default=None, max_length=20_000)
    input_only: bool = False
    control: Literal["ctrl_c", "ctrl_d", "enter", "escape", "tab", "backspace"] | None = None
    prompt: str | None = Field(default=None, max_length=500)
    submit: bool = True
    scope: Literal["all", "conversation"] = "all"
    state_filter: Literal["all", "active", "inactive"] = "all"
    max_sessions: int = Field(default=10, ge=1, le=500)
    created_after: str | None = None
    created_before: str | None = None


class FacadeTool(AgentTool):
    def __init__(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        tools: Mapping[str, AgentTool],
        action_map: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> None:
        self._definition = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_model.model_json_schema(),
        )
        self.input_model = input_model
        self.tools = dict(tools)
        self.action_map = dict(action_map)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        tool, mapped = self._resolve(arguments)
        return tool.permission_requests(mapped, context)

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        tool, mapped = self._resolve(arguments)
        preview = getattr(tool, "approval_preview", None)
        if preview is None:
            return None
        result = await preview(mapped, context, permission_requests, config)
        if not isinstance(result, ToolApprovalRequest):
            return result
        return ToolApprovalRequest(
            tool_name=self.definition.name,
            arguments=arguments,
            decision=result.decision,
            permission_requests=result.permission_requests,
            preview_kind=result.preview_kind,
            preview_title=result.preview_title,
            preview_body=result.preview_body,
        )

    async def execute(self, arguments: dict[str, Any], context: WorkContext) -> ToolResult:
        try:
            tool, mapped = self._resolve(arguments)
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
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code=exc.code.value,
                recoverable=exc.recoverable,
                metadata=dict(exc.metadata),
            )
        result = await tool.execute(mapped, context)
        result.metadata.setdefault("facade_tool", self.definition.name)
        result.metadata.setdefault("facade_action", str(arguments.get("action", "")))
        result.metadata.setdefault("inner_tool", tool.definition.name)
        return result

    def _resolve(self, arguments: dict[str, Any]) -> tuple[AgentTool, dict[str, Any]]:
        data = self.input_model.model_validate(arguments)
        raw = data.model_dump(exclude_none=True)
        action = str(raw.get("action") or "")
        try:
            tool_name, fields = self.action_map[action]
        except KeyError as exc:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                f"unsupported {self.definition.name} action: {action}",
                recoverable=True,
            ) from exc
        try:
            tool = self.tools[tool_name]
        except KeyError as exc:
            raise MiniHarnessError(
                ErrorCode.UNKNOWN_TOOL,
                f"facade target tool is not registered: {tool_name}",
                recoverable=True,
            ) from exc
        return tool, {field: raw[field] for field in fields if field in raw}


def build_facade_tools(internal_tools: list[AgentTool]) -> list[AgentTool]:
    tools = {tool.definition.name: tool for tool in internal_tools}
    return [
        FacadeTool(
            "file",
            "File operations. Use action=list/read/write/edit.",
            FileToolInput,
            tools,
            {
                "list": ("list_files", ("path", "recursive", "max_entries")),
                "read": ("read_file", ("path", "start_line", "end_line", "max_lines")),
                "write": ("write_file", ("path", "content", "expected_sha256")),
                "edit": (
                    "edit_file",
                    ("path", "old_text", "new_text", "expected_sha256", "replace_all"),
                ),
            },
        ),
        FacadeTool(
            "command",
            "Run a short clean non-interactive command. Use action=run.",
            CommandToolInput,
            tools,
            {
                "run": (
                    "run_command",
                    (
                        "target",
                        "argv",
                        "cwd",
                        "timeout_seconds",
                        "max_output_chars",
                        "force_clean",
                    ),
                )
            },
        ),
        FacadeTool(
            "task",
            "Manage long-running non-interactive tasks. Use action=start/observe/list/cancel.",
            TaskToolInput,
            tools,
            {
                "start": (
                    "start_task",
                    ("target", "argv", "cwd", "wait_seconds", "max_output_chars"),
                ),
                "observe": (
                    "observe_task",
                    ("task_ref", "wait_seconds", "max_output_chars"),
                ),
                "cancel": ("cancel_task", ("task_ref",)),
                "list": ("list_tasks", ("scope", "state_filter", "max_tasks")),
            },
        ),
        FacadeTool(
            "remote",
            "Remote setup and remote runtime tool checks. Use action=ensure_tool/request_ssh_connection.",
            RemoteToolInput,
            tools,
            {
                "ensure_tool": (
                    "ensure_remote_tool",
                    ("tool", "install", "wait_seconds", "max_output_chars"),
                ),
                "request_ssh_connection": ("request_ssh_connection", ("reason",)),
            },
        ),
        FacadeTool(
            "sync",
            "Local-to-remote mirror operations. Use action=status/push.",
            SyncToolInput,
            tools,
            {
                "status": ("sync_status", ("workspace_id", "max_paths")),
                "push": ("sync_push", ("workspace_id", "max_paths")),
            },
        ),
        FacadeTool(
            "terminal",
            "Interactive terminal operations. Use only for live input, passwords, REPLs, or stateful shell context.",
            TerminalToolInput,
            tools,
            {
                "open": ("open_terminal", ("target", "argv", "cwd", "cols", "rows")),
                "list": (
                    "list_terminal_sessions",
                    (
                        "target",
                        "scope",
                        "state_filter",
                        "max_sessions",
                        "created_after",
                        "created_before",
                    ),
                ),
                "inspect": ("inspect_terminal_session", ("session_ref", "tail_chars")),
                "activate": ("activate_terminal_session", ("session_ref",)),
                "observe": (
                    "observe_terminal",
                    ("session_ref", "wait_seconds", "idle_seconds", "max_output_chars"),
                ),
                "command": ("terminal_command", ("session_ref", "data", "input_only")),
                "control": ("send_terminal_control", ("session_ref", "control")),
                "human_input": (
                    "request_human_terminal_input",
                    ("session_ref", "prompt", "submit"),
                ),
                "close": ("close_terminal", ("session_ref",)),
            },
        ),
    ]


__all__ = [
    "CommandToolInput",
    "FacadeTool",
    "FileToolInput",
    "RemoteToolInput",
    "SyncToolInput",
    "TaskToolInput",
    "TerminalToolInput",
    "build_facade_tools",
]
