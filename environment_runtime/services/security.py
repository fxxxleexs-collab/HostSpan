from __future__ import annotations

from datetime import UTC, datetime, timedelta

from environment_runtime.core.errors import ConflictError, NotFoundError
from environment_runtime.core.models import WriterLease
from environment_runtime.services.runtime import RuntimeContext


class WriterLeaseService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def acquire(
        self,
        session_id: str,
        owner_type: str,
        owner_id: str,
        ttl_seconds: int = 300,
        force: bool = False,
    ) -> WriterLease:
        session = await self.context.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} was not found")
        current = await self.get_for_session(session_id)
        now = datetime.now(UTC)
        if current and current.expires_at > now and not force:
            raise ConflictError("session already has an active writer lease")
        lease = WriterLease(
            session_id=session_id,
            owner_type=owner_type,
            owner_id=owner_id,
            expires_at=now + timedelta(seconds=ttl_seconds),
            version=(current.version + 1 if current else 1),
        )
        await self.context.leases.upsert(lease)
        return lease

    async def get_for_session(self, session_id: str) -> WriterLease | None:
        leases = await self.context.leases.list()
        for lease in leases:
            if lease.session_id == session_id:
                return lease
        return None

    async def validate(self, session_id: str, owner_id: str) -> None:
        lease = await self.get_for_session(session_id)
        if lease is None:
            raise ConflictError("session has no writer lease")
        if lease.owner_id != owner_id:
            raise ConflictError("writer lease is held by another owner")
        if lease.expires_at <= datetime.now(UTC):
            raise ConflictError("writer lease has expired")
