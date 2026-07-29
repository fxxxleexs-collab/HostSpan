from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mini_harness.errors import ErrorCode, MiniHarnessError

_IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


@dataclass
class WorkContext:
    endpoint_id: str
    environment_id: str
    target_id: str
    project_root: str
    cwd: str = "."
    active_task_id: str | None = None
    task_log_cursor: int = 0
    iteration: int = 0
    last_tool_name: str | None = None
    last_task_state: str | None = None
    last_command_exit_code: int | None = None

    def normalize_path(self, path: str) -> str:
        if not path:
            path = "."
        path = path.replace("\\", "/")
        if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
            raise MiniHarnessError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "absolute paths are not allowed",
                recoverable=True,
            )
        parts: list[str] = []
        for part in path.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise MiniHarnessError(
                    ErrorCode.PATH_OUTSIDE_PROJECT,
                    "paths must stay inside the project root",
                    recoverable=True,
                )
            parts.append(part)
        return "/".join(parts) or "."

    def normalize_cwd(self, cwd: str | None) -> str:
        return self.normalize_path(cwd or self.cwd or ".")

    def runtime_cwd(self, cwd: str | None) -> str:
        relative = self.normalize_cwd(cwd)
        root = Path(self.project_root).resolve()
        resolved = root if relative == "." else (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise MiniHarnessError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "cwd must stay inside the project root",
                recoverable=True,
            )
        return str(resolved)

    def should_ignore_entry(self, relative_path: str) -> bool:
        return any(part in _IGNORED_DIRS for part in relative_path.replace("\\", "/").split("/"))

    def task_ref(self) -> str | None:
        return f"task:{self.active_task_id}" if self.active_task_id else None
