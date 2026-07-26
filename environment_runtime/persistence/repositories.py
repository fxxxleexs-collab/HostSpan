from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from environment_runtime.core.events import ExposurePolicy, RuntimeEvent
from environment_runtime.core.models import TerminalFrame, TerminalFrameKind
from environment_runtime.persistence.orm_models import (
    EventRecord,
    LogRecord,
    ResourceRecord,
    TerminalFrameRecord,
)

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

    async def resume_offset(self, task_id: str) -> int:
        """Byte position at which to resume tailing a detached task's log file.

        Log records store the *start* offset of each chunk, so the resume point
        is the last record's offset plus its chunk length (0 if no records).
        Reconnect seeks the log file to this position so already-persisted bytes
        are not re-appended.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(LogRecord.offset, LogRecord.chunk)
                .where(LogRecord.task_id == task_id)
                .order_by(LogRecord.offset.desc())
                .limit(1)
            )
            row = result.first()
            if row is None:
                return 0
            offset, chunk = row
            return int(offset) + len(chunk)


class TerminalFrameRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._append_lock = asyncio.Lock()

    async def append(
        self,
        session_id: str,
        kind: TerminalFrameKind,
        data: str,
        stream: str = "pty",
        encoding: str = "utf-8",
    ) -> TerminalFrame:
        async with self._append_lock, self._session_factory() as session:
            seq_result = await session.execute(
                select(func.max(TerminalFrameRecord.seq)).where(
                    TerminalFrameRecord.session_id == session_id
                )
            )
            offset_result = await session.execute(
                select(TerminalFrameRecord.offset, TerminalFrameRecord.data)
                .where(TerminalFrameRecord.session_id == session_id)
                .order_by(TerminalFrameRecord.seq.desc())
                .limit(1)
            )
            last_seq = seq_result.scalar_one_or_none()
            last = offset_result.first()
            offset = 0 if last is None else int(last[0]) + len(str(last[1]))
            frame = TerminalFrame(
                session_id=session_id,
                seq=0 if last_seq is None else int(last_seq) + 1,
                offset=offset,
                kind=kind,
                stream=stream,
                data=data,
                encoding=encoding,
            )
            session.add(
                TerminalFrameRecord(
                    frame_id=frame.frame_id,
                    session_id=frame.session_id,
                    seq=frame.seq,
                    offset=frame.offset,
                    kind=frame.kind.value,
                    stream=frame.stream,
                    data=frame.data,
                    encoding=frame.encoding,
                    created_at=frame.created_at,
                )
            )
            await session.commit()
            return frame

    async def list_frames(
        self,
        session_id: str,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[TerminalFrame]:
        async with self._session_factory() as session:
            query = select(TerminalFrameRecord).where(
                TerminalFrameRecord.session_id == session_id
            )
            if after_seq is not None:
                query = query.where(TerminalFrameRecord.seq > after_seq)
            query = query.order_by(TerminalFrameRecord.seq.asc()).limit(limit)
            result = await session.execute(query)
            return [_frame_from_record(record) for record in result.scalars().all()]

    async def tail_text(self, session_id: str, limit_chars: int = 20_000) -> str:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TerminalFrameRecord)
                .where(
                    TerminalFrameRecord.session_id == session_id,
                    TerminalFrameRecord.kind == TerminalFrameKind.OUTPUT.value,
                )
                .order_by(TerminalFrameRecord.seq.desc())
            )
            chunks: list[str] = []
            total = 0
            for record in result.scalars().all():
                chunks.append(record.data)
                total += len(record.data)
                if total >= limit_chars:
                    break
            text = "".join(reversed(chunks))
            return text[-limit_chars:]

    async def last_seq(self, session_id: str) -> int | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.max(TerminalFrameRecord.seq)).where(
                    TerminalFrameRecord.session_id == session_id
                )
            )
            value = result.scalar_one_or_none()
            return int(value) if value is not None else None


def _frame_from_record(record: TerminalFrameRecord) -> TerminalFrame:
    return TerminalFrame(
        frame_id=record.frame_id,
        session_id=record.session_id,
        seq=record.seq,
        offset=record.offset,
        kind=TerminalFrameKind(record.kind),
        stream=record.stream,
        data=record.data,
        encoding=record.encoding,
        created_at=record.created_at,
    )
