from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from environment_runtime.broker import BrokerAddress, BrokerClient, default_broker_address
from environment_runtime.config import RuntimeSettings


class RuntimeTransport(Protocol):
    """Stable SDK transport boundary.

    Facade clients express runtime operations as canonical method names. The
    transport decides whether those names go to the local broker, HTTP, or a
    future in-process/runtime-specific implementation.
    """

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Execute one request/response runtime command."""

    def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Subscribe to a runtime stream."""

    def close(self) -> None:
        """Release transport resources, if any."""


class BrokerTransport:
    def __init__(
        self,
        address: BrokerAddress | None = None,
        settings: RuntimeSettings | None = None,
        token: str | None = None,
        principal_id: str = "agent",
        principal_type: str = "agent",
        scope_id: str = "default",
    ) -> None:
        self.settings = settings or RuntimeSettings()
        self.address = address or default_broker_address(self.settings)
        self._client = BrokerClient(
            self.address,
            token=token,
            principal_id=principal_id,
            principal_type=principal_type,
            scope_id=scope_id,
            settings=self.settings,
        )

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._client.call(method, params)

    def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        yield from self._client.stream(method, params)

    def close(self) -> None:
        return None
