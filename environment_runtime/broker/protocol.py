from __future__ import annotations

import json
from typing import Any


def encode_message(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8")


def decode_message(data: bytes) -> dict[str, Any]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("broker message must be a JSON object")
    return payload


def request_message(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"method": method, "params": params or {}}


def ok_response(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def error_response(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
