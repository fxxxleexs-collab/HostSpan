from __future__ import annotations

import asyncio

from environment_runtime.core.errors import NotFoundError, ValidationError
from environment_runtime.core.events import RuntimeEvent
from environment_runtime.core.models import (
    Endpoint,
    Environment,
    ExecutionTarget,
    InteractionState,
    Session,
    SessionState,
    TerminalFrame,
    TerminalFrameKind,
)
from environment_runtime.providers.session.base import SessionCreateParams, TerminalSize
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
        env: dict[str, str] | None = None,
        backend: str | None = None,
        cols: int = 120,
        rows: int = 30,
        term_type: str = "xterm-256color",
    ) -> Session:
        if not argv:
            raise ValidationError("session argv cannot be empty")
        environment, target, endpoint = await self._resolve_target(environment_id, target_id)
        backend_name = self._resolve_backend(environment, endpoint, backend)
        session = Session(
            environment_id=environment_id,
            target_id=target_id,
            backend=backend_name,
            command=argv,
            environment_variables=env or {},
            default_cwd=cwd,
            terminal_cols=cols,
            terminal_rows=rows,
            term_type=term_type,
            state=SessionState.CREATING,
        )

        provider = self.context.providers.session.get(backend_name)
        if provider is None:
            raise ValidationError(f"session backend is not registered: {backend_name}")
        handle = await provider.create(
            SessionCreateParams(
                session_id=session.session_id,
                environment=environment,
                target=target,
                endpoint=endpoint,
                argv=argv,
                cwd=cwd,
                env=env or {},
                terminal_size=TerminalSize(cols=cols, rows=rows),
                term_type=term_type,
            ),
            on_output=self._make_output_callback(session),
        )
        session.state = SessionState.ACTIVE
        session.interaction_state = InteractionState.AUTOMATION_CONTROLLED
        session.backend_ref = handle.backend_ref()
        await self.context.sessions.upsert(session)
        self.context.active.session_handles[session.session_id] = handle
        watcher = asyncio.create_task(self._watch_session(session.session_id))
        self.context.active.background_tasks.append(watcher)
        await self._emit("session.created", session, session.model_dump(mode="json"))
        return session

    async def _resolve_target(
        self, environment_id: str, target_id: str
    ) -> tuple[Environment, ExecutionTarget, Endpoint]:
        environment = await self.context.environments.get(environment_id)
        if environment is None:
            raise NotFoundError(f"environment {environment_id} was not found")
        target = next(
            (item for item in environment.execution_targets if item.target_id == target_id),
            None,
        )
        if target is None:
            raise NotFoundError(f"target {target_id} was not found in environment {environment_id}")
        endpoint = await self.context.endpoints.get(target.endpoint_id)
        if endpoint is None:
            raise NotFoundError(f"endpoint {target.endpoint_id} was not found")
        return environment, target, endpoint

    def _resolve_backend(
        self,
        environment: Environment,
        endpoint: Endpoint,
        requested_backend: str | None,
    ) -> str:
        if requested_backend:
            return requested_backend
        if environment.default_session_backend:
            return environment.default_session_backend
        if endpoint.provider_type == "local":
            return "local_pty"
        if endpoint.provider_type == "ssh":
            return "ssh_pty"
        raise ValidationError(f"no default session backend for endpoint type: {endpoint.provider_type}")

    async def list_all(self) -> list[Session]:
        return await self.context.sessions.list()

    async def get(self, session_id: str) -> Session:
        session = await self.context.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} was not found")
        return session

    async def terminal_frames(
        self,
        session_id: str,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[TerminalFrame]:
        await self.get(session_id)
        if limit <= 0:
            raise ValidationError("limit must be positive")
        return await self.context.terminal_frames.list_frames(session_id, after_seq, limit)

    async def terminal_tail(self, session_id: str, limit_chars: int = 20_000) -> dict[str, object]:
        await self.get(session_id)
        if limit_chars <= 0:
            raise ValidationError("limit_chars must be positive")
        text = await self.context.terminal_frames.tail_text(session_id, limit_chars)
        last_seq = await self.context.terminal_frames.last_seq(session_id)
        return {"session_id": session_id, "text": text, "last_seq": last_seq}

    async def write(self, session_id: str, data: str) -> Session:
        session = await self.get(session_id)
        handle = self.context.active.session_handles.get(session_id)
        if handle is None:
            raise NotFoundError(f"session {session_id} is not active")
        await handle.write(data)
        await self.context.terminal_frames.append(
            session.session_id,
            TerminalFrameKind.REDACTED,
            "[REDACTED_INPUT]",
            stream="input",
        )
        return session

    async def resize(self, session_id: str, cols: int, rows: int) -> Session:
        if cols <= 0 or rows <= 0:
            raise ValidationError("terminal size must be positive")
        session = await self.get(session_id)
        handle = self.context.active.session_handles.get(session_id)
        if handle is None:
            raise NotFoundError(f"session {session_id} is not active")
        await handle.resize(cols, rows)
        await self.context.terminal_frames.append(
            session.session_id,
            TerminalFrameKind.RESIZE,
            f"{cols}x{rows}",
            stream="control",
        )
        session.terminal_cols = cols
        session.terminal_rows = rows
        await self.context.sessions.upsert(session)
        await self._emit(
            "session.resized",
            session,
            {"cols": cols, "rows": rows, "session_id": session.session_id},
        )
        return session

    async def terminate(self, session_id: str) -> Session:
        session = await self.get(session_id)
        handle = self.context.active.session_handles.get(session_id)
        if handle is not None:
            await handle.terminate()
            self.context.active.session_handles.pop(session_id, None)
        session.state = SessionState.TERMINATED
        session.interaction_state = InteractionState.NONE
        await self.context.sessions.upsert(session)
        await self._emit("session.terminated", session, {"state": session.state})
        return session

    async def reattach_on_startup(self, session: Session) -> bool:
        """Try to reclaim a durable session backend after runtime restart."""
        environment, target, endpoint = await self._resolve_target(
            session.environment_id,
            session.target_id,
        )
        _ = (environment, target)
        provider = self.context.providers.session.get(session.backend)
        if provider is None:
            return False
        status = await provider.status(session, endpoint)
        if not status.alive:
            if status.finished or status.exit_code is not None:
                session.exit_code = status.exit_code
                session.state = SessionState.TERMINATED
                session.interaction_state = InteractionState.NONE
                await self.context.sessions.upsert(session)
                await self._emit(
                    "session.recovered",
                    session,
                    {"returncode": status.exit_code, "session_id": session.session_id},
                )
                return True
            return False
        initial_offset = await self.context.terminal_frames.output_resume_offset(session.session_id)
        handle = await provider.attach(
            session,
            endpoint,
            self._make_output_callback(session),
            initial_output_offset=initial_offset,
        )
        session.state = SessionState.ACTIVE
        session.interaction_state = InteractionState.AUTOMATION_CONTROLLED
        session.backend_ref = handle.backend_ref()
        await self.context.sessions.upsert(session)
        self.context.active.session_handles[session.session_id] = handle
        watcher = asyncio.create_task(self._watch_session(session.session_id))
        self.context.active.background_tasks.append(watcher)
        await self._emit(
            "session.reconnected",
            session,
            {"session_id": session.session_id, "backend": session.backend},
        )
        return True

    async def _watch_session(self, session_id: str) -> None:
        handle = self.context.active.session_handles.get(session_id)
        if handle is None:
            return
        exit_code = await handle.wait()
        await handle.close()
        session = await self.get(session_id)
        session.exit_code = exit_code
        session.state = SessionState.TERMINATED
        session.interaction_state = InteractionState.NONE
        await self.context.sessions.upsert(session)
        self.context.active.session_handles.pop(session_id, None)
        await self._emit("session.terminated", session, {"returncode": exit_code})

    def _make_output_callback(self, session: Session):
        async def on_output(stream: str, chunk: str) -> None:
            await self.context.terminal_frames.append(
                session.session_id,
                TerminalFrameKind.OUTPUT,
                chunk,
                stream=stream,
            )
            await self._emit(
                "session.output",
                session,
                {"stream": stream, "chunk": chunk, "session_id": session.session_id},
            )

        return on_output

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
