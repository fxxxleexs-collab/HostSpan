from __future__ import annotations

import pytest

from mini_harness.errors import MiniHarnessError
from mini_harness.workspace import SandboxConfig, SandboxTargetConfig, WorkspacePolicy


def test_workspace_policy_maps_remote_paths_under_root() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    resolved = policy.runtime_path("src/app.py", target="remote")

    assert resolved.relative_path == "src/app.py"
    assert resolved.runtime_path == "/srv/app/src/app.py"


def test_workspace_policy_denies_secret_patterns() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.normalize_relative_path(".env")

    assert exc_info.value.code.value == "SANDBOX_DENIED"


def test_workspace_policy_off_skips_pattern_denies() -> None:
    policy = WorkspacePolicy(
        local_root="/project",
        remote_root="/srv/app",
        config=SandboxConfig(profile="off"),
    )

    assert policy.normalize_relative_path(".env") == ".env"


def test_workspace_policy_denies_parent_escape() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.normalize_relative_path("../outside")

    assert exc_info.value.code.value == "PATH_OUTSIDE_PROJECT"


def test_command_guard_denies_dangerous_command() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.authorize_command(["bash", "-lc", "rm -rf /"], target="remote")

    assert exc_info.value.code.value == "SANDBOX_DENIED"
    assert "destructive" in str(exc_info.value)


def test_command_guard_denies_root_shell_by_default() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.authorize_command(["sudo", "-i"], target="remote")

    assert exc_info.value.code.value == "SANDBOX_DENIED"
    assert "root shell" in str(exc_info.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["apt-get", "download", "tmux"],
        ["bash", "-lc", "apt-get download tmux"],
        ["apt", "source", "tmux"],
        ["python", "-m", "pip", "download", "requests"],
        ["npm", "pack", "left-pad"],
    ],
)
def test_command_guard_denies_package_downloads_by_default(argv: list[str]) -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.authorize_command(argv, target="remote")

    assert exc_info.value.code.value == "SANDBOX_DENIED"
    assert "package installation" in str(exc_info.value)


def test_command_guard_allows_root_shell_when_configured() -> None:
    policy = WorkspacePolicy(
        local_root="/project",
        remote_root="/srv/app",
        config=SandboxConfig(remote=SandboxTargetConfig(allow_root_shell=True)),
    )

    result = policy.authorize_command(["sudo", "-i"], target="remote")

    assert result.allowed


def test_policy_only_engine_wraps_without_changing_command() -> None:
    policy = WorkspacePolicy(local_root="/project", remote_root="/srv/app")

    sandboxed = policy.sandbox_task(["python", "-m", "pytest"], "/srv/app", target="remote")

    assert sandboxed.argv == ["python", "-m", "pytest"]
    assert sandboxed.cwd == "/srv/app"
    assert sandboxed.engine == "policy-only"


def test_command_guard_denies_network_when_disabled() -> None:
    policy = WorkspacePolicy(
        local_root="/project",
        remote_root="/srv/app",
        config=SandboxConfig(remote=SandboxTargetConfig(network="disabled")),
    )

    with pytest.raises(MiniHarnessError) as exc_info:
        policy.authorize_command(["curl", "https://example.test"], target="remote")

    assert exc_info.value.code.value == "SANDBOX_DENIED"
    assert "network" in str(exc_info.value)
