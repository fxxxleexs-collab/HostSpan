from __future__ import annotations

import os
import platform
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mini_harness.diffing import TextSnapshot
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.sync.config import SyncConfig
from mini_harness.workspace import SandboxConfig, SandboxedCommand, WorkspacePolicy

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
class SessionBrief:
    session_id: str
    target: str = "unknown"
    backend: str = "unknown"
    runtime_state: str = "unknown"
    brief: str = "terminal session"
    last_command: str | None = None
    cwd_hint: str | None = None
    privilege: Literal["unknown", "user", "root"] = "unknown"
    pending: bool = False
    history_only: bool = False
    updated_by: str = "unknown"
    touched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    touch_index: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "target": self.target,
            "backend": self.backend,
            "runtime_state": self.runtime_state,
            "brief": self.brief,
            "last_command": self.last_command,
            "cwd_hint": self.cwd_hint,
            "privilege": self.privilege,
            "pending": self.pending,
            "history_only": self.history_only,
            "updated_by": self.updated_by,
            "touched_at": self.touched_at,
            "touch_index": self.touch_index,
        }


@dataclass
class FileSnapshotSummary:
    path: str
    sha256: str
    size: int
    line_count: int
    newline: str
    encoding: str
    read_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "line_count": self.line_count,
            "newline": self.newline,
            "encoding": self.encoding,
            "read_at": self.read_at,
        }


@dataclass
class TaskBrief:
    task_id: str
    argv: list[str] = field(default_factory=list)
    cwd: str = "."
    state: str = "UNKNOWN"
    pid: int | None = None
    persistent: bool = False
    brief: str | None = None
    log_tail: str | None = None
    exit_code: int | None = None
    started_by: str = "unknown"
    touched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    touch_index: int = 0

    @property
    def active(self) -> bool:
        return self.state not in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "state": self.state,
            "pid": self.pid,
            "persistent": self.persistent,
            "brief": self.brief,
            "log_tail": self.log_tail,
            "exit_code": self.exit_code,
            "started_by": self.started_by,
            "active": self.active,
            "touched_at": self.touched_at,
            "touch_index": self.touch_index,
        }


@dataclass
class RemoteToolStatus:
    tool: str
    status: Literal["present", "missing", "unknown"] = "unknown"
    version: str | None = None
    reason: str | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    touch_index: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "status": self.status,
            "version": self.version,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "touch_index": self.touch_index,
        }


@dataclass
class RemoteEnvironmentInfo:
    status: Literal["ok", "unknown"] = "unknown"
    os_name: str = "unknown"
    arch: str = "unknown"
    shell: str = "unknown"
    sh_path: str | None = None
    bash_path: str | None = None
    python3_path: str | None = None
    python_path: str | None = None
    python3_version: str | None = None
    python_version: str | None = None
    nohup_path: str | None = None
    tmux_path: str | None = None
    tmux_version: str | None = None
    sudo_path: str | None = None
    reason: str | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "os_name": self.os_name,
            "arch": self.arch,
            "shell": self.shell,
            "sh_path": self.sh_path,
            "bash_path": self.bash_path,
            "python3_path": self.python3_path,
            "python_path": self.python_path,
            "python3_version": self.python3_version,
            "python_version": self.python_version,
            "nohup_path": self.nohup_path,
            "tmux_path": self.tmux_path,
            "tmux_version": self.tmux_version,
            "sudo_path": self.sudo_path,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


@dataclass
class RuntimeTransition:
    kind: Literal["task", "terminal", "remote", "sync", "file"]
    action: str
    ref: str | None
    summary: str
    state: str | None = None
    active_after: str | None = None
    touched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    index: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "action": self.action,
            "ref": self.ref,
            "summary": self.summary,
            "state": self.state,
            "active_after": self.active_after,
            "touched_at": self.touched_at,
            "index": self.index,
        }


