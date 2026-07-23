from __future__ import annotations

from environment_runtime.core.capabilities import Capability
from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import Endpoint, EndpointStatus, SSHEndpointConfig
from environment_runtime.services.runtime import RuntimeContext


class EndpointService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def add_local(self, name: str, root: str) -> Endpoint:
        endpoint = Endpoint(
            name=name,
            provider_type="local",
            config={"root": root},
            capabilities={
                Capability.LOCAL_EXECUTION,
                Capability.LOCAL_FILESYSTEM,
                Capability.INTERACTIVE_SESSION,
                Capability.WORKSPACE_SYNC,
                Capability.ARTIFACTS,
                Capability.EVENTS,
            },
            status=EndpointStatus.READY,
        )
        await self.context.endpoints.upsert(endpoint)
        await self._emit("endpoint.connected", endpoint)
        return endpoint

    async def add_ssh(
        self,
        name: str,
        hostname: str,
        username: str,
        known_hosts_file: str,
        port: int = 22,
        identity_file: str | None = None,
        use_ssh_agent: bool = True,
        proxy_jump: str | None = None,
        connect_timeout: float = 15.0,
        keepalive_interval: float = 20.0,
    ) -> Endpoint:
        if identity_file is None and not use_ssh_agent:
            raise ValidationError("identity_file is required when use_ssh_agent is false")
        config = SSHEndpointConfig(
            hostname=hostname,
            port=port,
            username=username,
            identity_file=identity_file,
            use_ssh_agent=use_ssh_agent,
            known_hosts_file=known_hosts_file,
            proxy_jump=proxy_jump,
            connect_timeout=connect_timeout,
            keepalive_interval=keepalive_interval,
        )
        endpoint = Endpoint(
            name=name,
            provider_type="ssh",
            config=config.model_dump(mode="json"),
            capabilities={
                Capability.SSH_TRANSPORT,
                Capability.REMOTE_EXECUTION,
                Capability.REMOTE_FILESYSTEM,
                Capability.EVENTS,
            },
            status=EndpointStatus.DECLARED,
        )
        await self.context.endpoints.upsert(endpoint)
        return endpoint

    async def list_all(self) -> list[Endpoint]:
        return await self.context.endpoints.list()

    async def get(self, endpoint_id: str) -> Endpoint:
        endpoint = await self.context.endpoints.get(endpoint_id)
        if endpoint is None:
            raise NotFoundError(f"endpoint {endpoint_id} was not found")
        return endpoint

    async def health(self, endpoint_id: str) -> dict:
        endpoint = await self.get(endpoint_id)
        provider = self.context.providers.transport[endpoint.provider_type]
        result = await provider.healthcheck(endpoint)
        return {"endpoint_id": endpoint_id, **result}

    async def _emit(self, event_type: str, endpoint: Endpoint) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="endpoint",
            resource_id=endpoint.endpoint_id,
            payload=endpoint.model_dump(mode="json"),
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
