from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

from environment_runtime.core.errors import ProviderError, ValidationError
from environment_runtime.core.models import Endpoint, SSHEndpointConfig

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection


class _TrustHostOnceSSHClient(asyncssh.SSHClient):
    def validate_host_public_key(self, host: str, addr: str, port: int, key: Any) -> bool:
        _ = host, addr, port, key
        return True

    def validate_host_ca_key(self, host: str, addr: str, port: int, key: Any) -> bool:
        _ = host, addr, port, key
        return True


class SSHTransportProvider:
    """AsyncSSH transport with strict host-key checking by default."""

    def __init__(self, secret_resolver: Callable[[str], str] | None = None) -> None:
        self._secret_resolver = secret_resolver
        self._connections: dict[str, SSHClientConnection] = {}
        self._trust_host_once_endpoint_ids: set[str] = set()

    def set_secret_resolver(self, resolver: Callable[[str], str] | None) -> None:
        self._secret_resolver = resolver

    def trust_host_once(self, endpoint_id: str) -> None:
        self._trust_host_once_endpoint_ids.add(endpoint_id)

    async def connect(self, endpoint: Endpoint) -> SSHClientConnection:
        config = self._load_config(endpoint)
        trust_host_once = endpoint.endpoint_id in self._trust_host_once_endpoint_ids
        cached = self._connections.get(endpoint.endpoint_id)
        if cached is not None and not cached.is_closed():
            if trust_host_once:
                self._trust_host_once_endpoint_ids.discard(endpoint.endpoint_id)
            return cached

        if config.proxy_jump:
            raise ProviderError("proxy_jump is not implemented for SSH endpoints yet")

        known_hosts = Path(config.known_hosts_file).expanduser()
        if not trust_host_once and not known_hosts.exists():
            raise ValidationError(f"known_hosts_file does not exist: {known_hosts}")

        client_keys: list[str] | None = None
        password: str | None = None
        agent_path = () if config.use_ssh_agent else None
        if config.identity_file:
            key_path = Path(config.identity_file).expanduser()
            if not key_path.exists():
                raise ValidationError(f"identity_file does not exist: {key_path}")
            client_keys = [str(key_path)]
        if config.password_secret_ref:
            if self._secret_resolver is None:
                raise ValidationError("SSH password secret resolver is not available")
            password = self._secret_resolver(config.password_secret_ref)
        if config.auth_method == "key" and client_keys is None:
            raise ValidationError("identity_file is required when auth_method is key")
        if config.auth_method == "password" and password is None:
            raise ValidationError("password is required when auth_method is password")
        if config.auth_method == "agent":
            if not config.use_ssh_agent:
                raise ValidationError("use_ssh_agent must be true when auth_method is agent")
            client_keys = None
            password = None
            agent_path = ()
        if config.auth_method == "password":
            client_keys = None
            agent_path = None
        if (
            config.auth_method == "auto"
            and client_keys is None
            and not config.use_ssh_agent
            and password is None
        ):
            raise ValidationError(
                "identity_file, password_secret_ref, or use_ssh_agent is required for SSH authentication"
            )

        connect_kwargs: dict[str, Any] = {
            "port": config.port,
            "username": config.username,
            "client_keys": client_keys,
            "password": password,
            "agent_path": agent_path,
            "known_hosts": str(known_hosts),
            "login_timeout": config.connect_timeout,
            "keepalive_interval": config.keepalive_interval,
        }
        if trust_host_once:
            connect_kwargs.update(
                {
                    "known_hosts": b"",
                    "client_factory": _TrustHostOnceSSHClient,
                    "server_host_key_algs": "default",
                }
            )

        try:
            connection = await asyncssh.connect(config.hostname, **connect_kwargs)
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"SSH connection failed: {exc}") from exc
        finally:
            self._trust_host_once_endpoint_ids.discard(endpoint.endpoint_id)

        self._connections[endpoint.endpoint_id] = connection
        return connection

    async def healthcheck(self, endpoint: Endpoint) -> dict[str, Any]:
        connection = await self.connect(endpoint)
        try:
            result = await connection.run("true", check=True)
        except (OSError, asyncssh.Error) as exc:
            self._connections.pop(endpoint.endpoint_id, None)
            raise ProviderError(f"SSH healthcheck failed: {exc}") from exc
        return {
            "status": "ok",
            "hostname": endpoint.config.get("hostname"),
            "port": endpoint.config.get("port"),
            "exit_status": result.exit_status,
        }

    async def close(self, endpoint_id: str) -> None:
        connection = self._connections.pop(endpoint_id, None)
        if connection is None:
            return
        connection.close()
        await connection.wait_closed()

    async def close_all(self) -> None:
        for endpoint_id in list(self._connections):
            await self.close(endpoint_id)

    def _load_config(self, endpoint: Endpoint) -> SSHEndpointConfig:
        if endpoint.provider_type != "ssh":
            raise ValidationError(f"endpoint {endpoint.endpoint_id} is not an SSH endpoint")
        return SSHEndpointConfig.model_validate(endpoint.config)
