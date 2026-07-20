from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from environment_runtime.api.errors import install_error_handlers
from environment_runtime.api.routes import (
    artifacts_router,
    endpoints_router,
    environments_router,
    interactions_router,
    sessions_router,
    tasks_router,
    workspaces_router,
)
from environment_runtime.config import RuntimeSettings
from environment_runtime.services.runtime import build_runtime, shutdown_runtime


def create_app(settings: RuntimeSettings | None = None) -> FastAPI:
    resolved_settings = settings or RuntimeSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = await build_runtime(resolved_settings)
        yield
        await shutdown_runtime(app.state.runtime)

    app = FastAPI(title="Environment Runtime", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(endpoints_router)
    app.include_router(environments_router)
    app.include_router(workspaces_router)
    app.include_router(tasks_router)
    app.include_router(sessions_router)
    app.include_router(interactions_router)
    app.include_router(artifacts_router)
    return app
