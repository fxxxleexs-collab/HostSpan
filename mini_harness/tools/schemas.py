from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolResult(BaseModel):
    ok: bool
    summary: str
    content: str | None = None
    resource_ref: str | None = None
    state: str | None = None
    cursor: int | None = None
    truncated: bool = False
    error_code: str | None = None
    recoverable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListFilesInput(BaseModel):
    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=200, ge=1, le=2_000)


class ReadFileInput(BaseModel):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(
        default=None,
        ge=1,
        le=5_000,
        description=(
            "Read at most this many lines starting at start_line. Use this for block/"
            "paged reads. Do not combine with end_line."
        ),
    )


class WriteFileInput(BaseModel):
    path: str
    target: Literal["current", "local", "remote", "sync"] = Field(
        default="current",
        description=(
            "Write target. current preserves the runtime default; local writes the local "
            "workspace; remote writes the SSH workspace; sync writes locally and then "
            "pushes that file to the configured remote mirror."
        ),
    )
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(
        default=None,
        description=(
            "Optional sha256 of the file version this write is based on. If omitted, "
            "Mini Harness uses the most recent file action=\"read\" snapshot for this "
            "path when available."
        ),
    )


class EditFileInput(BaseModel):
    path: str
    old_text: str = Field(min_length=1, max_length=200_000)
    new_text: str = Field(max_length=200_000)
    expected_sha256: str | None = Field(
        default=None,
        description=(
            "Optional sha256 of the file version this edit is based on. If omitted, "
            "Mini Harness uses the most recent file action=\"read\" snapshot for this "
            "path when available."
        ),
    )
    replace_all: bool = False


class RunCommandInput(BaseModel):
    target: Literal["current", "local", "remote"] = Field(
        default="current",
        description=(
            "Execution target. current uses the default runtime target; local runs on "
            "the user's machine when a local target is available; remote runs on the "
            "configured SSH host."
        ),
    )
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: int | None = Field(
        default=10,
        ge=1,
        le=3_600,
        description=(
            "Maximum seconds to wait for command output and completion before returning "
            "a RUNNING task_id that can be observed later with task action=\"observe\"."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    force_clean: bool = Field(
        default=False,
        description=(
            "If true, run as a clean task even when a stateful or privileged terminal "
            "session is active. Clean tasks do not inherit terminal cwd, env, venv, "
            "login, or root state."
        ),
    )
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence task state summary to save if this command becomes "
            "a tracked task. Do not include secrets or raw logs."
        ),
    )


class StartTaskInput(BaseModel):
    target: Literal["current", "local", "remote"] = Field(
        default="current",
        description=(
            "Execution target. current uses the default runtime target; local runs on "
            "the user's machine when a local target is available; remote runs on the "
            "configured SSH host."
        ),
    )
    argv: list[str] = Field(
        min_length=1,
        description=(
            "Command to start as a managed long-running, non-interactive task. "
            "Use this for dev servers, watchers, and services; use terminal tools "
            "only when human interaction or shell state is required."
        ),
        examples=[["python", "app.py"], ["npm", "run", "dev"], ["uvicorn", "app:app"]],
    )
    cwd: str = "."
    wait_seconds: float = Field(
        default=1.0,
        ge=0,
        le=30,
        description=(
            "Initial time to wait for startup logs after starting the task. Use "
            "task action=\"observe\" later for more logs."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence task state summary to save in the runtime activity "
            "context. Use when this call clarifies what the task is doing. Do not "
            "include secrets or raw logs."
        ),
    )


class ObserveTaskInput(BaseModel):
    task_ref: str | None = None
    wait_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        description=(
            "Maximum seconds to wait for new task logs or a terminal task state before "
            "returning. Use 0 for an immediate poll; set a larger value for long "
            "operations so the agent does not repeatedly poll."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence task state summary to save in the runtime activity "
            "context. Use when logs reveal the task purpose, status, or next step. "
            "Do not include secrets or raw logs."
        ),
    )


class CancelTaskInput(BaseModel):
    task_ref: str | None = None
    brief: str | None = Field(
        default=None,
        max_length=240,
        description="Optional one-sentence final task summary. Do not include secrets.",
    )


class ListTasksInput(BaseModel):
    scope: Literal["conversation"] = Field(
        default="conversation",
        description="Currently lists tasks started or observed in this Mini Harness conversation.",
    )
    state_filter: Literal["all", "active", "terminal"] = "all"
    max_tasks: int = Field(default=10, ge=1, le=100)


class EnsureRemoteToolInput(BaseModel):
    tool: Literal["tmux"] = "tmux"
    install: bool = False
    wait_seconds: float = Field(default=300.0, ge=1, le=900)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


class SyncStatusInput(BaseModel):
    workspace_id: str = Field(default="default", min_length=1, max_length=120)
    max_paths: int = Field(default=50, ge=1, le=500)


class SyncPushInput(BaseModel):
    workspace_id: str = Field(default="default", min_length=1, max_length=120)
    max_paths: int = Field(default=50, ge=1, le=500)


class RequestSSHConnectionInput(BaseModel):
    reason: str = Field(
        default="remote runtime is needed",
        max_length=500,
        description=(
            "Brief reason why an SSH runtime is needed. Do not include passwords, "
            "tokens, or private key contents."
        ),
    )


