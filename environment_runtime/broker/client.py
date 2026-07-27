from __future__ import annotations

from collections.abc import Iterator
from multiprocessing.connection import Client
from typing import Any

from environment_runtime.broker.address import BrokerAddress
from environment_runtime.broker.protocol import decode_message, encode_message, request_message
from environment_runtime.core.errors import ProviderError


class BrokerClient:
    def __init__(self, address: BrokerAddress, timeout: float | None = None) -> None:
        self.address = address
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        connection = Client(self.address.address, family=self.address.family, authkey=None)
        try:
            connection.send_bytes(encode_message(request_message(method, params)))
            response = decode_message(connection.recv_bytes())
        finally:
            connection.close()
        if response.get("ok") is True:
            return response.get("result")
        error_payload = response.get("error")
        error = error_payload if isinstance(error_payload, dict) else {}
        error_type = str(error.get("type", "BrokerError"))
        message = str(error.get("message", "broker request failed"))
        raise ProviderError(f"{error_type}: {message}")

    def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        include_control: bool = False,
    ) -> Iterator[Any]:
        connection = Client(self.address.address, family=self.address.family, authkey=None)
        try:
            connection.send_bytes(encode_message(request_message(method, params)))
            while True:
                message = decode_message(connection.recv_bytes())
                if message.get("ok") is not True:
                    error_payload = message.get("error")
                    error = error_payload if isinstance(error_payload, dict) else {}
                    error_type = str(error.get("type", "BrokerError"))
                    error_message = str(error.get("message", "broker stream failed"))
                    raise ProviderError(f"{error_type}: {error_message}")
                event = str(message.get("event", "item"))
                if include_control or event == "item":
                    yield message if include_control else message.get("result")
                if event == "end":
                    return
        finally:
            connection.close()
