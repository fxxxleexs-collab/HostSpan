from __future__ import annotations

import contextlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from environment_runtime.config import RuntimeSettings
from environment_runtime.core.errors import ConflictError, NotFoundError, SecurityError
from environment_runtime.core.models import WriterLease
from environment_runtime.services.runtime import RuntimeContext


@dataclass(frozen=True)
class Principal:
    principal_id: str
    principal_type: str = "agent"
    scope_id: str = "default"


def broker_token_path(settings: RuntimeSettings) -> Path:
    return settings.runtime.data_dir / "broker.token"


def ensure_broker_token(settings: RuntimeSettings) -> str:
    path = broker_token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return token


def read_broker_token(settings: RuntimeSettings) -> str:
    path = broker_token_path(settings)
    if not path.exists():
        raise SecurityError(f"broker token file does not exist: {path}")
    return path.read_text(encoding="utf-8").strip()


def validate_broker_token(settings: RuntimeSettings, token: str | None) -> None:
    if token is None or not token.strip():
        raise SecurityError("broker auth token is required")
    expected = read_broker_token(settings)
    if not hmac.compare_digest(expected, token):
        raise SecurityError("broker auth token is invalid")


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
