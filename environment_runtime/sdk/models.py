from __future__ import annotations

from pydantic import BaseModel


class ApiResponse(BaseModel):
    detail: str | None = None
