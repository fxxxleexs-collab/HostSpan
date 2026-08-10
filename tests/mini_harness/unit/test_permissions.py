from __future__ import annotations

import pytest

from mini_harness.permissions import CapabilitySetPermissionPolicy
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.config import SyncConfig
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry


def _registry(policy: CapabilitySetPermissionPolicy) -> ToolRegistry:
    registry = ToolRegistry(permission_policy=policy)
    return registry


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

    result = await registry.execute("open_local_terminal", {}, _context())

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["permission_requests"][0]["capability"] == "terminal.open"
    assert result.metadata["permission_requests"][0]["target"] == "local"
    assert "open_terminal" not in [name for name, _ in fake_runtime.requests]


@pytest.mark.asyncio
async def test_permission_policy_denies_run_in_session(fake_runtime) -> None:
    registry = _register_runtime_tools(
        _registry(CapabilitySetPermissionPolicy(denied={"session.run:*"})),
        fake_runtime,
    )
    context = _context()
    context.active_session_id = "session_1"
    context.mark_session_state(target="local", os_name="linux", shell="bash")

    result = await registry.execute("run_in_session", {"command": "id"}, context)

    assert not result.ok
    assert result.error_code == "PERMISSION_DENIED"
    assert result.metadata["missing_capabilities"] == ["session.run:local"]
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
