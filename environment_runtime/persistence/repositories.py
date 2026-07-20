from __future__ import annotations

import json
from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from environment_runtime.core.events import ExposurePolicy, RuntimeEvent
from environment_runtime.persistence.orm_models import EventRecord, LogRecord, ResourceRecord

ModelT = TypeVar("ModelT", bound=BaseModel)


class SqlAlchemyRepository(Generic[ModelT]):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        resource_type: str,
        loader: Callable[[dict], ModelT],
        id_field: str,
    ) -> None:
        self._session_factory = session_factory
        self._resource_type = resource_type
        self._loader = loader
        self._id_field = id_field

    async def get(self, resource_id: str) -> ModelT | None:
        async with self._session_factory() as session:
            record = await session.get(ResourceRecord, (self._resource_type, resource_id))
            if record is None:
                return None
            return self._loader(json.loads(record.payload))

    async def list(self) -> list[ModelT]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ResourceRecord).where(ResourceRecord.resource_type == self._resource_type)
            )
            return [self._loader(json.loads(record.payload)) for record in result.scalars().all()]

    async def upsert(self, model: ModelT) -> ModelT:
        resource_id = getattr(model, self._id_field)
        async with self._session_factory() as session:
            record = await session.get(ResourceRecord, (self._resource_type, resource_id))
            payload = model.model_dump_json()
            if record is None:
                record = ResourceRecord(
                    resource_type=self._resource_type, resource_id=resource_id, payload=payload
                )
                session.add(record)
            else:
                record.payload = payload
            await session.commit()
        return model

    async def delete(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(ResourceRecord).where(
                    ResourceRecord.resource_type == self._resource_type,
                    ResourceRecord.resource_id == resource_id,
                )
            )
            await session.commit()


class SqlAlchemyEventStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        async with self._session_factory() as session:
            record = EventRecord(
                event_id=event.event_id,
                event_type=event.event_type,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                environment_id=event.environment_id,
                payload=json.dumps(event.payload),
                exposure=event.exposure.value,
            )
            session.add(record)
            await session.flush()
            event.sequence = record.sequence
            await session.commit()
            return event

    async def list_events(self) -> list[RuntimeEvent]:
        async with self._session_factory() as session:
            result = await session.execute(select(EventRecord).order_by(EventRecord.sequence.asc()))
            events: list[RuntimeEvent] = []
            for record in result.scalars().all():
                events.append(
                    RuntimeEvent(
                        event_id=record.event_id,
                        sequence=record.sequence,
                        event_type=record.event_type,
                        resource_type=record.resource_type,
                        resource_id=record.resource_id,
                        environment_id=record.environment_id,
                        payload=json.loads(record.payload),
                        exposure=ExposurePolicy(record.exposure),
                        timestamp=record.created_at,
                    )
                )
            return events


class LogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, task_id: str, stream: str, offset: int, chunk: str) -> None:
        async with self._session_factory() as session:
            session.add(LogRecord(task_id=task_id, stream=stream, offset=offset, chunk=chunk))
            await session.commit()

    async def get_task_logs(self, task_id: str) -> list[dict]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(LogRecord).where(LogRecord.task_id == task_id).order_by(LogRecord.offset.asc())
            )
            return [
                {"stream": row.stream, "offset": row.offset, "chunk": row.chunk}
                for row in result.scalars().all()
            ]
