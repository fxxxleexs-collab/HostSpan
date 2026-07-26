from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from multiprocessing.connection import Connection, Listener
from typing import Any

from environment_runtime.broker.address import BrokerAddress, default_broker_address
from environment_runtime.broker.commands import RuntimeCommandHandler
from environment_runtime.broker.protocol import (
    decode_message,
    encode_message,
    error_response,
    ok_response,
)
from environment_runtime.config import RuntimeSettings
from environment_runtime.services.runtime import RuntimeContext, build_runtime, shutdown_runtime


class LocalBrokerServer:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        address: BrokerAddress | None = None,
    ) -> None:
        self.settings = settings or RuntimeSettings()
        self.address = address or default_broker_address(self.settings)
        self._listener: Listener | None = None
        self._runtime: RuntimeContext | None = None
        self._handler: RuntimeCommandHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self.ready = threading.Event()
        self._accept_thread: threading.Thread | None = None

    async def serve_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._runtime = await build_runtime(self.settings)
        self._handler = RuntimeCommandHandler(self._runtime)
        self._prepare_address()
        self._listener = Listener(self.address.address, family=self.address.family, authkey=None)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self.ready.set()
        try:
            await self._stop_event.wait()
        finally:
            await self._close()

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        listener = self._listener
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()

    def _prepare_address(self) -> None:
        if self.address.family == "AF_UNIX" and os.path.exists(self.address.address):
            os.unlink(self.address.address)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while True:
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                return
            thread = threading.Thread(target=self._handle_connection, args=(connection,), daemon=True)
            thread.start()

    def _handle_connection(self, connection: Connection) -> None:
        with contextlib.closing(connection):
            try:
                request = decode_message(connection.recv_bytes())
                response = self._dispatch_threadsafe(request)
            except Exception as exc:
                response = error_response(type(exc).__name__, str(exc))
            connection.send_bytes(encode_message(response))

    def _dispatch_threadsafe(self, request: dict[str, Any]) -> dict[str, Any]:
        loop = self._loop
        if loop is None:
            return error_response("BrokerNotReady", "broker event loop is not ready")
        future = asyncio.run_coroutine_threadsafe(self._dispatch(request), loop)
        return future.result()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method", ""))
        params = request.get("params", {})
        if not isinstance(params, dict):
            return error_response("ValidationError", "broker params must be an object")
        if method == "broker.shutdown":
            await self.stop()
            return ok_response({"status": "stopping"})
        handler = self._handler
        if handler is None:
            return error_response("BrokerNotReady", "broker handler is not ready")
        try:
            return ok_response(await handler.handle(method, params))
        except Exception as exc:
            return error_response(type(exc).__name__, str(exc))

    async def _close(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()
        if self._runtime is not None:
            await shutdown_runtime(self._runtime)
            self._runtime = None
        if self.address.family == "AF_UNIX" and os.path.exists(self.address.address):
            with contextlib.suppress(OSError):
                os.unlink(self.address.address)
