from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from mini_harness.errors import ErrorCode, MiniHarnessError

SandboxProfile = Literal["off", "workspace", "strict"]
SandboxEngineName = Literal["off", "policy-only", "auto", "bubblewrap", "container"]
SandboxTarget = Literal["local", "remote"]
NetworkMode = Literal["inherit", "disabled"]

DEFAULT_DENY_PATTERNS = [
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_*",
    ".ssh/**",
    "**/.ssh/**",
]


class SandboxTargetConfig(BaseModel):
    root: str | None = None
    engine: SandboxEngineName | None = None
    require_engine: bool = False
    network: NetworkMode = "inherit"
    allow_root_shell: bool = False
    allow_system_paths: bool = False
    allow_package_install: bool = False

    @field_validator("root")
    @classmethod
    def _valid_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/").strip()
        if not normalized:
            raise ValueError("sandbox root cannot be empty")
        if ".." in [part for part in normalized.split("/") if part]:
            raise ValueError("sandbox root cannot contain parent traversal")
        return normalized


class SandboxPathConfig(BaseModel):
    allow: list[str] = Field(default_factory=lambda: ["**"])
    deny: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_PATTERNS))
    follow_symlinks: bool = False


class SandboxConfig(BaseModel):
    profile: SandboxProfile = "workspace"
    engine: SandboxEngineName = "policy-only"
    local: SandboxTargetConfig = Field(default_factory=SandboxTargetConfig)
    remote: SandboxTargetConfig = Field(default_factory=SandboxTargetConfig)
    paths: SandboxPathConfig = Field(default_factory=SandboxPathConfig)


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    target: SandboxTarget
    relative_path: str
    runtime_path: str
    root: str


@dataclass(frozen=True)
class CommandCheckResult:
    allowed: bool
    reason: str
    tags: tuple[str, ...] = ()

    @classmethod
    def allow(cls, reason: str = "command allowed") -> CommandCheckResult:
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(cls, reason: str, *tags: str) -> CommandCheckResult:
        return cls(allowed=False, reason=reason, tags=tuple(tags))


@dataclass(frozen=True)
class SandboxedCommand:
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    engine: SandboxEngineName = "policy-only"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxProbeResult:
    available: bool
    engine: SandboxEngineName
    detail: str


class SandboxEngine(Protocol):
    name: SandboxEngineName

    def probe(self, target: SandboxTarget) -> SandboxProbeResult: ...

    def wrap_task(
        self,
        *,
        argv: list[str],
        cwd: str,
        target: SandboxTarget,
        policy: WorkspacePolicy,
    ) -> SandboxedCommand: ...

    def wrap_terminal(
        self,
        *,
        argv: list[str],
        cwd: str,
        target: SandboxTarget,
        policy: WorkspacePolicy,
    ) -> SandboxedCommand: ...


class PolicyOnlySandboxEngine:
    name: SandboxEngineName = "policy-only"

    def probe(self, target: SandboxTarget) -> SandboxProbeResult:
        return SandboxProbeResult(
            available=True,
            engine=self.name,
            detail=f"policy-only sandbox active for {target}",
        )

    def wrap_task(
        self,
        *,
        argv: list[str],
        cwd: str,
        target: SandboxTarget,
        policy: WorkspacePolicy,
    ) -> SandboxedCommand:
        policy.authorize_command(argv, target=target)
        return SandboxedCommand(argv=argv, cwd=cwd, engine=self.name)

    def wrap_terminal(
        self,
        *,
        argv: list[str],
        cwd: str,
        target: SandboxTarget,
        policy: WorkspacePolicy,
    ) -> SandboxedCommand:
        policy.authorize_command(argv, target=target)
        return SandboxedCommand(argv=argv, cwd=cwd, engine=self.name)


