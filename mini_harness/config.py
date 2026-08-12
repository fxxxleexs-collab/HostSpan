from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mini_harness.permissions import PermissionsConfig
from mini_harness.sync.config import SyncConfig
from mini_harness.workspace import SandboxConfig


class AgentConfig(BaseModel):
    max_iterations: int = Field(default=30, ge=1)
    max_consecutive_tool_errors: int = Field(default=3, ge=1)
    repeated_action_limit: int = Field(default=3, ge=2)
    max_context_chars: int = Field(default=120_000, ge=1_000)
    max_tool_result_chars: int = Field(default=20_000, ge=1_000)
    recent_tool_turns: int = Field(default=12, ge=1)
    auto_compact_turns: int = Field(default=8, ge=2)
    auto_compact_tool_turns: int = Field(default=24, ge=2)
    tool_timeout_seconds: float = Field(default=300.0, gt=0)
    task_observe_poll_seconds: float = Field(default=0.5, ge=0)
    allow_unguarded_write: bool = True
    block_final_on_failed_command: bool = False


ProviderName = Literal["openai", "openai-compatible", "anthropic"]


class ModelConfig(BaseModel):
    provider: ProviderName = "openai"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    max_tokens: int = Field(default=4096, ge=1)
    anthropic_version: str = "2023-06-01"

    @classmethod
    def from_env(
        cls, model: str | None = None, provider: ProviderName | None = None
    ) -> ModelConfig:
        configured_provider = provider or _provider_from_env() or "openai"
        api_key = os.getenv("MINI_AGENT_API_KEY")
        if configured_provider == "anthropic":
            api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        return cls(
            provider=configured_provider,
            model=model or os.getenv("MINI_AGENT_MODEL") or _default_model(configured_provider),
            base_url=os.getenv("MINI_AGENT_BASE_URL") or _default_base_url(configured_provider),
            api_key=api_key,
        )


RuntimeMode = Literal["local", "ssh"]
SSHAuthMethod = Literal["auto", "agent", "key", "password"]


class SSHRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    username: str | None = None
    port: int = Field(default=22, ge=1, le=65_535)
    known_hosts_file: str | None = None
    auth_method: SSHAuthMethod = "auto"
    identity_file: str | None = None
    use_ssh_agent: bool = True
    proxy_jump: str | None = None
    connect_timeout: float = Field(default=300.0, gt=0)
    keepalive_interval: float = Field(default=20.0, gt=0)
    remote_root: str = "."
    prefer_tmux: bool = True
    allow_ssh_pty_fallback: bool = True

    @field_validator("remote_root")
    @classmethod
    def _valid_remote_root(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        if not normalized:
            raise ValueError("remote_root cannot be empty")
        if ".." in [part for part in normalized.split("/") if part]:
            raise ValueError("remote_root cannot contain parent traversal")
        return normalized


class RuntimeConfig(BaseModel):
    mode: RuntimeMode = "local"
    name: str = "mini-harness"
    ssh: SSHRuntimeConfig = Field(default_factory=SSHRuntimeConfig)


class HarnessConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    model: ModelConfig = Field(default_factory=ModelConfig.from_env)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)


