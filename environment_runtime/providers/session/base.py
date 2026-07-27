from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from environment_runtime.core.models import Endpoint, Environment, ExecutionTarget, Session

SessionOutputCallback = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class TerminalSize:
    cols: int = 120
    rows: int = 30


@dataclass(frozen=True)
class SessionCreateParams:
    session_id: str
    environment: Environment
    target: ExecutionTarget
    endpoint: Endpoint
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    terminal_size: TerminalSize = TerminalSize()
    term_type: str = "xterm-256color"


@dataclass(frozen=True)
class SessionBackendStatus:
    alive: bool
    exit_code: int | None = None
    detail: str | None = None
    finished: bool = False


class SessionHandle(Protocol):
    backend_name: str

    def backend_ref(self) -> dict[str, object]:
        """Return backend-specific metadata safe to persist on the Session."""

    async def write(self, data: str) -> None:
        """Write text to the interactive session input stream."""

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the remote/local terminal. Providers may no-op if unsupported."""

    async def wait(self) -> int | None:
        """Wait for the session to finish and return its exit code if known."""

    async def detach(self) -> None:
        """Stop local bookkeeping while leaving a detachable backend alive if possible."""

    async def terminate(self) -> None:
        """Terminate the interactive session."""

    async def close(self) -> None:
        """Release local resources associated with this handle."""


class SessionBackendProvider(Protocol):
    backend_name: str

    async def create(
        self,
        params: SessionCreateParams,
        on_output: SessionOutputCallback,
    ) -> SessionHandle:
        """Create a new interactive session."""

    async def attach(
        self,
        session: Session,
        endpoint: Endpoint,
        on_output: SessionOutputCallback,
        initial_output_offset: int = 0,
    ) -> SessionHandle:
        """Attach to a persisted session if the backend supports it."""

    async def status(self, session: Session, endpoint: Endpoint) -> SessionBackendStatus:
        """Return best-effort backend liveness for a persisted session."""
