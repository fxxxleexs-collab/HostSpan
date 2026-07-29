from __future__ import annotations

from typing import Any

import httpx

MAX_ERROR_BODY_CHARS = 2_000


def format_http_status_error(provider: str, exc: httpx.HTTPStatusError) -> str:
    response = exc.response
    request_id = _request_id(response)
    body = _response_body_summary(response)
    request_id_part = f", request_id={request_id}" if request_id else ""
    return (
        f"{provider} API returned HTTP {response.status_code} {response.reason_phrase}"
        f"{request_id_part}: {body}"
    )


def should_retry_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _request_id(response: httpx.Response) -> str | None:
    for header in ["x-request-id", "request-id", "anthropic-request-id"]:
        value = response.headers.get(header)
        if value:
            return value
    return None


def _response_body_summary(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return _truncate(text) if text else "<empty response body>"
    return _truncate(_json_error_summary(payload))


def _json_error_summary(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = []
            for key in ["type", "code", "message"]:
                value = error.get(key)
                if value:
                    parts.append(f"{key}={value}")
            if parts:
                return "; ".join(parts)
        if isinstance(error, str):
            return error
        parts = []
        for key in ["type", "code", "message"]:
            value = payload.get(key)
            if value:
                parts.append(f"{key}={value}")
        if parts:
            return "; ".join(parts)
    return str(payload)


def _truncate(text: str) -> str:
    if len(text) <= MAX_ERROR_BODY_CHARS:
        return text
    return text[:MAX_ERROR_BODY_CHARS] + "...[truncated]"
