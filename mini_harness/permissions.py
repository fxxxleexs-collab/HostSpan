from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from mini_harness.runtime.work_context import ResolvedTerminalTarget

PermissionTarget = Literal["local", "remote", "any"]


class PermissionsConfig(BaseModel):
    allow: list[str] = Field(default_factory=lambda: ["*"])
    deny: list[str] = Field(default_factory=list)
    approve_sandbox_denials: bool = True
    approve_terminal_open: bool = True
    approve_root_escalation: bool = True

    @field_validator("allow", "deny")
    @classmethod
    def _valid_capability_patterns(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("permission capability patterns cannot be empty")
            normalized.append(item)
        return normalized

    def build_policy(self) -> PermissionPolicy:
        return build_permission_policy(self)


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    capability: str
    target: PermissionTarget = "any"
    operation: str | None = None
    resource: str | None = None
    argv: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_target(
        cls,
        *,
        tool_name: str,
        capability: str,
        target: ResolvedTerminalTarget | PermissionTarget = "any",
        operation: str | None = None,
        resource: str | None = None,
        argv: list[str] | tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> PermissionRequest:
        return cls(
            tool_name=tool_name,
            capability=capability,
            target=target,
            operation=operation,
            resource=resource,
            argv=tuple(argv),
            metadata=metadata or {},
        )

    @property
    def capability_key(self) -> str:
        return f"{self.capability}:{self.target}"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str
    missing_capabilities: tuple[str, ...] = ()
    approval_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "allowed") -> PermissionDecision:
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        missing_capabilities: list[str] | tuple[str, ...] = (),
        approval_required: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        return cls(
            allowed=False,
            reason=reason,
            missing_capabilities=tuple(missing_capabilities),
            approval_required=approval_required,
            metadata=metadata or {},
        )


class PermissionPolicy(Protocol):
    def authorize_many(self, requests: list[PermissionRequest]) -> PermissionDecision: ...


class AllowAllPermissionPolicy:
    def authorize_many(self, requests: list[PermissionRequest]) -> PermissionDecision:
        _ = requests
        return PermissionDecision.allow("default allow policy")


class CapabilitySetPermissionPolicy:
    def __init__(self, allowed: set[str] | None = None, denied: set[str] | None = None) -> None:
        self.allowed = allowed or {"*"}
        self.denied = denied or set()

    def authorize_many(self, requests: list[PermissionRequest]) -> PermissionDecision:
        missing: list[str] = []
        denied: list[str] = []
        for request in requests:
            keys = _capability_keys(request)
            if any(_matches(pattern, key) for pattern in self.denied for key in keys):
                denied.append(request.capability_key)
                continue
            if not any(_matches(pattern, key) for pattern in self.allowed for key in keys):
                missing.append(request.capability_key)

        if denied:
            return PermissionDecision.deny(
                f"permission denied by policy: {', '.join(denied)}",
                missing_capabilities=denied,
                metadata={"denied_capabilities": denied},
            )
        if missing:
            return PermissionDecision.deny(
                f"missing required capabilities: {', '.join(missing)}",
                missing_capabilities=missing,
            )
        return PermissionDecision.allow("capabilities granted")


def _capability_keys(request: PermissionRequest) -> tuple[str, ...]:
    return (
        request.capability_key,
        f"{request.capability}:any",
        request.capability,
    )


def _matches(pattern: str, key: str) -> bool:
    return pattern == "*" or fnmatchcase(key, pattern)


def build_permission_policy(config: PermissionsConfig | None = None) -> PermissionPolicy:
    if config is None:
        return AllowAllPermissionPolicy()
    return CapabilitySetPermissionPolicy(allowed=set(config.allow), denied=set(config.deny))
