from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    runtime_mode: Literal["local", "ssh"] = "local"
    remote_root: str | None = None
    cwd: str = "."
    active_task_id: str | None = None
    task_log_cursor: int = 0
    active_session_id: str | None = None
    active_session_kind: Literal["pty", "tmux", "unknown"] | None = None
    active_session_privilege: Literal["unknown", "user", "root"] = "unknown"
    active_session_stateful: bool = False
    active_session_reason: str | None = None
    terminal_cursor: int | None = None
    terminal_input_pending: bool = False
    last_terminal_input: str | None = None
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
        if self.runtime_mode == "ssh":
            return self.runtime_path(relative)
        root = Path(self.project_root).resolve()
        resolved = root if relative == "." else (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise MiniHarnessError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "cwd must stay inside the project root",
                recoverable=True,
            )
        return str(resolved)

    def runtime_path(self, path: str) -> str:
        relative = self.normalize_path(path)
        if self.runtime_mode != "ssh":
            return relative
        root = self._normalized_remote_root()
        if relative == ".":
            return root
        return f"{root.rstrip('/')}/{relative}"

    def display_path(self, runtime_path: str) -> str:
        if self.runtime_mode != "ssh":
            return runtime_path
        root = self._normalized_remote_root().rstrip("/")
        value = runtime_path.replace("\\", "/")
        if value == root:
            return "."
        prefix = f"{root}/"
        if value.startswith(prefix):
            return value[len(prefix) :]
        return value

    def should_ignore_entry(self, relative_path: str) -> bool:
        return any(part in _IGNORED_DIRS for part in relative_path.replace("\\", "/").split("/"))

    def task_ref(self) -> str | None:
        return f"task:{self.active_task_id}" if self.active_task_id else None

    def session_ref(self) -> str | None:
        return f"session:{self.active_session_id}" if self.active_session_id else None

    def session_state_summary(self) -> str:
        if not self.active_session_id:
            return "none"
        details = [
            f"session:{self.active_session_id}",
            f"kind={self.active_session_kind or 'unknown'}",
            f"privilege={self.active_session_privilege}",
        ]
        if self.active_session_stateful:
            details.append("stateful=true")
        if self.active_session_reason:
            details.append(f"reason={self.active_session_reason}")
        return ", ".join(details)

    def mark_session_state(
        self,
        *,
        kind: Literal["pty", "tmux", "unknown"] | None = None,
        privilege: Literal["unknown", "user", "root"] | None = None,
        stateful: bool | None = None,
        reason: str | None = None,
    ) -> None:
        if kind is not None:
            self.active_session_kind = kind
        if privilege is not None:
            self.active_session_privilege = privilege
        if stateful is not None:
            self.active_session_stateful = stateful
        if reason:
            self.active_session_reason = reason

    def clear_session_state(self) -> None:
        self.active_session_id = None
        self.active_session_kind = None
        self.active_session_privilege = "unknown"
        self.active_session_stateful = False
        self.active_session_reason = None
        self.terminal_cursor = None
        self.terminal_input_pending = False
        self.last_terminal_input = None

    def _normalized_remote_root(self) -> str:
        root = (self.remote_root or ".").replace("\\", "/").strip()
        if not root:
            root = "."
        return root
