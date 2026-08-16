from __future__ import annotations

import pytest

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.permissions import CapabilitySetPermissionPolicy
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.config import SyncConfig
from mini_harness.tools.registry import ToolRegistry
from mini_harness.tools.runtime.builder import build_internal_runtime_tools as build_runtime_tools


def _registry(policy: CapabilitySetPermissionPolicy) -> ToolRegistry:
    registry = ToolRegistry(permission_policy=policy)
    return registry


class FakeApprovalHandler:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests: list[ToolApprovalRequest] = []

    async def approve(self, request: ToolApprovalRequest) -> bool:
        self.requests.append(request)
        return self.approved


def _registry_with_approval(
    policy: CapabilitySetPermissionPolicy,
    approval_handler: FakeApprovalHandler,
) -> ToolRegistry:
    return ToolRegistry(permission_policy=policy, approval_handler=approval_handler)


def _register_runtime_tools(registry: ToolRegistry, fake_runtime) -> ToolRegistry:
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    return registry


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
        local_os="linux",
        local_shell="bash",
    )


def _remote_context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root="/project",
        runtime_mode="ssh",
        remote_root="/srv/app",
        local_os="windows",
        local_shell="powershell",
        remote_os="linux",
        remote_shell="bash",
    )


@pytest.mark.asyncio
async def test_permission_policy_denies_file_write_before_runtime_call(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(allowed={"file.read:*"}, denied={"file.write:*"})),
        fake_runtime,
    )

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "x = 1\n"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["file.write:local"]
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_can_be_overridden_by_user_approval(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        _registry_with_approval(
            CapabilitySetPermissionPolicy(denied={"file.write:*"}),
            approval,
        ),
        fake_runtime,
    )

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "x = 1\n"},
        _context(),
    )

    assert result.ok
    assert result.metadata["permission_override"] is True
    assert result.metadata["approved_by_user"] is True
    assert result.metadata["preview_approved"] is True
    assert len(approval.requests) == 1
    assert approval.requests[0].tool_name == "write_file"
    assert approval.requests[0].preview_kind == "diff"
    assert approval.requests[0].decision.missing_capabilities == ("file.write:local",)
    assert fake_runtime.requests[-1][0] == "write_text"


@pytest.mark.asyncio
async def test_permission_policy_stays_denied_when_user_rejects(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=False)
    registry = _register_runtime_tools(
        _registry_with_approval(
            CapabilitySetPermissionPolicy(denied={"file.write:*"}),
            approval,
        ),
        fake_runtime,
    )

    result = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "x = 1\n"},
        _context(),
    )

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["approved_by_user"] is False
    assert len(approval.requests) == 1
    assert approval.requests[0].preview_kind == "diff"
    assert approval.requests[0].decision.reason == "write_file will modify calculator.py"
    assert approval.requests[0].decision.missing_capabilities == ("file.write:local",)
    assert "permission denied by policy" in approval.requests[0].decision.metadata["risks"][-1]
    assert "write_text" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_authorizes_file_read(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(allowed={"file.read:local"})),
        fake_runtime,
    )

    result = await registry.execute("read_file", {"path": "calculator.py"}, _context())

    assert result.ok
    assert "return a - b" in str(result.content)
    assert fake_runtime.requests[-1][0] == "read_text"


@pytest.mark.asyncio
async def test_permission_policy_denies_local_terminal_target(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"terminal.open:local"})),
        fake_runtime,
    )

    result = await registry.execute("open_terminal", {"target": "local"}, _context())

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["permission_requests"][0]["capability"] == "terminal.open"
    assert result.metadata["permission_requests"][0]["target"] == "local"
    assert "open_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_terminal_open_requires_user_approval_when_configured(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_terminal_open=True,
        ),
        fake_runtime,
    )

    result = await registry.execute("open_terminal", {"target": "local"}, _context())

    assert result.ok
    assert result.metadata["terminal_open_approved"] is True
    assert approval.requests[0].tool_name == "open_terminal"
    assert "interactive terminal" in approval.requests[0].decision.reason
    assert "open_terminal" in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_terminal_open_stays_blocked_when_user_rejects(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=False)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_terminal_open=True,
        ),
        fake_runtime,
    )

    result = await registry.execute("open_terminal", {"target": "local"}, _context())

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["approved_by_user"] is False
    assert "open_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_denies_terminal_control(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"terminal.send_input:*"})),
        fake_runtime,
    )
    context = _context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="local", os_name="linux", shell="bash")

    result = await registry.execute("send_terminal_control", {"control": "ctrl_c"}, context)

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["terminal.send_input:local"]
    assert "write_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_denies_terminal_command(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"terminal.send_input:*"})),
        fake_runtime,
    )
    context = _context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="local", os_name="linux", shell="bash")

    result = await registry.execute("terminal_command", {"data": "id"}, context)

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["terminal.send_input:local"]
    assert "write_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_sandbox_absolute_cwd_can_be_approved_before_runtime_call(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_sandbox_denials=True,
        ),
        fake_runtime,
    )

    result = await registry.execute(
        "run_command",
        {"argv": ["bash", "-lc", "pwd"], "cwd": "/tmp"},
        _remote_context(),
    )

    assert result.ok
    assert result.metadata["sandbox_override"] is True
    assert approval.requests[0].decision.missing_capabilities == ("sandbox.override:any",)
    assert (
        "run_command",
        {
            "environment_id": "env_ssh",
            "target_id": "target_ssh",
            "argv": ["bash", "-lc", "pwd"],
            "cwd": "/tmp",
        },
    ) in fake_runtime.requests