def load_harness_config(
    config_path: str | None = None,
    project_root: str | None = None,
    model_override: str | None = None,
    provider_override: str | None = None,
    max_iterations_override: int | None = None,
    runtime_mode_override: str | None = None,
    ssh_host_override: str | None = None,
    ssh_user_override: str | None = None,
    ssh_port_override: int | None = None,
    ssh_auth_method_override: str | None = None,
    ssh_key_override: str | None = None,
    ssh_known_hosts_override: str | None = None,
    remote_root_override: str | None = None,
) -> HarnessConfig:
    payload: dict[str, object] = {}
    path = _resolve_config_path(config_path, project_root)
    if path is not None:
        payload = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    config = HarnessConfig.model_validate(payload)
    runtime = _runtime_from_env(config.runtime)
    runtime_mode = normalize_runtime_mode(runtime_mode_override) or runtime.mode
    ssh = runtime.ssh.model_copy(
        update={
            "hostname": ssh_host_override or runtime.ssh.hostname,
            "username": ssh_user_override or runtime.ssh.username,
            "port": ssh_port_override or runtime.ssh.port,
            "auth_method": normalize_ssh_auth_method(ssh_auth_method_override)
            or runtime.ssh.auth_method,
            "identity_file": ssh_key_override or runtime.ssh.identity_file,
            "known_hosts_file": ssh_known_hosts_override or runtime.ssh.known_hosts_file,
            "remote_root": remote_root_override or runtime.ssh.remote_root,
        }
    )
    runtime = runtime.model_copy(update={"mode": runtime_mode, "ssh": ssh})
    model = config.model
    env_provider = _provider_from_env()
    provider = normalize_provider(provider_override) or env_provider or model.provider
    model = model.model_copy(
        update={
            "provider": provider,
            "model": model_override
            or os.getenv("MINI_AGENT_MODEL")
            or model.model
            or _default_model(provider),
            "base_url": os.getenv("MINI_AGENT_BASE_URL")
            or model.base_url
            or _default_base_url(provider),
            "api_key": os.getenv("MINI_AGENT_API_KEY")
            or (os.getenv("ANTHROPIC_API_KEY") if provider == "anthropic" else None)
            or model.api_key,
        }
    )
    agent = config.agent
    if max_iterations_override is not None:
        agent = agent.model_copy(update={"max_iterations": max_iterations_override})
    return HarnessConfig(
        agent=agent,
        model=model,
        runtime=runtime,
        sandbox=config.sandbox,
        permissions=config.permissions,
        sync=config.sync,
    )


def normalize_provider(value: str | None) -> ProviderName | None:
    if value is None:
        return None
    if value in {"openai", "openai-compatible", "anthropic"}:
        return cast(ProviderName, value)
    raise ValueError("provider must be one of: openai, openai-compatible, anthropic")


def normalize_runtime_mode(value: str | None) -> RuntimeMode | None:
    if value is None:
        return None
    if value in {"local", "ssh"}:
        return cast(RuntimeMode, value)
    raise ValueError("runtime mode must be one of: local, ssh")


def normalize_ssh_auth_method(value: str | None) -> SSHAuthMethod | None:
    if value is None:
        return None
    if value in {"auto", "agent", "key", "password"}:
        return cast(SSHAuthMethod, value)
    raise ValueError("ssh auth method must be one of: auto, agent, key, password")


def _resolve_config_path(config_path: str | None, project_root: str | None) -> Path | None:
    if config_path:
        return Path(config_path).expanduser().resolve()
    candidates: list[Path] = []
    if project_root:
        root = Path(project_root).resolve()
        candidates.extend([root / "mini-harness.toml", root / ".mini-harness.toml"])
    candidates.extend([Path.cwd() / "mini-harness.toml", Path.cwd() / ".mini-harness.toml"])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _provider_from_env() -> ProviderName | None:
    return normalize_provider(os.getenv("MINI_AGENT_PROVIDER"))


def _runtime_from_env(config: RuntimeConfig) -> RuntimeConfig:
    mode = normalize_runtime_mode(os.getenv("MINI_AGENT_RUNTIME_MODE")) or config.mode
    ssh = config.ssh.model_copy(
        update={
            "hostname": os.getenv("MINI_AGENT_SSH_HOST") or config.ssh.hostname,
            "username": os.getenv("MINI_AGENT_SSH_USER") or config.ssh.username,
            "port": int(os.getenv("MINI_AGENT_SSH_PORT") or config.ssh.port),
            "known_hosts_file": os.getenv("MINI_AGENT_SSH_KNOWN_HOSTS")
            or config.ssh.known_hosts_file,
            "auth_method": normalize_ssh_auth_method(os.getenv("MINI_AGENT_SSH_AUTH_METHOD"))
            or config.ssh.auth_method,
            "identity_file": os.getenv("MINI_AGENT_SSH_KEY") or config.ssh.identity_file,
            "remote_root": os.getenv("MINI_AGENT_REMOTE_ROOT") or config.ssh.remote_root,
        }
    )
    return config.model_copy(update={"mode": mode, "ssh": ssh})


def _default_base_url(provider: ProviderName) -> str:
    if provider == "anthropic":
        return "https://api.anthropic.com"
    return "https://api.openai.com/v1"


def _default_model(provider: ProviderName) -> str | None:
    if provider == "anthropic":
        return None
    return "gpt-4.1-mini"
