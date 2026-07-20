from __future__ import annotations

from environment_runtime.core.capabilities import Capability
from environment_runtime.core.errors import NotFoundError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import Endpoint, EndpointStatus
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
        result = await provider.healthcheck()
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
