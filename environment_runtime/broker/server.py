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
    stream_message,
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
                method = str(request.get("method", ""))
                if method in {"event.subscribe", "session.subscribe_frames"}:
                    self._stream_threadsafe(connection, request)
                    return
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

    def _stream_threadsafe(self, connection: Connection, request: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            connection.send_bytes(
                encode_message(error_response("BrokerNotReady", "broker event loop is not ready"))
            )
            return
        future = asyncio.run_coroutine_threadsafe(self._stream(connection, request), loop)
        future.result()

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

    async def _stream(self, connection: Connection, request: dict[str, Any]) -> None:
        method = str(request.get("method", ""))
        params = request.get("params", {})
        if not isinstance(params, dict):
            await asyncio.to_thread(
                connection.send_bytes,
                encode_message(error_response("ValidationError", "broker params must be an object")),
            )
            return
        runtime = self._runtime
        if runtime is None:
            await asyncio.to_thread(
                connection.send_bytes,
                encode_message(error_response("BrokerNotReady", "broker runtime is not ready")),
            )
            return
        try:
            if method == "event.subscribe":
                await _stream_events(connection, runtime, params)
                return
            if method == "session.subscribe_frames":
                await _stream_session_frames(connection, runtime, params)
                return
            await asyncio.to_thread(
                connection.send_bytes,
                encode_message(error_response("ValidationError", f"unknown stream method: {method}")),
            )
        except (BrokenPipeError, EOFError, OSError):
            return
        except Exception as exc:
            with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                await asyncio.to_thread(
                    connection.send_bytes,
                    encode_message(error_response(type(exc).__name__, str(exc))),
                )

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


async def _stream_events(
    connection: Connection,
    runtime: RuntimeContext,
    params: dict[str, Any],
) -> None:
    after_sequence = int(params.get("after_sequence", 0))
    max_items = _optional_positive_int(params.get("max_items"))
    timeout_seconds = _optional_positive_float(params.get("timeout_seconds"))
    heartbeat_seconds = float(params.get("heartbeat_seconds", 15.0))
    resource_type = params.get("resource_type")
    resource_id = params.get("resource_id")
    event_types = set(params.get("event_types", []))
    sent = 0
    last_sequence = after_sequence
    queue = await runtime.event_bus.subscribe()
    deadline = _deadline(timeout_seconds)
    try:
        await _send_stream(connection, "start", {"method": "event.subscribe"})
        for event in await runtime.event_store.list_events():
            if event.sequence <= after_sequence:
                continue
            if not _event_matches(event, resource_type, resource_id, event_types):
                continue
            await _send_stream(connection, "item", event.model_dump(mode="json"))
            sent += 1
            last_sequence = max(last_sequence, event.sequence)
            if _limit_reached(sent, max_items):
                await _send_stream(connection, "end", {"reason": "max_items", "items": sent})
                return
        while True:
            if _limit_reached(sent, max_items):
                await _send_stream(connection, "end", {"reason": "max_items", "items": sent})
                return
            timeout = _next_wait(deadline, heartbeat_seconds)
            if timeout is not None and timeout <= 0:
                await _send_stream(connection, "end", {"reason": "timeout", "items": sent})
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                await _send_stream(connection, "heartbeat", {"items": sent})
                continue
            if event.sequence <= last_sequence:
                continue
            if not _event_matches(event, resource_type, resource_id, event_types):
                continue
            await _send_stream(connection, "item", event.model_dump(mode="json"))
            sent += 1
            last_sequence = max(last_sequence, event.sequence)
    finally:
        await runtime.event_bus.unsubscribe(queue)


async def _stream_session_frames(
    connection: Connection,
    runtime: RuntimeContext,
    params: dict[str, Any],
) -> None:
    session_id = str(params["session_id"])
    after_seq = params.get("after_seq")
    last_seq = int(after_seq) if after_seq is not None else -1
    max_items = _optional_positive_int(params.get("max_items"))
    timeout_seconds = _optional_positive_float(params.get("timeout_seconds"))
    heartbeat_seconds = float(params.get("heartbeat_seconds", 15.0))
    sent = 0
    queue = await runtime.event_bus.subscribe()
    deadline = _deadline(timeout_seconds)
    try:
        await _send_stream(
            connection,
            "start",
            {"method": "session.subscribe_frames", "session_id": session_id},
        )
        sent, last_seq = await _send_new_frames(
            connection,
            runtime,
            session_id,
            last_seq,
            sent,
            max_items,
        )
        if _limit_reached(sent, max_items):
            await _send_stream(connection, "end", {"reason": "max_items", "items": sent})
            return
        while True:
            timeout = _next_wait(deadline, heartbeat_seconds)
            if timeout is not None and timeout <= 0:
                await _send_stream(connection, "end", {"reason": "timeout", "items": sent})
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                await _send_stream(connection, "heartbeat", {"items": sent, "last_seq": last_seq})
                continue
            if event.resource_type != "session" or event.resource_id != session_id:
                continue
            sent, last_seq = await _send_new_frames(
                connection,
                runtime,
                session_id,
                last_seq,
                sent,
                max_items,
            )
            if _limit_reached(sent, max_items):
                await _send_stream(connection, "end", {"reason": "max_items", "items": sent})
                return
    finally:
        await runtime.event_bus.unsubscribe(queue)


async def _send_new_frames(
    connection: Connection,
    runtime: RuntimeContext,
    session_id: str,
    last_seq: int,
    sent: int,
    max_items: int | None,
) -> tuple[int, int]:
    while True:
        frames = await runtime.terminal_frames.list_frames(session_id, last_seq, limit=500)
        if not frames:
            return sent, last_seq
        for frame in frames:
            await _send_stream(connection, "item", frame.model_dump(mode="json"))
            sent += 1
            last_seq = max(last_seq, frame.seq)
            if _limit_reached(sent, max_items):
                return sent, last_seq


async def _send_stream(connection: Connection, event: str, result: Any) -> None:
    await asyncio.to_thread(connection.send_bytes, encode_message(stream_message(event, result)))


def _event_matches(
    event: Any,
    resource_type: Any,
    resource_id: Any,
    event_types: set[Any],
) -> bool:
    if resource_type is not None and event.resource_type != str(resource_type):
        return False
    if resource_id is not None and event.resource_id != str(resource_id):
        return False
    return not (event_types and event.event_type not in {str(item) for item in event_types})


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _optional_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _deadline(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return asyncio.get_running_loop().time() + timeout_seconds


def _next_wait(deadline: float | None, heartbeat_seconds: float) -> float | None:
    if deadline is None:
        return heartbeat_seconds
    remaining = deadline - asyncio.get_running_loop().time()
    return min(max(remaining, 0.0), heartbeat_seconds)


def _limit_reached(sent: int, max_items: int | None) -> bool:
    return max_items is not None and sent >= max_items
