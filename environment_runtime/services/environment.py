from __future__ import annotations

from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import Environment, EnvironmentState, ExecutionTarget
from environment_runtime.services.runtime import RuntimeContext


class EnvironmentService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def create(
        self,
        name: str,
        endpoint_ids: list[str],
        workspace_ids: list[str] | None = None,
    ) -> Environment:
        if not endpoint_ids:
            raise ValidationError("an environment requires at least one endpoint")
        targets: list[ExecutionTarget] = []
        for endpoint_id in endpoint_ids:
            endpoint = await self.context.endpoints.get(endpoint_id)
            if endpoint is None:
                raise NotFoundError(f"endpoint {endpoint_id} was not found")
            targets.append(
                ExecutionTarget(
                    endpoint_id=endpoint.endpoint_id,
                    provider=(
                        "local_process"
                        if endpoint.provider_type == "local"
                        else "ssh_process"
                        if endpoint.provider_type == "ssh"
                        else "unknown"
                    ),
                    capabilities=endpoint.capabilities,
                )
            )
        environment = Environment(
            name=name,
            endpoint_ids=endpoint_ids,
            workspace_ids=workspace_ids or [],
            execution_targets=targets,
            default_execution_target_id=targets[0].target_id,
            default_session_backend=_default_session_backend(targets[0].provider),
            required_capabilities=set(),
            status=EnvironmentState.READY,
        )
        await self.context.environments.upsert(environment)
        await self._emit("environment.ready", environment)
        return environment

    async def get(self, environment_id: str) -> Environment:
        environment = await self.context.environments.get(environment_id)
        if environment is None:
            raise NotFoundError(f"environment {environment_id} was not found")
        return environment

    async def list_all(self) -> list[Environment]:
        return await self.context.environments.list()

    async def delete(self, environment_id: str) -> None:
        await self.get(environment_id)
        await self.context.environments.delete(environment_id)

    async def reconcile(self, environment_id: str) -> Environment:
        environment = await self.get(environment_id)
        running_tasks = [
            task
            for task in await self.context.tasks.list()
            if task.environment_id == environment_id and task.state in {"RUNNING", "PREPARING"}
        ]
        if running_tasks:
            environment.status = EnvironmentState.DEGRADED
            await self._emit("environment.degraded", environment)
        else:
            environment.status = EnvironmentState.READY
            await self._emit("environment.ready", environment)
        await self.context.environments.upsert(environment)
        return environment

    async def _emit(self, event_type: str, environment: Environment) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="environment",
            resource_id=environment.environment_id,
            environment_id=environment.environment_id,
            payload=environment.model_dump(mode="json"),
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)


def _default_session_backend(target_provider: str) -> str | None:
    if target_provider == "local_process":
        return "local_pty"
    if target_provider == "ssh_process":
        return "ssh_pty"
    return None