class OpenTerminalInput(BaseModel):
    target: Literal["current", "local", "remote"] = Field(
        default="current",
        description=(
            "Terminal target. current uses the configured default runtime target; local "
            "opens on the user's machine; remote opens on the configured SSH host."
        ),
    )
    argv: list[str] | None = Field(
        default=None,
        description=(
            "Program to start inside the selected runtime target. Leave unset to use "
            "the target's default interactive shell. In SSH runtime mode, do not run "
            "ssh here when target is remote; the terminal is already opened on the "
            "remote host."
        ),
    )
    cwd: str = "."
    cols: int = Field(default=120, ge=20, le=400)
    rows: int = Field(default=30, ge=5, le=120)
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence terminal state summary to save in the runtime "
            "activity context. Describe what this session is for. Do not include secrets."
        ),
    )


class ListTerminalSessionsInput(BaseModel):
    target: Literal["current", "local", "remote", "any"] = Field(
        default="current",
        description=(
            "Filter sessions by terminal target. current uses the configured default target; "
            "any includes every target visible to the Runtime session registry."
        ),
    )
    scope: Literal["all", "conversation"] = Field(
        default="all",
        description=(
            "all lists Runtime registry sessions; conversation lists only sessions touched "
            "during the current Mini Harness conversation."
        ),
    )
    state_filter: Literal["all", "active", "inactive"] = Field(
        default="all",
        description="Filter by Runtime session state.",
    )
    max_sessions: int = Field(default=10, ge=1, le=500)
    created_after: str | None = Field(
        default=None,
        description=(
            "Optional ISO date or datetime lower bound, for example 2026-08-11 or "
            "2026-08-11T10:00:00Z. Sessions without timestamps are excluded when set."
        ),
    )
    created_before: str | None = Field(
        default=None,
        description=(
            "Optional ISO date or datetime upper bound, for example 2026-08-11 or "
            "2026-08-11T18:00:00Z. Sessions without timestamps are excluded when set."
        ),
    )

class InspectTerminalSessionInput(BaseModel):
    session_ref: str
    tail_chars: int = Field(default=4000, ge=0, le=100_000)
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence terminal state summary based on this inspection. "
            "Do not include secrets or raw logs."
        ),
    )


class ActivateTerminalSessionInput(BaseModel):
    session_ref: str
    brief: str | None = Field(
        default=None,
        max_length=240,
        description="Optional one-sentence terminal state summary. Do not include secrets.",
    )


class ObserveTerminalInput(BaseModel):
    session_ref: str | None = None
    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=300,
        description=(
            "Maximum seconds to wait for new terminal output before returning. Leave "
            "unset for the default quick observe; use 0 for an immediate poll; set to "
            "the expected command duration plus a small buffer for long terminal work."
        ),
    )
    idle_seconds: float = Field(
        default=1.5,
        ge=0.1,
        le=30,
        description="Return after this many quiet seconds once meaningful output has arrived.",
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence terminal state summary to save in runtime activity. "
            "Use when output reveals what the session is doing, whether it is waiting, "
            "or the next likely action. Do not include secrets or raw logs."
        ),
    )


class SendTerminalInput(BaseModel):
    data: str = Field(
        max_length=20_000,
        description=(
            "Exact bytes/text to send to the terminal. Use an empty string or '\\n' "
            "to press Enter. By default, Mini Harness appends Enter when data does "
            "not already end with Enter, so shell commands and password prompts are "
            "submitted immediately. Set input_only=true only when typing text without "
            "submitting it."
        ),
        examples=["echo hello", "echo hello\n", "\n", "", "y\n"],
    )
    input_only: bool = Field(
        default=False,
        description=(
            "If true, send data exactly as provided and do not append Enter. Use for "
            "partial interactive typing only; leave false for commands, confirmations, "
            "and passwords that should be submitted."
        ),
    )
    session_ref: str | None = None
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence terminal state summary after sending input. "
            "Do not include secrets or raw logs."
        ),
    )


class RequestHumanTerminalInput(BaseModel):
    prompt: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Human-facing prompt explaining what input is needed for the active terminal. "
            "Use for passwords, one-time codes, or other sensitive interactive input that "
            "must not be visible to the model."
        ),
    )
    session_ref: str | None = None
    submit: bool = Field(
        default=True,
        description=(
            "If true, append Enter before sending the hidden user input. Leave true for "
            "password prompts, yes/no prompts, sudo, su, and most terminal interactions."
        ),
    )
    brief: str | None = Field(
        default=None,
        max_length=240,
        description=(
            "Optional one-sentence terminal state summary after hidden user input. "
            "Do not include the hidden input or any secret."
        ),
    )


class SendTerminalControlInput(BaseModel):
    control: Literal["ctrl_c", "ctrl_d", "enter", "escape", "tab", "backspace"] = Field(
        description=(
            "Terminal control key to send as real control bytes. Use ctrl_c to interrupt "
            "a running foreground process instead of sending literal '\\x03' text."
        )
    )
    session_ref: str | None = None
    brief: str | None = Field(
        default=None,
        max_length=240,
        description="Optional one-sentence terminal state summary. Do not include secrets.",
    )


class CloseTerminalInput(BaseModel):
    session_ref: str | None = None
    brief: str | None = Field(
        default=None,
        max_length=240,
        description="Optional one-sentence terminal closing summary. Do not include secrets.",
    )
