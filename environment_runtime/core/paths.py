from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, field_validator

from .errors import SecurityError, ValidationError


class WorkspacePath(BaseModel):
    workspace_id: str
    root_id: str
    relative_path: str = Field(default="")

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        if value in {"", "."}:
            return ""
        normalized = PurePosixPath(value.replace("\\", "/")).as_posix()
        if normalized.startswith("/"):
            raise ValidationError("workspace path cannot be absolute")
        if ".." in PurePosixPath(normalized).parts:
            raise ValidationError("workspace path cannot escape the root")
        return normalized

    def as_uri(self) -> str:
        tail = f"/{self.relative_path}" if self.relative_path else ""
        return f"workspace://{self.workspace_id}/{self.root_id}{tail}"


def ensure_allowed_local_path(path: Path, allowed_roots: list[Path]) -> Path:
    resolved = path.expanduser().resolve()
    for root in allowed_roots:
        root_resolved = root.expanduser().resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise SecurityError(f"path {resolved} is outside the allowed local roots")
