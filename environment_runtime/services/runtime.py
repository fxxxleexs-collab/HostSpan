from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from environment_runtime.config import RuntimeSettings
from environment_runtime.core.models import (
    Artifact,
    Endpoint,
    Environment,
    InputRequest,
    Session,
    Task,
    Workspace,
    WriterLease,
)
from environment_runtime.events.bus import InMemoryEventBus
from environment_runtime.persistence.database import create_engine, create_schema
from environment_runtime.persistence.repositories import (
    LogRepository,
    SqlAlchemyEventStore,
    SqlAlchemyRepository,
    TerminalFrameRepository,
)
from environment_runtime.providers.registry import ProviderRegistry
from environment_runtime.providers.session.base import SessionHandle
from environment_runtime.services.recovery import RecoveryService
from environment_runtime.services.secrets import InMemorySecretStore


@dataclass
class ActiveRuntimeState:
    task_handles: dict[str, Any] = field(default_factory=dict)
    session_handles: dict[str, SessionHandle] = field(default_factory=dict)
    background_tasks: list[asyncio.Task[object]] = field(default_factory=list)


@dataclass
class RuntimeContext:
    settings: RuntimeSettings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    providers: ProviderRegistry
    event_bus: InMemoryEventBus
    event_store: SqlAlchemyEventStore
    log_repository: LogRepository
    terminal_frames: TerminalFrameRepository
    endpoints: SqlAlchemyRepository[Endpoint]
    environments: SqlAlchemyRepository[Environment]
    workspaces: SqlAlchemyRepository[Workspace]
    tasks: SqlAlchemyRepository[Task]
    sessions: SqlAlchemyRepository[Session]
    artifacts: SqlAlchemyRepository[Artifact]
    inputs: SqlAlchemyRepository[InputRequest]
    leases: SqlAlchemyRepository[WriterLease]
    active: ActiveRuntimeState = field(default_factory=ActiveRuntimeState)
    secrets: InMemorySecretStore = field(default_factory=InMemorySecretStore)


async def build_runtime(settings: RuntimeSettings) -> RuntimeContext:
    engine = create_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)
    # Ensure detached-task log/status directories exist.
    data_dir = settings.runtime.data_dir
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "status").mkdir(parents=True, exist_ok=True)
    providers = ProviderRegistry(settings)
    context = RuntimeContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        providers=providers,
        event_bus=InMemoryEventBus(),
        event_store=SqlAlchemyEventStore(session_factory),
        log_repository=LogRepository(session_factory),
        terminal_frames=TerminalFrameRepository(session_factory),
        endpoints=SqlAlchemyRepository(session_factory, "endpoint", Endpoint.model_validate, "endpoint_id"),
        environments=SqlAlchemyRepository(
            session_factory, "environment", Environment.model_validate, "environment_id"
        ),
        workspaces=SqlAlchemyRepository(
            session_factory, "workspace", Workspace.model_validate, "workspace_id"
        ),
        tasks=SqlAlchemyRepository(session_factory, "task", Task.model_validate, "task_id"),
        sessions=SqlAlchemyRepository(session_factory, "session", Session.model_validate, "session_id"),
        artifacts=SqlAlchemyRepository(
            session_factory, "artifact", Artifact.model_validate, "artifact_id"
        ),
        inputs=SqlAlchemyRepository(
            session_factory, "input_request", InputRequest.model_validate, "request_id"
        ),
        leases=SqlAlchemyRepository(session_factory, "writer_lease", WriterLease.model_validate, "lease_id"),
    )
    ssh_transport = context.providers.transport.get("ssh")
    if hasattr(ssh_transport, "set_secret_resolver"):
        ssh_transport.set_secret_resolver(context.secrets.get)
    # A freshly built context has no live handles, so any persisted task/session
    # claiming to be active is stale (its handle died with the previous process).
    # Reconcile before serving so the DB no longer lies. See RecoveryService.
    await RecoveryService(context).reconcile_on_startup()
    return context


async def shutdown_runtime(context: RuntimeContext) -> None:
    for task_handle in list(context.active.task_handles.values()):
        # Detached (persistent) handles survive a shutdown: stop watching but
        # leave the subprocess running. Others are killed as before.
        if hasattr(task_handle, "detach"):
            await task_handle.detach()
        elif hasattr(task_handle, "cancel"):
            await task_handle.cancel()
    for session_handle in list(context.active.session_handles.values()):
        if getattr(session_handle, "backend_name", None) == "ssh_tmux":
            await session_handle.detach()
        elif hasattr(session_handle, "terminate"):
            await session_handle.terminate()
    for task in context.active.background_tasks:
        if hasattr(task, "cancel") and not task.done():
            task.cancel()
    for task in context.active.background_tasks:
        if hasattr(task, "done"):
            with contextlib.suppress(BaseException):
                await task
    await context.engine.dispose()
