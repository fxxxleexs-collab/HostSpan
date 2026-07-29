from __future__ import annotations

from typing import Any

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


class ObserveTaskInput(BaseModel):
    task_ref: str | None = None
    wait_seconds: float = Field(default=0.5, ge=0, le=30)
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)


class CancelTaskInput(BaseModel):
    task_ref: str | None = None
