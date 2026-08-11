from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mini_harness.permissions import PermissionDecision, PermissionRequest


@dataclass(frozen=True)
class ToolApprovalRequest:
    tool_name: str
    arguments: dict[str, Any]
    decision: PermissionDecision
    permission_requests: list[PermissionRequest]

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
        return lines


class ApprovalHandler(Protocol):
    async def approve(self, request: ToolApprovalRequest) -> bool: ...

    async def prompt_secret(self, prompt: str) -> str | None: ...
