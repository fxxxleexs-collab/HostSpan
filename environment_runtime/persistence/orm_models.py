from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ResourceRecord(Base):
    __tablename__ = "resources"

    resource_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class EventRecord(Base):
    __tablename__ = "events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    event_type: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(128))
    environment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[str] = mapped_column(Text())
    exposure: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class LogRecord(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    stream: Mapped[str] = mapped_column(String(16))
    offset: Mapped[int] = mapped_column(Integer)
    chunk: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
