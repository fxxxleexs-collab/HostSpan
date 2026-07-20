from __future__ import annotations

from pydantic import BaseModel, Field


class CommandSpec(BaseModel):
    argv: list[str]
    shell: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    stdin_mode: str = "pipe"
