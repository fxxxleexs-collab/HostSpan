from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from environment_runtime.config import RuntimeSettings
from environment_runtime.persistence.orm_models import Base


def create_engine(settings: RuntimeSettings) -> AsyncEngine:
    return create_async_engine(settings.database.url, future=True)


def create_session_factory(settings: RuntimeSettings) -> async_sessionmaker[AsyncSession]:
    engine = create_engine(settings)
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
