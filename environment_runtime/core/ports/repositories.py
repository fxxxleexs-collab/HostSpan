from __future__ import annotations

from typing import Protocol, TypeVar

ModelT = TypeVar("ModelT")


class Repository(Protocol[ModelT]):
    async def get(self, resource_id: str) -> ModelT | None: ...

    async def list(self) -> list[ModelT]: ...

    async def upsert(self, model: ModelT) -> ModelT: ...

    async def delete(self, resource_id: str) -> None: ...
