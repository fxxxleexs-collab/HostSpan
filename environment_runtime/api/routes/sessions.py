from __future__ import annotations

from fastapi import APIRouter, Depends

from environment_runtime.api.dependencies import get_runtime
from environment_runtime.api.schemas import (
    CreateSessionRequest,
    ResizeSessionRequest,
    WriteSessionRequest,
)
from environment_runtime.services.runtime import RuntimeContext
from environment_runtime.services.security import WriterLeaseService
from environment_runtime.services.session import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(body: CreateSessionRequest, runtime: RuntimeContext = Depends(get_runtime)):
    return await SessionService(runtime).create(
        body.environment_id,
        body.target_id,
        body.argv,
        cwd=body.cwd,
        env=body.env,
        backend=body.backend,
        cols=body.cols,
        rows=body.rows,
        term_type=body.term_type,
    )


@router.get("")
async def list_sessions(runtime: RuntimeContext = Depends(get_runtime)):
    return await SessionService(runtime).list_all()


@router.get("/{session_id}")
async def get_session(session_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await SessionService(runtime).get(session_id)


@router.post("/{session_id}/write")
async def write_session(
    session_id: str,
    body: WriteSessionRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    await WriterLeaseService(runtime).validate(session_id, body.owner_id)
    return await SessionService(runtime).write(session_id, body.data)


@router.post("/{session_id}/resize")
async def resize_session(
    session_id: str,
    body: ResizeSessionRequest,
    runtime: RuntimeContext = Depends(get_runtime),
):
    return await SessionService(runtime).resize(session_id, body.cols, body.rows)


@router.post("/{session_id}/terminate")
async def terminate_session(session_id: str, runtime: RuntimeContext = Depends(get_runtime)):
    return await SessionService(runtime).terminate(session_id)
