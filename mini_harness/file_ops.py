from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import ResolvedTerminalTarget, WorkContext

FileOpsBackend = Literal["runtime", "sync-mirror"]


@dataclass(frozen=True)
class FileLocation:
    path: str
    runtime_path: str
    endpoint_id: str
    target: ResolvedTerminalTarget
    backend: FileOpsBackend = "runtime"

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "runtime_path": self.runtime_path,
            "endpoint_id": self.endpoint_id,
            "target": self.target,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class FileRead:
    location: FileLocation
    text: str


@dataclass(frozen=True)
class ParentDirectoryResult:
    path: str | None
    runtime_path: str | None
    ensured: bool

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "path": self.path,
            "runtime_path": self.runtime_path,
            "ensured": self.ensured,
        }


@dataclass(frozen=True)
class FileWrite:
    location: FileLocation
    size: int
    parent_directory: ParentDirectoryResult


class WorkspaceFileOps(Protocol):
    backend: FileOpsBackend

    def location(self, path: str) -> FileLocation: ...

    def read_text(self, path: str) -> FileRead: ...

    def write_text(self, path: str, text: str, *, ensure_parent: bool = True) -> FileWrite: ...

    def ensure_parent_directory(self, path: str) -> ParentDirectoryResult: ...


class RuntimeWorkspaceFileOps:
    backend: FileOpsBackend = "runtime"

    def __init__(self, runtime: HarnessRuntimeClient, context: WorkContext) -> None:
        self.runtime = runtime
        self.context = context

    def location(self, path: str) -> FileLocation:
        normalized = self.context.normalize_path(path)
        return FileLocation(
            path=normalized,
            runtime_path=self.context.runtime_path(normalized),
            endpoint_id=self.context.endpoint_id,
            target=self.context.default_terminal_target(),
            backend=self.backend,
        )

    def read_text(self, path: str) -> FileRead:
        location = self.location(path)
        return FileRead(
            location=location,
            text=self.runtime.read_text(location.endpoint_id, location.runtime_path),
        )

    def write_text(self, path: str, text: str, *, ensure_parent: bool = True) -> FileWrite:
        location = self.location(path)
        parent = (
            self.ensure_parent_directory(location.path)
            if ensure_parent
            else ParentDirectoryResult(path=None, runtime_path=None, ensured=False)
        )
        result = self.runtime.write_text(location.endpoint_id, location.runtime_path, text)
        size = int(result.get("size", len(text.encode("utf-8"))))
        return FileWrite(location=location, size=size, parent_directory=parent)

    def ensure_parent_directory(self, path: str) -> ParentDirectoryResult:
        location = self.location(path)
        parent = parent_directory(location.path)
        if parent is None:
            return ParentDirectoryResult(path=None, runtime_path=None, ensured=False)
        parent_location = self.location(parent)
        self.runtime.ensure_dir(parent_location.endpoint_id, parent_location.runtime_path)
        return ParentDirectoryResult(
            path=parent_location.path,
            runtime_path=parent_location.runtime_path,
            ensured=True,
        )


def parent_directory(path: str) -> str | None:
    normalized = path.replace("\\", "/").rstrip("/")
    if not normalized or normalized in {".", "/"}:
        return None
    parts = [part for part in normalized.split("/") if part]
    if len(parts) <= 1:
        return None
    return "/".join(parts[:-1])
