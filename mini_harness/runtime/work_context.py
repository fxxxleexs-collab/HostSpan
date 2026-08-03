from __future__ import annotations

import os
import platform
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


TerminalTarget = Literal["current", "local", "remote"]
ResolvedTerminalTarget = Literal["local", "remote"]


@dataclass(frozen=True)
class TargetBinding:
    location: ResolvedTerminalTarget
    endpoint_id: str
    environment_id: str
    target_id: str
    root: str
    os_name: str
    shell: str


@dataclass
class WorkContext:
    endpoint_id: str
    environment_id: str
    target_id: str
    project_root: str
    runtime_mode: Literal["local", "ssh"] = "local"
    remote_root: str | None = None
    local_endpoint_id: str | None = None
    local_environment_id: str | None = None
    local_target_id: str | None = None
    local_os: str = "unknown"
    local_shell: str = "unknown"
    remote_os: str = "unknown"
    remote_shell: str = "bash"
    cwd: str = "."
    active_task_id: str | None = None
    task_log_cursor: int = 0
    active_session_id: str | None = None
    active_session_target: ResolvedTerminalTarget | None = None
    active_session_os: str = "unknown"
    active_session_shell: str = "unknown"
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

    def __post_init__(self) -> None:
        if self.local_os == "unknown":
            self.local_os = local_os_name()
        if self.local_shell == "unknown":
            self.local_shell = default_local_shell()
        if self.runtime_mode == "ssh" and self.remote_shell == "unknown":
            self.remote_shell = "bash"

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
        return self.runtime_cwd_for(cwd, self.default_terminal_target())

    def runtime_cwd_for(self, cwd: str | None, target: TerminalTarget) -> str:
        resolved_target = self.resolve_terminal_target(target)
        relative = self.normalize_cwd(cwd)
        if resolved_target == "remote":
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
            f"target={self.active_session_target or 'unknown'}",
            f"os={self.active_session_os}",
            f"shell={self.active_session_shell}",
            f"kind={self.active_session_kind or 'unknown'}",
            f"privilege={self.active_session_privilege}",
        ]
        if self.active_session_stateful:
            details.append("stateful=true")
        if self.active_session_reason:
            details.append(f"reason={self.active_session_reason}")
        return ", ".join(details)

    def target_summary(self) -> str:
        local = self.local_target()
        lines = [
            (
                "Local target: "
                f"{'available' if local else 'unavailable'}, "
                f"os={self.local_os}, shell={self.local_shell}, root={self.project_root}"
            )
        ]
        remote = self.remote_target()
        lines.append(
            "Remote target: "
            f"{'available' if remote else 'unavailable'}, "
            f"os={self.remote_os}, shell={self.remote_shell}, "
            f"root={self.remote_root or 'n/a'}"
        )
        lines.append(f"Default command target: {self.default_terminal_target()}")
        return "\n".join(lines)

    def default_terminal_target(self) -> ResolvedTerminalTarget:
        return "remote" if self.runtime_mode == "ssh" else "local"

    def resolve_terminal_target(self, target: TerminalTarget) -> ResolvedTerminalTarget:
        if target == "current":
            return self.default_terminal_target()
        return target

    def terminal_target(self, target: TerminalTarget) -> TargetBinding:
        resolved = self.resolve_terminal_target(target)
        binding = self.local_target() if resolved == "local" else self.remote_target()
        if binding is None:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                f"{resolved} terminal target is not available in this runtime context",
                recoverable=True,
            )
        return binding

    def local_target(self) -> TargetBinding | None:
        if self.local_endpoint_id and self.local_environment_id and self.local_target_id:
            return TargetBinding(
                location="local",
                endpoint_id=self.local_endpoint_id,
                environment_id=self.local_environment_id,
                target_id=self.local_target_id,
                root=self.project_root,
                os_name=self.local_os,
                shell=self.local_shell,
            )
        if self.runtime_mode == "local":
            return TargetBinding(
                location="local",
                endpoint_id=self.endpoint_id,
                environment_id=self.environment_id,
                target_id=self.target_id,
                root=self.project_root,
                os_name=self.local_os,
                shell=self.local_shell,
            )
        return None

    def remote_target(self) -> TargetBinding | None:
        if self.runtime_mode != "ssh":
            return None
        return TargetBinding(
            location="remote",
            endpoint_id=self.endpoint_id,
            environment_id=self.environment_id,
            target_id=self.target_id,
            root=self._normalized_remote_root(),
            os_name=self.remote_os,
            shell=self.remote_shell,
        )

    def mark_session_state(
        self,
        *,
        target: ResolvedTerminalTarget | None = None,
        os_name: str | None = None,
        shell: str | None = None,
        kind: Literal["pty", "tmux", "unknown"] | None = None,
        privilege: Literal["unknown", "user", "root"] | None = None,
        stateful: bool | None = None,
        reason: str | None = None,
    ) -> None:
        if target is not None:
            self.active_session_target = target
        if os_name is not None:
            self.active_session_os = os_name
        if shell is not None:
            self.active_session_shell = shell
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
        self.active_session_target = None
        self.active_session_os = "unknown"
        self.active_session_shell = "unknown"
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


def local_os_name() -> str:
    system = platform.system().lower()
    if system.startswith("windows"):
        return "windows"
    if system.startswith("darwin"):
        return "macos"
    if system.startswith("linux"):
        return "linux"
    return system or "unknown"


def default_local_shell() -> str:
    if os.name == "nt":
        shell = os.environ.get("PSMODULEPATH")
        return "powershell" if shell else "cmd"
    return Path(os.environ.get("SHELL", "")).name or "sh"
