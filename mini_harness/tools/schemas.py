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


class WriteFileInput(BaseModel):
    path: str
    content: str = Field(max_length=1_000_000)


class RunCommandInput(BaseModel):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: int | None = Field(default=120, ge=1, le=3_600)
    force_clean: bool = Field(
        default=False,
        description=(
            "If true, run as a clean task even when a stateful or privileged terminal "
            "session is active. Clean tasks do not inherit terminal cwd, env, venv, "
            "login, or root state."
        ),
    )


class ObserveTaskInput(BaseModel):
    task_ref: str | None = None
    wait_seconds: float = Field(default=0.5, ge=0, le=30)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


class CancelTaskInput(BaseModel):
    task_ref: str | None = None


class EnsureRemoteToolInput(BaseModel):
    tool: Literal["tmux"] = "tmux"
    install: bool = False
    wait_seconds: float = Field(default=30.0, ge=1, le=300)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


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


class ObserveTerminalInput(BaseModel):
    session_ref: str | None = None
    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=120,
        description=(
            "Maximum seconds to wait for new terminal output. Leave unset for the default "
            "short observe, or set to expected command duration plus a small buffer."
        ),
    )
    idle_seconds: float = Field(
        default=1.5,
        ge=0.1,
        le=30,
        description="Return after this many quiet seconds once meaningful output has arrived.",
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


class SendTerminalInput(BaseModel):
    data: str = Field(
        max_length=20_000,
        description=(
            "Exact bytes/text to send to the terminal. Use an empty string or '\\n' "
            "to press Enter. Set run_directly=true to append Enter automatically "
            "when submitting a shell command."
        ),
        examples=["echo hello", "echo hello\n", "\n", "", "y\n"],
    )
    run_directly: bool = Field(
        default=False,
        description=(
            "If true, append a newline when data does not already end with Enter, "
            "so the terminal executes the command immediately. Do not use for passwords "
            "or partial interactive input unless you intend to submit it."
        ),
    )
    session_ref: str | None = None


class RunInSessionInput(BaseModel):
    command: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Shell command to execute inside the active terminal session. This inherits "
            "session state such as root shell, cwd, activated venv, exported env vars, "
            "and login state."
        ),
        examples=["apt-get install -y tmux", "pytest -q", "id"],
    )
    session_ref: str | None = None
    wait_seconds: float = Field(default=12.0, ge=0, le=300)
    idle_seconds: float = Field(default=1.5, ge=0.1, le=30)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


class CloseTerminalInput(BaseModel):
    session_ref: str | None = None
