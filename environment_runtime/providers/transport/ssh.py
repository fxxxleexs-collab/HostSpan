from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

from environment_runtime.core.errors import ProviderError, ValidationError
from environment_runtime.core.models import Endpoint, SSHEndpointConfig

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection


class SSHTransportProvider:
    """AsyncSSH transport with strict host-key checking by default."""

    def __init__(self) -> None:
        self._connections: dict[str, SSHClientConnection] = {}

    async def connect(self, endpoint: Endpoint) -> SSHClientConnection:
        config = self._load_config(endpoint)
        cached = self._connections.get(endpoint.endpoint_id)
        if cached is not None and not cached.is_closed():
            return cached

        if config.proxy_jump:
            raise ProviderError("proxy_jump is not implemented for SSH endpoints yet")

        known_hosts = Path(config.known_hosts_file).expanduser()
        if not known_hosts.exists():
            raise ValidationError(f"known_hosts_file does not exist: {known_hosts}")

        client_keys: list[str] | None = None
        if config.identity_file:
            key_path = Path(config.identity_file).expanduser()
            if not key_path.exists():
                raise ValidationError(f"identity_file does not exist: {key_path}")
            client_keys = [str(key_path)]
        elif not config.use_ssh_agent:
            raise ValidationError("identity_file is required when use_ssh_agent is false")

        try:
            connection = await asyncssh.connect(
                config.hostname,
                port=config.port,
                username=config.username,
                client_keys=client_keys,
                agent_path=() if config.use_ssh_agent else None,
                known_hosts=str(known_hosts),
                login_timeout=config.connect_timeout,
                keepalive_interval=config.keepalive_interval,
            )
        except (OSError, asyncssh.Error) as exc:
            raise ProviderError(f"SSH connection failed: {exc}") from exc

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
