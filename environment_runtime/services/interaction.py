from __future__ import annotations

from environment_runtime.core.errors import NotFoundError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import InputRequest, InputRequestStatus, InputType
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.security import WriterLeaseService
from environment_runtime.services.session import SessionService


class InteractionService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        self._leases = WriterLeaseService(context)
        self._sessions = SessionService(context)

    async def create_request(
        self,
        session_id: str,
        input_type: InputType,
        prompt: str | None = None,
        task_id: str | None = None,
        allowed_values: list[str] | None = None,
    ) -> InputRequest:
        await self._sessions.get(session_id)
        request = InputRequest(
            session_id=session_id,
            task_id=task_id,
            input_type=input_type,
            prompt=prompt,
            allowed_values=allowed_values,
        )
        await self.context.inputs.upsert(request)
        await self._emit("interaction.requested", request)
        return request

    async def submit_input(self, request_id: str, owner_id: str, value: str) -> InputRequest:
        request = await self.context.inputs.get(request_id)
        if request is None:
            raise NotFoundError(f"input request {request_id} was not found")
        await self._leases.validate(request.session_id, owner_id)
        await self._sessions.write(request.session_id, value)
        request.status = InputRequestStatus.RESOLVED
        await self.context.inputs.upsert(request)
        await self._emit("interaction.resolved", request)
        return request

    async def _emit(self, event_type: str, request: InputRequest) -> None:
        payload = request.model_dump(mode="json")
        if request.input_type == InputType.SECRET:
            payload["prompt"] = request.prompt
            payload["allowed_values"] = None
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="input_request",
            resource_id=request.request_id,
            payload=payload,
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
