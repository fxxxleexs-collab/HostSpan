from __future__ import annotations

import asyncio
from pathlib import Path

from environment_runtime.core.errors import NotFoundError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import InteractionState, Session, SessionState
from environment_runtime.services.runtime import RuntimeContext


class SessionService:
    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    async def create(
        self,
        environment_id: str,
        target_id: str,
        argv: list[str],
        cwd: str | None = None,
    ) -> Session:
        session = Session(
            environment_id=environment_id,
            target_id=target_id,
            backend="local_pty",
            command=argv,
            default_cwd=cwd,
            state=SessionState.CREATING,
        )

        async def on_output(stream: str, chunk: str) -> None:
            await self._emit(
                "session.output",
                session,
                {"stream": stream, "chunk": chunk, "session_id": session.session_id},
            )

        handle = await self.context.providers.session["local_pty"].create(
            argv=argv,
            cwd=Path(cwd) if cwd else None,
            env={},
            on_output=on_output,
        )
        session.state = SessionState.ACTIVE
        session.interaction_state = InteractionState.AUTOMATION_CONTROLLED
        session.backend_ref = {"pid": handle.process.pid}
        await self.context.sessions.upsert(session)
        self.context.active.session_handles[session.session_id] = handle
        watcher = asyncio.create_task(self._watch_session(session.session_id))
        self.context.active.background_tasks.append(watcher)
        await self._emit("session.created", session, session.model_dump(mode="json"))
        return session

    async def list_all(self) -> list[Session]:
        return await self.context.sessions.list()

    async def get(self, session_id: str) -> Session:
        session = await self.context.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} was not found")
        return session

    async def write(self, session_id: str, data: str) -> Session:
        session = await self.get(session_id)
        handle = self.context.active.session_handles.get(session_id)
        if handle is None:
            raise NotFoundError(f"session {session_id} is not active")
        await handle.write(data)
        return session

    async def terminate(self, session_id: str) -> Session:
        session = await self.get(session_id)
        handle = self.context.active.session_handles.get(session_id)
        if handle is not None:
            await handle.terminate()
            self.context.active.session_handles.pop(session_id, None)
        session.state = SessionState.TERMINATED
        await self.context.sessions.upsert(session)
        await self._emit("session.detached", session, {"state": session.state})
        return session

    async def _watch_session(self, session_id: str) -> None:
        handle = self.context.active.session_handles.get(session_id)
        if handle is None:
            return
        await handle.wait()
        session = await self.get(session_id)
        session.state = SessionState.TERMINATED
        session.interaction_state = InteractionState.NONE
        await self.context.sessions.upsert(session)
        self.context.active.session_handles.pop(session_id, None)
        await self._emit("session.lost", session, {"returncode": handle.process.returncode})

    async def _emit(self, event_type: str, session: Session, payload: dict) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            resource_type="session",
            resource_id=session.session_id,
            environment_id=session.environment_id,
            payload=payload,
        )
        await self.context.event_store.append(event)
        await self.context.event_bus.publish(event)
