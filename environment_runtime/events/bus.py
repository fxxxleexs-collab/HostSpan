from __future__ import annotations

import asyncio

from environment_runtime.core.events import RuntimeEvent


class InMemoryEventBus:
    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._sequence = 0
        self._subscribers: set[asyncio.Queue[RuntimeEvent]] = set()

    async def publish(self, event: RuntimeEvent) -> RuntimeEvent:
        self._sequence += 1
        event.sequence = self._sequence
        self._events.append(event)
        for queue in list(self._subscribers):
            await queue.put(event)
        return event

    async def list_events(self) -> list[RuntimeEvent]:
        return list(self._events)

    async def subscribe(self) -> asyncio.Queue[RuntimeEvent]:
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[RuntimeEvent]) -> None:
        self._subscribers.discard(queue)
