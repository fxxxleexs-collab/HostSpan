from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.config import load_harness_config


def test_load_config_supports_anthropic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("MINI_AGENT_MODEL", raising=False)
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[agent]
max_iterations = 9

[model]
provider = "anthropic"
model = "claude-test"
api_key = "from-file"
base_url = "https://anthropic.example"
max_tokens = 2048
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.agent.max_iterations == 9
    assert config.model.provider == "anthropic"
    assert config.model.model == "claude-test"
    assert config.model.api_key == "from-file"
    assert config.model.base_url == "https://anthropic.example"
    assert config.model.max_tokens == 2048


def test_env_and_cli_override_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[agent]
max_iterations = 5

[model]
provider = "openai"
model = "file-model"
api_key = "from-file"
""",
        encoding="utf-8",
    )

    config = load_harness_config(
        config_path=str(config_path),
        project_root=str(tmp_path),
        model_override="cli-model",
        max_iterations_override=3,
    )

    assert config.agent.max_iterations == 3
    assert config.model.provider == "anthropic"
    assert config.model.model == "cli-model"
    assert config.model.api_key == "from-env"


def test_load_config_supports_ssh_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_AGENT_RUNTIME_MODE", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[runtime]
mode = "ssh"
name = "remote-dev"

[runtime.ssh]
hostname = "example.test"
username = "envrt"
port = 2222
known_hosts_file = "known_hosts"
identity_file = "id_ed25519"
auth_method = "key"
use_ssh_agent = false
remote_root = "/srv/project"
prefer_tmux = true
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.runtime.mode == "ssh"
    assert config.runtime.name == "remote-dev"
    assert config.runtime.ssh.hostname == "example.test"
    assert config.runtime.ssh.username == "envrt"
    assert config.runtime.ssh.port == 2222
    assert config.runtime.ssh.auth_method == "key"
    assert config.runtime.ssh.identity_file == "id_ed25519"
    assert config.runtime.ssh.known_hosts_file == "known_hosts"
    assert config.runtime.ssh.remote_root == "/srv/project"


def test_load_config_supports_ssh_password_auth_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINI_AGENT_RUNTIME_MODE", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[runtime]
mode = "ssh"

[runtime.ssh]
hostname = "example.test"
username = "envrt"
known_hosts_file = "known_hosts"
auth_method = "password"
use_ssh_agent = false
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.runtime.ssh.auth_method == "password"
    assert config.runtime.ssh.use_ssh_agent is False


def test_load_config_rejects_plaintext_ssh_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINI_AGENT_RUNTIME_MODE", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[runtime]
mode = "ssh"

[runtime.ssh]
hostname = "example.test"
username = "envrt"
known_hosts_file = "known_hosts"
auth_method = "password"
password = "secret"
use_ssh_agent = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_harness_config(config_path=str(config_path), project_root=str(tmp_path))


def test_load_config_supports_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_AGENT_RUNTIME_MODE", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[sandbox]
profile = "strict"
engine = "policy-only"

[sandbox.remote]
root = "/srv/app"
require_engine = true
network = "disabled"
allow_root_shell = false
allow_package_install = false

[sandbox.paths]
allow = ["src/**", "tests/**"]
deny = [".env", "**/*.pem"]
follow_symlinks = false
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.sandbox.profile == "strict"
    assert config.sandbox.engine == "policy-only"
    assert config.sandbox.remote.root == "/srv/app"
    assert config.sandbox.remote.require_engine is True
    assert config.sandbox.remote.network == "disabled"
    assert config.sandbox.paths.allow == ["src/**", "tests/**"]
    assert config.sandbox.paths.deny == [".env", "**/*.pem"]


def test_load_config_supports_permissions(tmp_path: Path) -> None:
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[permissions]
allow = ["file.read:local", "task.observe:*"]
deny = ["file.write:*", "terminal.open:remote"]
approve_sandbox_denials = false
approve_terminal_open = true
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.permissions.allow == ["file.read:local", "task.observe:*"]
    assert config.permissions.deny == ["file.write:*", "terminal.open:remote"]
    assert config.permissions.approve_sandbox_denials is False
    assert config.permissions.approve_terminal_open is True


def test_ssh_runtime_env_and_cli_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_AGENT_RUNTIME_MODE", "ssh")
    monkeypatch.setenv("MINI_AGENT_SSH_HOST", "env-host")
    monkeypatch.setenv("MINI_AGENT_SSH_USER", "env-user")
    monkeypatch.setenv("MINI_AGENT_SSH_PORT", "2200")
    monkeypatch.setenv("MINI_AGENT_SSH_AUTH_METHOD", "password")
    monkeypatch.setenv("MINI_AGENT_SSH_KEY", "env-key")
    monkeypatch.setenv("MINI_AGENT_SSH_KNOWN_HOSTS", "env-known-hosts")
    monkeypatch.setenv("MINI_AGENT_REMOTE_ROOT", "/env/root")

    config = load_harness_config(
        project_root=str(tmp_path),
        ssh_host_override="cli-host",
        remote_root_override="/cli/root",
    )

    assert config.runtime.mode == "ssh"
    assert config.runtime.ssh.hostname == "cli-host"
    assert config.runtime.ssh.username == "env-user"
    assert config.runtime.ssh.port == 2200
    assert config.runtime.ssh.auth_method == "password"
    assert config.runtime.ssh.identity_file == "env-key"
    assert config.runtime.ssh.known_hosts_file == "env-known-hosts"
    assert config.runtime.ssh.remote_root == "/cli/root"


def test_ssh_runtime_cli_auth_method_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINI_AGENT_SSH_AUTH_METHOD", raising=False)

    config = load_harness_config(
        project_root=str(tmp_path),
        ssh_auth_method_override="agent",
    )

    assert config.runtime.ssh.auth_method == "agent"
