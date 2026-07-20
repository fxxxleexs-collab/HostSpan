from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from environment_runtime.core.errors import (
    ConflictError,
    NotFoundError,
    RuntimeErrorBase,
    ValidationError,
)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _bad_request(_, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RuntimeErrorBase)
    async def _runtime_error(_, exc: RuntimeErrorBase) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
