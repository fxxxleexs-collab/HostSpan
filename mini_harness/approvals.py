from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mini_harness.permissions import PermissionDecision, PermissionRequest

if TYPE_CHECKING:
    from mini_harness.config import RuntimeConfig


@dataclass(frozen=True)
class ToolApprovalRequest:
    tool_name: str
    arguments: dict[str, Any]
    decision: PermissionDecision
    permission_requests: list[PermissionRequest]
    preview_kind: str | None = None
    preview_title: str | None = None
    preview_body: str | None = None

    def lines(self) -> list[str]:
        lines = [
            f"Tool: {self.tool_name}",
            f"Reason: {self.decision.reason}",
        ]
        warning = self.decision.metadata.get("warning")
        if warning:
            lines.append(f"Warning: {warning}")
        risks = self.decision.metadata.get("risks")
        if isinstance(risks, list) and risks:
            lines.append("Risks:")
            lines.extend(f"  - {risk}" for risk in risks)
        if self.decision.missing_capabilities:
            lines.append("Capabilities: " + ", ".join(self.decision.missing_capabilities))
        for request in self.permission_requests:
            detail = f"- {request.capability_key}"
            if request.operation:
                detail += f" operation={request.operation}"
            if request.resource:
                detail += f" resource={request.resource}"
            if request.argv:
                detail += f" argv={' '.join(request.argv)}"
            request_warning = request.metadata.get("warning")
            if request_warning:
                detail += f" warning={request_warning}"
            lines.append(detail)
        if self.preview_body:
            title = self.preview_title or "Preview"
            kind = f" ({self.preview_kind})" if self.preview_kind else ""
            lines.append(f"{title}{kind}:")
            lines.extend(f"  {line}" for line in self.preview_body.splitlines())
        return lines


class ApprovalHandler(Protocol):
    async def approve(self, request: ToolApprovalRequest) -> bool: ...

    async def prompt_secret(self, prompt: str) -> str | None: ...

    async def prompt_ssh_connection(
        self,
        *,
        reason: str,
        default_name: str,
    ) -> RuntimeConfig | None: ...
