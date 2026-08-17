from __future__ import annotations

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import SSHRuntimeConfig
from mini_harness.permissions import PermissionDecision


def is_untrusted_host_key_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "host key" in text and ("not trusted" in text or "untrusted" in text)


async def approve_trust_host_once(
    approval_handler: object | None,
    *,
    tool_name: str,
    ssh: SSHRuntimeConfig,
    error: BaseException,
) -> bool:
    if approval_handler is None:
        return False
    approve = getattr(approval_handler, "approve", None)
    if approve is None:
        return False
    return await approve(
        ToolApprovalRequest(
            tool_name=tool_name,
            arguments={
                "hostname": ssh.hostname,
                "port": ssh.port,
                "username": ssh.username,
                "known_hosts_file": ssh.known_hosts_file,
            },
            decision=PermissionDecision.deny(
                "SSH host key is not trusted by the configured known_hosts file",
                approval_required=True,
                metadata={
                    "warning": (
                        "Only approve if you expected this SSH host and trust the network path. "
                        "This does not write to known_hosts; it only trusts the host key for the "
                        "next connection attempt in this Runtime process."
                    ),
                    "risks": [
                        "Approving the wrong host key can expose credentials or commands to an impersonating server.",
                        "This one-shot trust is not a persistent host-key verification policy.",
                    ],
                    "original_error": str(error),
                },
            ),
            permission_requests=[],
            preview_kind="ssh-host-key",
            preview_title="Untrusted SSH Host Key",
            preview_body=(
                f"Host: {ssh.username}@{ssh.hostname}:{ssh.port}\n"
                f"known_hosts: {ssh.known_hosts_file}\n"
                "Approve to retry once without writing the host key to known_hosts."
            ),
        )
    )
