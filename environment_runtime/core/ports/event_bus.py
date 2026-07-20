from __future__ import annotations

from typing import Protocol

from ..events import RuntimeEvent


class EventBus(Protocol):
    async def publish(self, event: RuntimeEvent) -> RuntimeEvent: ...

    async def list_events(self) -> list[RuntimeEvent]: ...
