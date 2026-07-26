from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ..ids import new_id


class TerminalFrameKind(StrEnum):
    OUTPUT = "output"
    INPUT = "input"
    RESIZE = "resize"
    MARKER = "marker"
    REDACTED = "redacted"


class TerminalFrame(BaseModel):
    frame_id: str = Field(default_factory=lambda: new_id("terminal_frame"))
    session_id: str
    seq: int
    offset: int
    kind: TerminalFrameKind
    stream: str = "pty"
    data: str
    encoding: str = "utf-8"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