@pytest.mark.asyncio
async def test_sandbox_absolute_cwd_stays_blocked_when_user_rejects(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=False)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_sandbox_denials=True,
        ),
        fake_runtime,
    )

    result = await registry.execute(
        "run_command",
        {"argv": ["bash", "-lc", "pwd"], "cwd": "/tmp"},
        _remote_context(),
    )

    assert not result.ok
    assert result.error_code == "PATH_OUTSIDE_PROJECT"
    assert result.metadata["approved_by_user"] is False
    assert "run_command" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_denies_shell_file_write_in_run_command(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"file.write:*"})),
        fake_runtime,
    )

    result = await registry.execute(
        "run_command",
        {
            "argv": ["bash", "-lc", "cat > port_monitor.py <<'PY'\nprint('ok')\nPY"],
            "cwd": ".",
        },
        _remote_context(),
    )

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["file.write:remote"]
    assert "port_monitor.py" in result.metadata["permission_requests"][1]["resource"]
    assert "run_command" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_can_approve_shell_file_write_in_session(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        _registry_with_approval(
            CapabilitySetPermissionPolicy(denied={"file.write:*"}),
            approval,
        ),
        fake_runtime,
    )
    context = _remote_context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="remote", os_name="linux", shell="bash")

    result = await registry.execute(
        "terminal_command",
        {"data": "cat > port_monitor.py <<'PY'\nprint('ok')\nPY"},
        context,
    )

    assert result.ok
    assert approval.requests[0].permission_requests[1].capability_key == "file.write:remote"
    assert "port_monitor.py" in str(approval.requests[0].permission_requests[1].resource)
    assert fake_runtime.requests[-1] == (
        "write_terminal",
        {"session_id": "session_1", "data": "cat > port_monitor.py <<'PY'\nprint('ok')\nPY\n"},
    )


@pytest.mark.asyncio
async def test_root_escalation_approval_uses_specific_warning(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_sandbox_denials=True,
            approve_root_escalation=True,
        ),
        fake_runtime,
    )
    context = _remote_context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="remote", os_name="linux", shell="bash")

    result = await registry.execute(
        "terminal_command",
        {"data": "sudo -i"},
        context,
    )

    assert result.ok
    assert result.metadata["sandbox_override"] is True
    assert approval.requests[0].decision.metadata["warning"].startswith(
        "This operation attempts to open or enter a root/elevated shell"
    )
    assert any(
        "Subsequent terminal commands in this session may run with root privileges" in risk
        for risk in approval.requests[0].decision.metadata["risks"]
    )


@pytest.mark.asyncio
async def test_root_escalation_can_disable_approval_path(fake_runtime) -> None:
    approval = FakeApprovalHandler(approved=True)
    registry = _register_runtime_tools(
        ToolRegistry(
            permission_policy=CapabilitySetPermissionPolicy(),
            approval_handler=approval,
            approve_sandbox_denials=True,
            approve_root_escalation=False,
        ),
        fake_runtime,
    )
    context = _remote_context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="remote", os_name="linux", shell="bash")

    result = await registry.execute(
        "terminal_command",
        {"data": "sudo -i"},
        context,
    )

    assert not result.ok
    assert result.error_code == "SANDBOX_DENIED"
    assert result.metadata["sandbox_reason"] == "root_escalation"
    assert approval.requests == []
    assert "write_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_denies_sync_push_before_runtime_call(
    fake_runtime,
    tmp_path,
) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"sync.push:*"})),
        fake_runtime,
    )
    context = WorkContext(
        endpoint_id="endpoint_ssh",
        environment_id="env_ssh",
        target_id="target_ssh",
        project_root=str(tmp_path),
        runtime_mode="ssh",
        remote_root="/srv/app",
        sync_config=SyncConfig(enabled=True),
    )

    result = await registry.execute("sync_push", {}, context)

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["sync.push:remote"]
    assert "write_text" not in [name for name, _ in fake_runtime.requests]