class WorkspacePolicy:
    def __init__(
        self,
        *,
        local_root: str,
        remote_root: str | None,
        config: SandboxConfig | None = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self.local_root = self._target_root("local", local_root)
        self.remote_root = self._target_root("remote", remote_root or ".")
        self.engine: SandboxEngine = PolicyOnlySandboxEngine()

    @property
    def profile(self) -> SandboxProfile:
        return self.config.profile

    def normalize_relative_path(self, path: str) -> str:
        if self.profile == "off":
            return _normalize_relative_shape(path)
        relative = _normalize_relative_shape(path)
        self._authorize_patterns(relative)
        return relative

    def runtime_path(self, path: str, *, target: SandboxTarget) -> ResolvedWorkspacePath:
        relative = self.normalize_relative_path(path)
        root = self.root_for(target)
        if target == "remote":
            runtime_path = root if relative == "." else f"{root.rstrip('/')}/{relative}"
            return ResolvedWorkspacePath(target, relative, runtime_path, root)
        runtime_path = root if relative == "." else str(PurePosixPath(root) / relative)
        return ResolvedWorkspacePath(target, relative, runtime_path, root)

    def runtime_cwd(self, cwd: str | None, *, target: SandboxTarget) -> ResolvedWorkspacePath:
        return self.runtime_path(cwd or ".", target=target)

    def display_path(self, runtime_path: str, *, target: SandboxTarget) -> str:
        if target != "remote":
            return runtime_path
        root = self.root_for("remote").rstrip("/")
        value = runtime_path.replace("\\", "/")
        if value == root:
            return "."
        prefix = f"{root}/"
        if value.startswith(prefix):
            return value[len(prefix) :]
        return value

    def authorize_command(self, argv: list[str], *, target: SandboxTarget) -> CommandCheckResult:
        if self.profile == "off":
            return CommandCheckResult.allow("sandbox profile is off")
        command = " ".join(argv)
        target_config = self._target_config(target)
        if _contains_dangerous_command(command):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                "sandbox denied command because it appears destructive",
                recoverable=True,
            )
        if not target_config.allow_system_paths and _contains_system_path(command):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                "sandbox denied command because it references a system path",
                recoverable=True,
            )
        if not target_config.allow_root_shell and _opens_root_shell(command):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                "sandbox denied command because root shell escalation is disabled",
                recoverable=True,
            )
        if not target_config.allow_package_install and _installs_packages(command):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                "sandbox denied command because package installation is disabled",
                recoverable=True,
            )
        if (self.profile == "strict" or target_config.network == "disabled") and _uses_network_tool(
            command
        ):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                "sandbox denied command because network tools are disabled",
                recoverable=True,
            )
        return CommandCheckResult.allow()

    def sandbox_task(self, argv: list[str], cwd: str, *, target: SandboxTarget) -> SandboxedCommand:
        return self.engine.wrap_task(argv=argv, cwd=cwd, target=target, policy=self)

    def sandbox_terminal(
        self, argv: list[str], cwd: str, *, target: SandboxTarget
    ) -> SandboxedCommand:
        return self.engine.wrap_terminal(argv=argv, cwd=cwd, target=target, policy=self)

    def root_for(self, target: SandboxTarget) -> str:
        return self.local_root if target == "local" else self.remote_root

    def _target_root(self, target: SandboxTarget, fallback: str) -> str:
        configured = self._target_config(target).root
        return (configured or fallback).replace("\\", "/").rstrip("/") or "."

    def _target_config(self, target: SandboxTarget) -> SandboxTargetConfig:
        return self.config.local if target == "local" else self.config.remote

    def _authorize_patterns(self, relative: str) -> None:
        if self.config.paths.deny and _matches_any(relative, self.config.paths.deny):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                f"sandbox denied path by deny pattern: {relative}",
                recoverable=True,
            )
        if self.config.paths.allow and not _matches_any(relative, self.config.paths.allow):
            raise MiniHarnessError(
                ErrorCode.SANDBOX_DENIED,
                f"sandbox denied path because it is not allowed: {relative}",
                recoverable=True,
            )


def _normalize_relative_shape(path: str) -> str:
    if not path:
        path = "."
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise MiniHarnessError(
            ErrorCode.PATH_OUTSIDE_PROJECT,
            "absolute paths are not allowed",
            recoverable=True,
        )
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
    return "/".join(parts) or "."


def _matches_any(path: str, patterns: list[str]) -> bool:
    candidates = [path]
    if path != ".":
        candidates.append(f"./{path}")
    return any(fnmatchcase(candidate, pattern) for pattern in patterns for candidate in candidates)


def _contains_dangerous_command(command: str) -> bool:
    lowered = command.lower()
    patterns = [
        r"\brm\s+-[^\n;]*r[^\n;]*f\s+/",
        r"\bchmod\s+-r\s+/",
        r"\bchown\s+-r\s+/",
        r"\bmkfs(\.|\s)",
        r"\bdd\s+.*\bof=",
        r"\bshutdown\b",
        r"\breboot\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _contains_system_path(command: str) -> bool:
    normalized = command.replace("\\", "/")
    system_patterns = [
        r"(^|\s)/(etc|root|boot|dev|proc|sys|var/lib|usr/bin|usr/sbin)(/|\s|$)",
        r"(^|\s)[A-Za-z]:/Users(/|\s|$)",
        r"(^|\s)[A-Za-z]:/Windows(/|\s|$)",
    ]
    return any(re.search(pattern, normalized) for pattern in system_patterns)


def _opens_root_shell(command: str) -> bool:
    lowered = command.lower()
    patterns = [
        r"\bsudo\s+(-i|-s)\b",
        r"\bsudo\s+su\b",
        r"(^|[;&|]\s*)su\s+-?\b",
        r"\bdoas\s+-s\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _installs_packages(command: str) -> bool:
    lowered = command.lower()
    patterns = [
        r"\bapt(-get)?\s+(install|reinstall|download|source|build-dep|update|upgrade|dist-upgrade|full-upgrade)\b",
        r"\bdpkg\s+(-i|--install|--unpack)\b",
        r"\byum\s+(install|reinstall|update|upgrade|download|downloader)\b",
        r"\bdnf\s+(install|reinstall|update|upgrade|download|repoquery)\b",
        r"\bapk\s+(add|fetch|update|upgrade)\b",
        r"\bpacman\s+(-s|-sy|-syu|-sw|-u)\b",
        r"\bbrew\s+(install|reinstall|upgrade|fetch)\b",
        r"\bpip(x)?\s+(install|download|wheel)\b",
        r"\bpython\s+-m\s+pip\s+(install|download|wheel)\b",
        r"\bnpm\s+(install|i|ci|pack)\b",
        r"\bpnpm\s+(install|i|add|pack)\b",
        r"\byarn\s+(install|add|pack)\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _uses_network_tool(command: str) -> bool:
    lowered = command.lower()
    patterns = [
        r"\bcurl\b",
        r"\bwget\b",
        r"\bgit\s+clone\b",
        r"\bpip\s+install\b",
        r"\bnpm\s+install\b",
        r"\bpnpm\s+install\b",
        r"\byarn\s+add\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)
