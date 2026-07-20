from __future__ import annotations

from fastapi import Request

from environment_runtime.services.runtime import RuntimeContext


def get_runtime(request: Request) -> RuntimeContext:
    return request.app.state.runtime
