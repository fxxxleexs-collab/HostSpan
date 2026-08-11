from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from mini_harness.config import ModelConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.models.http_errors import format_http_status_error, should_retry_status
from mini_harness.models.schemas import AgentDecision, FinalDecision, ModelMessage, ToolDecision
from mini_harness.tools.schemas import ToolDefinition

_DECISION_ADAPTER: TypeAdapter[AgentDecision] = TypeAdapter(AgentDecision)


class OpenAICompatibleModelProvider:
    def __init__(
        self, config: ModelConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport

    async def decide(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> AgentDecision:
        if not self.config.api_key:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "MINI_AGENT_API_KEY is required for OpenAI-compatible providers",
                recoverable=False,
            )
        if not self.config.model:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model is required for OpenAI-compatible providers",
                recoverable=False,
            )
        payload = {
            "model": self.config.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "tools": [_tool_payload(tool) for tool in tools],
            "tool_choice": "auto",
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.config.base_url or "https://api.openai.com/v1",
                    timeout=self.config.timeout_seconds,
                    transport=self.transport,
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                ) as client:
                    response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
                return _convert_response(response.json())
            except (httpx.TimeoutException, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise MiniHarnessError(
                        ErrorCode.MODEL_TIMEOUT,
                        "model request timed out",
                        recoverable=True,
                    ) from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if attempt >= self.config.max_retries or not should_retry_status(
                    exc.response.status_code
                ):
                    raise MiniHarnessError(
                        ErrorCode.MODEL_INVALID_RESPONSE,
                        format_http_status_error("OpenAI-compatible", exc),
                        recoverable=True,
                    ) from exc
            except MiniHarnessError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise MiniHarnessError(
                        ErrorCode.MODEL_INVALID_RESPONSE,
                        f"model request failed: {type(exc).__name__}",
                        recoverable=True,
                    ) from exc
            await asyncio.sleep(0.25 * (attempt + 1))
        raise MiniHarnessError(
            ErrorCode.MODEL_INVALID_RESPONSE,
            f"model request failed: {type(last_error).__name__}",
            recoverable=True,
        )


def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _convert_response(payload: dict[str, Any]) -> AgentDecision:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MiniHarnessError(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "model response did not include a chat message",
            recoverable=True,
        ) from exc
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        call = tool_calls[0]
        function = call.get("function", {})
        args_text = function.get("arguments") or "{}"
        try:
            arguments = json.loads(args_text)
        except json.JSONDecodeError as exc:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model tool call arguments were not valid JSON",
                recoverable=True,
            ) from exc
        return ToolDecision(
            type="tool",
            tool_name=str(function.get("name") or ""),
            arguments=arguments,
            reason_summary=_tool_reason(message.get("content")),
            raw_output=json.dumps(
                {"content": message.get("content"), "tool_calls": tool_calls},
                ensure_ascii=False,
                indent=2,
            ),
        )
    content = str(message.get("content") or "").strip()
    return parse_final_decision(content)


def parse_final_decision(content: str) -> AgentDecision:
    if not content.strip():
        raise MiniHarnessError(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "model final response was empty",
            recoverable=True,
        )
    try:
        decision = _DECISION_ADAPTER.validate_json(content)
        return decision.model_copy(update={"raw_output": content})
    except ValidationError as exc:
        _ = exc
        return FinalDecision(
            type="final",
            summary=_plain_text_summary(content),
            details=_plain_text_details(content),
            raw_output=content,
        )


def _plain_text_summary(content: str) -> str:
    for line in content.splitlines():
        normalized = line.strip()
        if normalized:
            return normalized if len(normalized) <= 300 else normalized[:297] + "..."
    return "Model returned a final response."


def _plain_text_details(content: str) -> str | None:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            rest = "\n".join(lines[index + 1 :]).strip()
            return rest or None
    return None


def _tool_reason(content: Any) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()
    return "Model selected a tool call."