@dataclass
class WorkContext:
    endpoint_id: str
    environment_id: str
    target_id: str
    project_root: str
    runtime_mode: Literal["local", "ssh"] = "local"
    runtime_name: str = "mini-harness"
    remote_root: str | None = None
    remote_hostname: str | None = None
    remote_username: str | None = None
    remote_port: int | None = None
    remote_auth_method: str | None = None
    local_endpoint_id: str | None = None
    local_environment_id: str | None = None
    local_target_id: str | None = None
    local_os: str = "unknown"
    local_shell: str = "unknown"
    remote_os: str = "unknown"
    remote_shell: str = "bash"
    sandbox_config: SandboxConfig | None = None
    workspace_policy: WorkspacePolicy | None = None
    sync_config: SyncConfig | None = None
    approval_handler: object | None = None
    cwd: str = "."
    active_task_id: str | None = None
    task_log_cursor: int = 0
    active_session_id: str | None = None
    active_session_target: ResolvedTerminalTarget | None = None
    active_session_os: str = "unknown"
    active_session_shell: str = "unknown"
    active_session_kind: Literal["pty", "tmux", "unknown"] | None = None
    active_session_runtime_state: str = "unknown"
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
    file_snapshots: dict[str, FileSnapshotSummary] = field(default_factory=dict)
    session_briefs: dict[str, SessionBrief] = field(default_factory=dict)
    task_briefs: dict[str, TaskBrief] = field(default_factory=dict)
    remote_environment: RemoteEnvironmentInfo = field(default_factory=RemoteEnvironmentInfo)
    remote_tool_statuses: dict[str, RemoteToolStatus] = field(default_factory=dict)
    runtime_transitions: list[RuntimeTransition] = field(default_factory=list)
    _session_touch_counter: int = 0
    _task_touch_counter: int = 0
    _remote_tool_touch_counter: int = 0
    _runtime_transition_counter: int = 0
    _sandbox_approval_depth: int = 0

    def __post_init__(self) -> None:
        if self.local_os == "unknown":
            self.local_os = local_os_name()
        if self.local_shell == "unknown":
            self.local_shell = default_local_shell()
        if self.runtime_mode == "ssh" and self.remote_shell == "unknown":
            self.remote_shell = "bash"
        if self.workspace_policy is None:
            self.workspace_policy = WorkspacePolicy(
                local_root=self.project_root,
                remote_root=self.remote_root,
                config=self.sandbox_config,
            )

    def normalize_path(self, path: str) -> str:
        if self.sandbox_approval_active():
            return _normalize_approved_workspace_path(path)
        return self._workspace_policy().normalize_relative_path(path)

    def normalize_cwd(self, cwd: str | None) -> str:
        return self.normalize_path(cwd or self.cwd or ".")

    def runtime_cwd(self, cwd: str | None) -> str:
        return self.runtime_cwd_for(cwd, self.default_terminal_target())

    def runtime_cwd_for(self, cwd: str | None, target: TerminalTarget) -> str:
        resolved_target = self.resolve_terminal_target(target)
        if self.sandbox_approval_active():
            approved = _normalize_approved_workspace_path(cwd or self.cwd or ".")
            if _is_absolute_path(approved):
                if resolved_target == "remote":
                    return approved
                return str(Path(approved).resolve())
            if resolved_target == "remote":
                return self._runtime_relative_path(approved, target="remote")
            return self._local_runtime_cwd(approved)
        relative = self.normalize_cwd(cwd)
        if resolved_target == "remote":
            return self._workspace_policy().runtime_cwd(relative, target="remote").runtime_path
        return self._local_runtime_cwd(relative)

    def runtime_path(self, path: str) -> str:
        return self.runtime_path_for(path, self.default_terminal_target())

    def runtime_path_for(self, path: str, target: TerminalTarget) -> str:
        resolved_target = self.resolve_terminal_target(target)
        if self.sandbox_approval_active():
            approved = _normalize_approved_workspace_path(path)
            if _is_absolute_path(approved):
                return approved
            if resolved_target == "remote":
                return self._runtime_relative_path(approved, target="remote")
            return str((Path(self.project_root).resolve() / approved).resolve())
        relative = self.normalize_path(path)
        if resolved_target == "local":
            return relative
        return self._workspace_policy().runtime_path(relative, target=resolved_target).runtime_path

    def sandbox_task(
        self,
        argv: list[str],
        cwd: str,
        target: TerminalTarget = "current",
    ) -> SandboxedCommand:
        resolved_target = self.resolve_terminal_target(target)
        if self.sandbox_approval_active():
            return self._approved_sandbox_command(argv, cwd)
        return self._workspace_policy().sandbox_task(argv, cwd, target=resolved_target)

    def sandbox_terminal(
        self,
        argv: list[str],
        cwd: str,
        target: TerminalTarget,
    ) -> SandboxedCommand:
        resolved_target = self.resolve_terminal_target(target)
        if self.sandbox_approval_active():
            return self._approved_sandbox_command(argv, cwd)
        return self._workspace_policy().sandbox_terminal(argv, cwd, target=resolved_target)

    def authorize_session_command(self, command: str) -> None:
        if self.sandbox_approval_active():
            return
        target = self.active_session_target or self.default_terminal_target()
        self._workspace_policy().authorize_command([command], target=target)

    @contextmanager
    def approved_sandbox(self) -> Iterator[None]:
        self._sandbox_approval_depth += 1
        try:
            yield
        finally:
            self._sandbox_approval_depth -= 1

    def sandbox_approval_active(self) -> bool:
        return self._sandbox_approval_depth > 0

    def display_path(self, runtime_path: str) -> str:
        if self.runtime_mode != "ssh":
            return runtime_path
        return self._workspace_policy().display_path(runtime_path, target="remote")

    def should_ignore_entry(self, relative_path: str) -> bool:
        return any(part in _IGNORED_DIRS for part in relative_path.replace("\\", "/").split("/"))

    def task_ref(self) -> str | None:
        return f"task:{self.active_task_id}" if self.active_task_id else None

    def active_task_brief(self) -> TaskBrief | None:
        if not self.active_task_id:
            return None
        return self.task_briefs.get(self.active_task_id)

    def active_task_is_tracked(self) -> bool:
        return self.active_task_brief() is not None

    def managed_task_inventory(self, *, active_only: bool = False) -> list[dict[str, object]]:
        tasks = sorted(
            self.task_briefs.values(),
            key=lambda item: item.touch_index,
            reverse=True,
        )
        if active_only:
            tasks = [task for task in tasks if task.active or task.task_id == self.active_task_id]
        return [task.as_dict() for task in tasks]

    def record_runtime_transition(
        self,
        *,
        kind: Literal["task", "terminal", "remote", "sync", "file"],
        action: str,
        ref: str | None = None,
        summary: str,
        state: str | None = None,
        active_after: str | None = None,
    ) -> RuntimeTransition:
        self._runtime_transition_counter += 1
        transition = RuntimeTransition(
            kind=kind,
            action=action,
            ref=ref,
            summary=_compact_session_text(summary, limit=300),
            state=state,
            active_after=active_after,
            index=self._runtime_transition_counter,
        )
        self.runtime_transitions.append(transition)
        self.runtime_transitions = self.runtime_transitions[-20:]
        return transition

    def recent_runtime_transitions(self, *, limit: int = 8) -> list[dict[str, object]]:
        return [
            transition.as_dict()
            for transition in sorted(
                self.runtime_transitions,
                key=lambda item: item.index,
                reverse=True,
            )[:limit]
        ]

    def session_ref(self) -> str | None:
        return f"session:{self.active_session_id}" if self.active_session_id else None

    def record_task_brief(
        self,
        task_id: str,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        state: str | None = None,
        pid: int | None = None,
        persistent: bool | None = None,
        brief: str | None = None,
        log_tail: str | None = None,
        exit_code: int | None = None,
        started_by: str | None = None,
    ) -> TaskBrief:
        self._task_touch_counter += 1
        item = self.task_briefs.get(task_id) or TaskBrief(task_id=task_id)
        if argv is not None:
            item.argv = list(argv)
        if cwd is not None:
            item.cwd = cwd
        if state is not None:
            item.state = state
        if pid is not None:
            item.pid = pid
        if persistent is not None:
            item.persistent = persistent
        if brief is not None:
            item.brief = _compact_session_text(brief, limit=240)
        if log_tail is not None:
            item.log_tail = log_tail[-4000:]
        if exit_code is not None:
            item.exit_code = exit_code
        if started_by is not None:
            item.started_by = started_by
        item.touched_at = datetime.now(UTC).isoformat()
        item.touch_index = self._task_touch_counter
        self.task_briefs[task_id] = item
        return item

    def session_state_summary(self) -> str:
        if not self.active_session_id:
            return "none"
        details = [
            f"session:{self.active_session_id}",
            f"target={self.active_session_target or 'unknown'}",
            f"os={self.active_session_os}",
            f"shell={self.active_session_shell}",
            f"kind={self.active_session_kind or 'unknown'}",
            f"runtime_state={self.active_session_runtime_state}",
            f"privilege={self.active_session_privilege}",
        ]
        if self.active_session_stateful:
            details.append("stateful=true")
        if self.active_session_reason:
            details.append(f"reason={self.active_session_reason}")
        return ", ".join(details)

    def record_session_interaction(
        self,
        session_id: str,
        *,
        target: str | None = None,
        backend: str | None = None,
        runtime_state: str | None = None,
        brief: str | None = None,
        last_command: str | None = None,
        cwd_hint: str | None = None,
        privilege: Literal["unknown", "user", "root"] | None = None,
        pending: bool | None = None,
        history_only: bool | None = None,
        updated_by: str,
    ) -> SessionBrief:
        self._session_touch_counter += 1
        existing = self.session_briefs.get(session_id)
        item = existing or SessionBrief(session_id=session_id)
        if target is not None:
            item.target = target
        if backend is not None:
            item.backend = backend
        if runtime_state is not None:
            item.runtime_state = runtime_state
        if brief is not None:
            item.brief = _compact_session_text(brief, limit=240)
        if last_command is not None:
            item.last_command = _compact_session_text(last_command, limit=240)
        if cwd_hint is not None:
            item.cwd_hint = _compact_session_text(cwd_hint, limit=180)
        if privilege is not None:
            item.privilege = privilege
        if pending is not None:
            item.pending = pending
        if history_only is not None:
            item.history_only = history_only
        item.updated_by = updated_by
        item.touched_at = datetime.now(UTC).isoformat()
        item.touch_index = self._session_touch_counter
        self.session_briefs[session_id] = item
        return item

    def session_brief(self, session_id: str) -> SessionBrief | None:
        return self.session_briefs.get(session_id)

    def record_file_snapshot(self, snapshot: TextSnapshot) -> FileSnapshotSummary:
        summary = FileSnapshotSummary(
            path=snapshot.path,
            sha256=snapshot.sha256,
            size=snapshot.size,
            line_count=snapshot.line_count,
            newline=snapshot.newline,
            encoding=snapshot.encoding,
        )
        self.file_snapshots[snapshot.path] = summary
        return summary

    def file_snapshot(self, path: str) -> FileSnapshotSummary | None:
        return self.file_snapshots.get(path)

    def record_remote_tool_status(
        self,
        tool: str,
        status: Literal["present", "missing", "unknown"],
        *,
        version: str | None = None,
        reason: str | None = None,
    ) -> RemoteToolStatus:
        self._remote_tool_touch_counter += 1
        item = self.remote_tool_statuses.get(tool) or RemoteToolStatus(tool=tool)
        item.status = status
        item.version = _compact_session_text(version, limit=120) if version else None
        item.reason = _compact_session_text(reason, limit=180) if reason else None
        item.checked_at = datetime.now(UTC).isoformat()
        item.touch_index = self._remote_tool_touch_counter
        self.remote_tool_statuses[tool] = item
        return item

    def record_remote_environment(
        self,
        *,
        status: Literal["ok", "unknown"],
        os_name: str | None = None,
        arch: str | None = None,
        shell: str | None = None,
        sh_path: str | None = None,
        bash_path: str | None = None,
        python3_path: str | None = None,
        python_path: str | None = None,
        python3_version: str | None = None,
        python_version: str | None = None,
        nohup_path: str | None = None,
        tmux_path: str | None = None,
        tmux_version: str | None = None,
        sudo_path: str | None = None,
        reason: str | None = None,
    ) -> RemoteEnvironmentInfo:
        info = RemoteEnvironmentInfo(
            status=status,
            os_name=_compact_session_text(os_name or "unknown", limit=80),
            arch=_compact_session_text(arch or "unknown", limit=80),
            shell=_compact_session_text(shell or "unknown", limit=120),
            sh_path=_optional_compact(sh_path, limit=160),
            bash_path=_optional_compact(bash_path, limit=160),
            python3_path=_optional_compact(python3_path, limit=160),
            python_path=_optional_compact(python_path, limit=160),
            python3_version=_optional_compact(python3_version, limit=120),
            python_version=_optional_compact(python_version, limit=120),
            nohup_path=_optional_compact(nohup_path, limit=160),
            tmux_path=_optional_compact(tmux_path, limit=160),
            tmux_version=_optional_compact(tmux_version, limit=120),
            sudo_path=_optional_compact(sudo_path, limit=160),
            reason=_optional_compact(reason, limit=220),
        )
        self.remote_environment = info
        if info.os_name != "unknown":
            self.remote_os = _normalize_remote_os_name(info.os_name)
        if info.bash_path:
            self.remote_shell = "bash"
        elif info.sh_path:
            self.remote_shell = "sh"
        elif info.shell != "unknown":
            self.remote_shell = Path(info.shell).name or info.shell
        return info

    def remote_environment_summary(self) -> str:
        if self.runtime_mode != "ssh" and self.remote_environment.status == "unknown":
            return "Remote environment: n/a"
        info = self.remote_environment
        if info.status == "unknown":
            reason = f" reason={info.reason}" if info.reason else ""
            return f"Remote environment: unknown{reason}"
        tools = [
            f"sh={'present' if info.sh_path else 'missing'}",
            f"bash={'present' if info.bash_path else 'missing'}",
            f"python3={'present' if info.python3_path else 'missing'}",
            f"python={'present' if info.python_path else 'missing'}",
            f"nohup={'present' if info.nohup_path else 'missing'}",
            f"sudo={'present' if info.sudo_path else 'missing'}",
        ]
        if info.tmux_path:
            tools.append("tmux=present")
        return (
            "Remote environment: "
            f"os={info.os_name}, arch={info.arch}, shell={info.shell}; "
            + ", ".join(tools)
        )

    def remote_tools_summary(self) -> str:
        if self.runtime_mode != "ssh" and not self.remote_tool_statuses:
            return "Remote tools: n/a"
        if not self.remote_tool_statuses:
            return "Remote tools: tmux=unknown reason=not probed"
        parts: list[str] = []
        for status in sorted(
            self.remote_tool_statuses.values(),
            key=lambda item: item.tool,
        ):
            text = f"{status.tool}={status.status}"
            if status.version:
                text += f" version={status.version}"
            if status.reason and status.status != "present":
                text += f" reason={status.reason}"
            parts.append(text)
        return "Remote tools: " + "; ".join(parts)

    def target_summary(self) -> str:
        local = self.local_target()
        remote_configured = self.remote_hostname is not None
        remote_connected = self.remote_target() is not None
        remote_address = self.remote_address_summary()
        lines = [
            (
                "Local target: "
                f"{'available' if local else 'unavailable'}, "
                f"os={self.local_os}, shell={self.local_shell}, root={self.project_root}"
            )
        ]
        lines.append(
            "Remote connection: "
            f"configured={str(remote_configured).lower()}, "
            f"connected={str(remote_connected).lower()}, "
            f"host={remote_address}, "
            f"auth={self.remote_auth_method or 'n/a'}"
        )
        remote = self.remote_target()
        lines.append(
            "Remote target: "
            f"{'available' if remote else 'unavailable'}, "
            f"os={self.remote_os}, shell={self.remote_shell}, "
            f"root={self.remote_root or 'n/a'}"
        )
        if remote_configured or remote_connected:
            lines.append(self.remote_environment_summary())
            lines.append(self.remote_tools_summary())
        lines.append(f"Default command target: {self.default_terminal_target()}")
        return "\n".join(lines)

    def remote_address_summary(self) -> str:
        if not self.remote_hostname:
            return "n/a"
        user_prefix = f"{self.remote_username}@" if self.remote_username else ""
        port_suffix = f":{self.remote_port}" if self.remote_port else ""
        return f"{user_prefix}{self.remote_hostname}{port_suffix}"

    def sandbox_summary(self) -> str:
        policy = self._workspace_policy()
        return (
            f"Sandbox: profile={policy.config.profile}, engine={policy.config.engine}, "
            f"local_root={policy.root_for('local')}, remote_root={policy.root_for('remote')}"
        )

    def sync_remote_root(self) -> str:
        return self._normalized_remote_root()

    def sync_summary(self) -> str:
        config = self.sync_config or SyncConfig()
        return (
            f"Sync: enabled={config.enabled}, mode={config.mode}, "
            f"local_state_dir={config.local_state_dir}, "
            f"remote_manifest_path={config.remote_manifest_path}"
        )

    def runtime_activity_summary(
        self,
        *,
        max_tasks: int = 5,
        max_sessions: int = 5,
        max_transitions: int = 8,
    ) -> str:
        lines = ["Runtime activity:"]
        transition_lines = self._runtime_transition_lines(max_transitions=max_transitions)
        task_lines = self._task_activity_lines(max_tasks=max_tasks)
        session_lines = self._session_activity_lines(max_sessions=max_sessions)
        if transition_lines:
            lines.append("Recent runtime transitions:")
            lines.extend(transition_lines)
        else:
            lines.append("Recent runtime transitions: none")
        if task_lines:
            lines.append("Managed tasks tracked for this conversation:")
            lines.extend(task_lines)
        else:
            lines.append("Managed tasks: none")
        if session_lines:
            lines.append("Terminal sessions:")
            lines.extend(session_lines)
        else:
            lines.append("Terminal sessions: none")
        return "\n".join(lines)

    def _runtime_transition_lines(self, *, max_transitions: int) -> list[str]:
        transitions = sorted(
            self.runtime_transitions,
            key=lambda item: item.index,
            reverse=True,
        )[:max_transitions]
        lines: list[str] = []
        for transition in transitions:
            parts = [
                f"- {transition.kind}.{transition.action}",
            ]
            if transition.ref:
                parts.append(f"ref={transition.ref}")
            if transition.state:
                parts.append(f"state={transition.state}")
            if transition.active_after is not None:
                parts.append(f"active_after={transition.active_after}")
            parts.append(f"summary={_compact_session_text(transition.summary, limit=220)}")
            lines.append(" | ".join(parts))
        return lines

    def _task_activity_lines(self, *, max_tasks: int) -> list[str]:
        candidates = [
            task
            for task in self.task_briefs.values()
            if task.active or task.persistent or task.task_id == self.active_task_id
        ]
        candidates.sort(key=lambda item: item.touch_index, reverse=True)
        lines: list[str] = []
        for task in candidates[:max_tasks]:
            command = _compact_session_text(" ".join(task.argv), limit=180) if task.argv else "n/a"
            parts = [
                f"- task:{task.task_id}",
                f"state={task.state}",
                f"pid={task.pid or 'unknown'}",
                f"persistent={str(task.persistent).lower()}",
                f"cwd={_compact_session_text(task.cwd, limit=120)}",
            ]
            if task.exit_code is not None:
                parts.append(f"exit={task.exit_code}")
            parts.append(f"cmd={command}")
            if task.brief:
                parts.append(f"brief={task.brief}")
            if task.log_tail:
                parts.append(f"tail={_compact_session_text(task.log_tail, limit=240)}")
            lines.append(" | ".join(parts))
        return lines

    def _session_activity_lines(self, *, max_sessions: int) -> list[str]:
        candidates = [
            session
            for session in self.session_briefs.values()
            if session.runtime_state.upper() == "ACTIVE"
            or session.pending
            or session.session_id == self.active_session_id
        ]
        candidates.sort(key=lambda item: item.touch_index, reverse=True)
        lines: list[str] = []
        for session in candidates[:max_sessions]:
            parts = [
                f"- session:{session.session_id}",
                f"target={session.target}",
                f"backend={session.backend}",
                f"state={session.runtime_state}",
                f"privilege={session.privilege}",
                f"pending={str(session.pending).lower()}",
            ]
            if session.cwd_hint:
                parts.append(f"cwd={session.cwd_hint}")
            if session.last_command:
                parts.append(f"last={session.last_command}")
            if session.brief:
                parts.append(f"brief={session.brief}")
            lines.append(" | ".join(parts))
        return lines

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
        runtime_state: str | None = None,
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
        if runtime_state is not None:
            self.active_session_runtime_state = runtime_state
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
        self.active_session_runtime_state = "unknown"
        self.active_session_privilege = "unknown"
        self.active_session_stateful = False
        self.active_session_reason = None
        self.terminal_cursor = None
        self.terminal_input_pending = False
        self.last_terminal_input = None

    def refresh_workspace_policy(self) -> None:
        self.workspace_policy = WorkspacePolicy(
            local_root=self.project_root,
            remote_root=self.remote_root,
            config=self.sandbox_config,
        )

    def _normalized_remote_root(self) -> str:
        root = self._workspace_policy().root_for("remote").replace("\\", "/").strip()
        if not root:
            root = "."
        return root

    def _workspace_policy(self) -> WorkspacePolicy:
        if self.workspace_policy is None:
            self.workspace_policy = WorkspacePolicy(
                local_root=self.project_root,
                remote_root=self.remote_root,
                config=self.sandbox_config,
            )
        return self.workspace_policy

    def _runtime_relative_path(self, relative: str, *, target: ResolvedTerminalTarget) -> str:
        root = self._workspace_policy().root_for(target)
        if target == "remote":
            return root if relative == "." else f"{root.rstrip('/')}/{relative}"
        return relative

    def _local_runtime_cwd(self, relative: str) -> str:
        root = Path(self.project_root).resolve()
        resolved = root if relative == "." else (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise MiniHarnessError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "cwd must stay inside the project root",
                recoverable=True,
            )
        return str(resolved)

    def _approved_sandbox_command(self, argv: list[str], cwd: str) -> SandboxedCommand:
        return SandboxedCommand(
            argv=argv,
            cwd=cwd,
            engine=self._workspace_policy().config.engine,
            metadata={"sandbox_override": "approved_by_user"},
        )


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


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|secret|[A-Z0-9_]*api[_-]?key)\s*[:=]\s*([^\s;&|]+)"
)
_AUTH_BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+([^\s;&|]+)")
_KNOWN_TOKEN_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"sk-ant-[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{16,}"
    r")\b"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _compact_session_text(text: str, *, limit: int) -> str:
    value = text.replace("\r", "\n")
    value = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _AUTH_BEARER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", value)
    value = _KNOWN_TOKEN_RE.sub("[REDACTED]", value)
    value = _PEM_PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", value)
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 14)].rstrip() + " ...[truncated]"


def _optional_compact(text: str | None, *, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return _compact_session_text(text, limit=limit)


def _normalize_remote_os_name(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("linux"):
        return "linux"
    if lowered.startswith("darwin"):
        return "macos"
    if lowered.startswith(("freebsd", "openbsd", "netbsd")):
        return lowered.split()[0]
    return lowered or "unknown"


def _normalize_approved_workspace_path(path: str | None) -> str:
    if not path:
        path = "."
    normalized = path.replace("\\", "/").strip()
    absolute = _is_absolute_path(normalized)
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise MiniHarnessError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "paths must stay inside the project root",
                recoverable=True,
            )
        parts.append(part)
    if absolute:
        if len(normalized) > 1 and normalized[1] == ":":
            return f"{normalized[:2]}/{'/'.join(parts[1:])}".rstrip("/")
        return "/" + "/".join(parts)
    return "/".join(parts) or "."


def _is_absolute_path(path: str) -> bool:
    return path.startswith("/") or (len(path) > 1 and path[1] == ":")
